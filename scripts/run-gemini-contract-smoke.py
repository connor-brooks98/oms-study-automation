#!/usr/bin/env python3
"""Offline-tested orchestration for the explicitly authorized Task 2.8 live smoke."""

from __future__ import annotations

import argparse
import asyncio
import base64
import csv
import hashlib
import json
import os
import re
import subprocess
import tempfile
import traceback
from collections.abc import AsyncIterable, Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from importlib import import_module
from io import BytesIO
from pathlib import Path
from time import monotonic
from typing import TYPE_CHECKING, Any, Protocol

from pydantic import BaseModel, ConfigDict, SecretStr, ValidationError
from reportlab.lib.pagesizes import letter  # type: ignore[import-untyped]
from reportlab.pdfgen.canvas import Canvas  # type: ignore[import-untyped]

from oms_hub.providers.gemini.client import (
    GeminiClientFactory,
    SdkFactory,
    translate_gemini_error,
)
from oms_hub.providers.gemini.errors import GeminiProviderError
from oms_hub.providers.gemini.models import GeminiConfig
from oms_hub.source_trust_schema29 import project_schema29_index_input

if TYPE_CHECKING:
    from oms_hub.artifacts import ArtifactService
    from oms_hub.indexing.service import IndexResult
    from oms_hub.knowledge.service import IndexInputView
    from oms_hub.security.secret_store import SecretStore

SYNTHETIC_COURSE_ID = "task-2-8-synthetic-course"
SYNTHETIC_EXAM_ID = "task-2-8-synthetic-exam"
SYNTHETIC_LECTURE_ID = "task-2-8-synthetic-lecture"
SYNTHETIC_REVISION_ID = "sr_aaaaaaaaaaaaaaaaaaaaaaaaaa"
SYNTHETIC_MARKER = "cobalt-otter-28"
SYNTHETIC_FACT = f"The Task 2.8 synthetic marker is {SYNTHETIC_MARKER}."
WRONG_LECTURE_ID = "task-2-8-wrong-lecture"
_REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
_MAX_DIAGNOSTIC_BYTES = 16 * 1024 * 1024
_IS_WINDOWS = os.name == "nt"
_CITATION_CHECKS = (
    "citation_presence",
    "citation_document_binding",
    "citation_file_binding",
    "citation_scope_binding",
    "citation_page_binding",
    "citation_excerpt_binding",
)
_PRIVATE_SLIDE_COORDINATE = re.compile(
    r"(?:slide\s+)?([1-9][0-9]*)(?::[1-9][0-9]*)?\Z"
)


class SmokeContractError(RuntimeError):
    _SAFE_REASONS = frozenset(
        {
            "citation_document_identity_unavailable",
            "citation_document_uri_absent",
            "citation_document_uri_invalid",
            "citation_excerpt_invalid",
            "citation_excerpt_absent",
            "citation_excerpt_mismatch",
            "citation_excerpt_unavailable",
            "citation_file_absent",
            "citation_file_invalid",
            "citation_metadata_invalid",
            "citation_metadata_absent",
            "citation_annotations_absent",
            "citation_annotations_invalid",
            "citation_content_absent",
            "citation_content_invalid",
            "citation_page_invalid",
            "citation_page_absent",
            "citation_page_mismatch",
            "citation_scope_mismatch",
            "citation_steps_absent",
            "citation_steps_invalid",
            "citation_wrong_document",
            "citation_wrong_file",
            "diagnostic_overflow",
            "diagnostic_permissions_unavailable",
            "negative_answer_invalid",
            "positive_answer_invalid",
            "positive_answer_missing_marker",
            "positive_answer_unsupported",
            "positive_citation_missing",
            "positive_citation_unresolved",
            "structured_output_invalid",
            "structured_output_absent",
            "structured_output_unavailable",
            "usage_input_absent",
            "usage_input_invalid",
            "usage_output_absent",
            "usage_output_invalid",
            "usage_count_invalid",
        }
    )

    def __init__(self, message: str, *, reason: str | None = None) -> None:
        super().__init__(message)
        self.reason = reason if reason in self._SAFE_REASONS else None


class SmokeTemporaryFailure(RuntimeError):
    pass


class LiveSmokeBlocked(RuntimeError):
    pass


def _canonical_slide_number(locator: str) -> int:
    match = _PRIVATE_SLIDE_COORDINATE.fullmatch(locator)
    if match is None:
        raise LiveSmokeBlocked("private shadow source has invalid slide evidence")
    try:
        return int(match.group(1))
    except ValueError:
        raise LiveSmokeBlocked(
            "private shadow source has invalid slide evidence"
        ) from None


def prepare_private_shadow_index_input(
    slide_revision_id: str,
    *,
    schema_version: int,
    artifacts: ArtifactService,
    materialization_root: Path,
    parser: Any | None = None,
) -> IndexInputView:
    return project_schema29_index_input(
        slide_revision_id,
        schema_version=schema_version,
        ingestion=artifacts.repository,
        catalog=artifacts.catalog,
        artifacts=artifacts,
        materialization_root=materialization_root,
        parser=parser,
    )


def run_private_shadow_preflight(
    slide_revision_id: str,
    *,
    schema_version: int,
    artifacts: ArtifactService,
    materialization_root: Path,
    parser: Any | None = None,
) -> dict[str, object]:
    from oms_hub.files.pdf import validate_pdf
    from oms_hub.indexing.service import build_index_manifest
    from oms_hub.knowledge.models import EvidenceLocatorKind

    view = prepare_private_shadow_index_input(
        slide_revision_id,
        schema_version=schema_version,
        artifacts=artifacts,
        materialization_root=materialization_root,
        parser=parser,
    )
    manifest = build_index_manifest(view)
    if not view.evidence_units or any(
        unit.locator.kind is not EvidenceLocatorKind.SLIDE
        for unit in view.evidence_units
    ):
        raise LiveSmokeBlocked("private shadow source has invalid slide evidence")
    slide_numbers = {
        _canonical_slide_number(unit.locator.value) for unit in view.evidence_units
    }
    inputs = (view.pptx.path, *(item.path for item in manifest.inputs))
    return {
        "status": "ready",
        "source_revision_hash": hashlib.sha256(
            view.source_revision_id.encode("utf-8")
        ).hexdigest(),
        "document_types": sorted(
            {"pptx", *(item.input_kind for item in manifest.inputs)}
        ),
        "page_count": validate_pdf(view.pdf.path).page_count,
        "slide_count": len(slide_numbers),
        "provider_operation_states": ["private_preflight_ready"],
        "byte_usage": {"index_inputs": sum(path.stat().st_size for path in inputs)},
        "warnings": [],
    }


class _OperationFailure(Exception):
    def __init__(self, status_code: int | None) -> None:
        self.status_code = status_code
        super().__init__("Gemini import operation failed")


class SmokeAnswer(BaseModel):
    model_config = ConfigDict(extra="forbid")

    answer: str
    supported: bool


@dataclass(frozen=True, slots=True)
class SmokeScope:
    course_id: str
    exam_id: str
    lecture_id: str


@dataclass(frozen=True, slots=True)
class SmokeCitation:
    document_name: str
    page_number: int | None
    excerpt: str


@dataclass(frozen=True, slots=True)
class SmokeQueryResult:
    answer: dict[str, object]
    citations: tuple[SmokeCitation, ...]
    input_tokens: int | None = None
    output_tokens: int | None = None
    citation_checks: tuple[tuple[str, str], ...] = ()
    usage_checks: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True, slots=True)
class CitationAudit:
    citations: tuple[SmokeCitation, ...]
    checks: dict[str, str]


@dataclass(frozen=True, slots=True)
class UsageAudit:
    input_tokens: int | None
    output_tokens: int | None
    checks: dict[str, str]


class DiagnosticSink(Protocol):
    def capture(self, label: str, value: object) -> None: ...

    def capture_exception(self, label: str, error: BaseException) -> None: ...


@dataclass(frozen=True, slots=True)
class _SyntheticDiagnosticRequest:
    output_path: Path
    course_id: str
    exam_id: str
    lecture_id: str
    source_revision_id: str
    fixture_sha256: str


@dataclass(slots=True)
class _SyntheticDiagnosticSink:
    request: _SyntheticDiagnosticRequest
    events: list[dict[str, object]] = field(default_factory=list)
    secrets: list[str] = field(default_factory=list)
    _closed: bool = False

    @classmethod
    def open(cls, request: _SyntheticDiagnosticRequest) -> _SyntheticDiagnosticSink:
        _validate_diagnostic_request(request)
        if request.output_path.exists():
            raise LiveSmokeBlocked("synthetic diagnostic output already exists")
        return cls(request=request)

    def add_secret(self, value: str) -> None:
        if value:
            self.secrets.append(value)

    def capture(self, label: str, value: object) -> None:
        self.events.append(
            {
                "label": label,
                "value": _diagnostic_value(value, tuple(self.secrets)),
            }
        )

    def capture_exception(self, label: str, error: BaseException) -> None:
        response = _field(error, "response")
        self.capture(
            label,
            {
                "exception_type": type(error).__name__,
                "message": str(error),
                "traceback": "".join(traceback.format_exception(error)),
                "status": _field(response, "status_code"),
                "headers": _field(response, "headers"),
                "body": _response_body(response),
            },
        )

    def close(self) -> None:
        if self._closed:
            return
        payload = json.dumps(
            {
                "schema_version": 1,
                "synthetic_only": True,
                "events": self.events,
            },
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        if len(payload) > _MAX_DIAGNOSTIC_BYTES:
            raise SmokeContractError(
                "synthetic diagnostic overflow",
                reason="diagnostic_overflow",
            )
        output = self.request.output_path
        file_descriptor, temporary_name = tempfile.mkstemp(
            dir=output.parent,
            prefix=f".{output.name}.",
            suffix=".tmp",
        )
        temporary = Path(temporary_name)
        try:
            try:
                _restrict_diagnostic_file(file_descriptor, temporary)
            except BaseException:
                os.close(file_descriptor)
                raise
            with os.fdopen(file_descriptor, "wb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.link(temporary, output)
        finally:
            temporary.unlink(missing_ok=True)
        self._closed = True

    def delete(self) -> None:
        self.request.output_path.unlink(missing_ok=True)
        self.events.clear()
        self.secrets.clear()


def _restrict_diagnostic_file(file_descriptor: int, path: Path) -> None:
    if not _IS_WINDOWS:
        fchmod = getattr(os, "fchmod", None)
        if callable(fchmod):
            fchmod(file_descriptor, 0o600)
        else:
            os.chmod(path, 0o600)
        return
    try:
        identity = subprocess.run(
            ["whoami", "/user", "/fo", "csv", "/nh"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        row = next(csv.reader([identity.stdout.strip()]))
        sid = row[1].strip() if len(row) == 2 else ""
        if identity.returncode != 0 or re.fullmatch(r"S-1(?:-\d+)+", sid) is None:
            raise ValueError
        secured = subprocess.run(
            [
                "icacls",
                str(path),
                "/inheritance:r",
                "/grant:r",
                f"*{sid}:(F)",
            ],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        verified = subprocess.run(
            ["icacls", str(path), "/verify"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        if secured.returncode != 0 or verified.returncode != 0:
            raise ValueError
        inspection_environment = dict(os.environ)
        inspection_environment["OMS_TASK28_DIAGNOSTIC_PATH"] = str(path)
        inspected = subprocess.run(
            [
                "powershell.exe",
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                (
                    "$acl=Get-Acl -LiteralPath $env:OMS_TASK28_DIAGNOSTIC_PATH;"
                    "$rules=@($acl.GetAccessRules($true,$false,"
                    "[System.Security.Principal.SecurityIdentifier]));"
                    "$items=@($rules|ForEach-Object{[pscustomobject]@{"
                    "Sid=$_.IdentityReference.Value;"
                    "Allow=($_.AccessControlType -eq "
                    "[System.Security.AccessControl.AccessControlType]::Allow);"
                    "FullControl=(($_.FileSystemRights -band "
                    "[System.Security.AccessControl.FileSystemRights]::FullControl) "
                    "-eq [System.Security.AccessControl.FileSystemRights]::FullControl);"
                    "Inherited=$_.IsInherited}});"
                    "[pscustomobject]@{Protected=$acl.AreAccessRulesProtected;"
                    "Rules=$items}|ConvertTo-Json -Compress -Depth 4"
                ),
            ],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
            env=inspection_environment,
        )
        inspection = json.loads(inspected.stdout)
        rules = inspection.get("Rules") if isinstance(inspection, Mapping) else None
        if isinstance(rules, Mapping):
            rule_list = [rules]
        elif isinstance(rules, list):
            rule_list = rules
        else:
            rule_list = []
        rule = rule_list[0] if len(rule_list) == 1 else None
        if (
            inspected.returncode != 0
            or not isinstance(inspection, Mapping)
            or inspection.get("Protected") is not True
            or not isinstance(rule, Mapping)
            or rule.get("Sid") != sid
            or rule.get("Allow") is not True
            or rule.get("FullControl") is not True
            or rule.get("Inherited") is not False
        ):
            raise ValueError
    except (OSError, StopIteration, subprocess.SubprocessError, ValueError):
        raise SmokeContractError(
            "synthetic diagnostic permissions were unavailable",
            reason="diagnostic_permissions_unavailable",
        ) from None


class SmokeSession(Protocol):
    async def create_store(self, display_name: str, embedding_model: str) -> str: ...

    async def upload_pdf(self, display_name: str, content: bytes) -> str: ...

    async def import_file(
        self,
        store_name: str,
        file_name: str,
        metadata: tuple[tuple[str, str], ...],
    ) -> str: ...

    async def wait_for_import(self, operation_name: str) -> str: ...

    async def query(
        self,
        store_name: str,
        prompt: str,
        scope: SmokeScope,
        *,
        response_schema: type[SmokeAnswer] | None,
        omit_thinking: bool,
    ) -> SmokeQueryResult: ...

    async def list_documents(self, store_name: str) -> tuple[str, ...]: ...

    async def delete_document(self, document_name: str) -> None: ...

    async def delete_file(self, file_name: str) -> None: ...

    async def delete_store(self, store_name: str) -> None: ...


class GoogleGenaiSmokeSession:
    """Minimum live adapter for the pinned google-genai 2.14.0 contract."""

    def __init__(
        self,
        api_key: str,
        *,
        sdk_factory: SdkFactory | None = None,
        diagnostic_sink: DiagnosticSink | None = None,
    ) -> None:
        self._config = GeminiConfig(api_key=SecretStr(api_key))
        self._clients = GeminiClientFactory(self._config, sdk_factory=sdk_factory)
        self._diagnostic_sink = diagnostic_sink
        self._store_name: str | None = None
        self._document_name: str | None = None
        self._file_name: str | None = None

    async def create_store(self, display_name: str, embedding_model: str) -> str:
        async with self._clients.client() as client:
            created = await _provider_call(
                lambda: client.file_search_stores.create(
                    config={
                        "display_name": display_name,
                        "embedding_model": embedding_model,
                    }
                ),
                diagnostic_sink=self._diagnostic_sink,
                label="create_store",
            )
        self._store_name = _provider_identity(created, "store")
        return self._store_name

    async def upload_pdf(self, display_name: str, content: bytes) -> str:
        async with self._clients.client() as client:
            uploaded = await _provider_call(
                lambda: client.files.upload(
                    file=BytesIO(content),
                    config={
                        "display_name": display_name,
                        "mime_type": "application/pdf",
                    },
                ),
                diagnostic_sink=self._diagnostic_sink,
                label="upload_pdf",
            )
        file_name = _provider_identity(uploaded, "file")
        self._file_name = file_name
        return file_name

    async def import_file(
        self,
        store_name: str,
        file_name: str,
        metadata: tuple[tuple[str, str], ...],
    ) -> str:
        custom_metadata = [
            {"key": key, "string_value": value} for key, value in metadata
        ]
        async with self._clients.client() as client:
            operation = await _provider_call(
                lambda: client.file_search_stores.import_file(
                    file_search_store_name=store_name,
                    file_name=file_name,
                    config={"custom_metadata": custom_metadata},
                ),
                diagnostic_sink=self._diagnostic_sink,
                label="import_file",
            )
        return _provider_identity(operation, "operation")

    async def wait_for_import(self, operation_name: str) -> str:
        try:
            operation_type = import_module("google.genai.types").ImportFileOperation
            operation = operation_type(name=operation_name)
        except Exception as error:
            raise translate_gemini_error(error) from None
        deadline = monotonic() + self._config.operation_timeout_seconds
        async with self._clients.client() as client:
            while True:
                remaining = deadline - monotonic()
                if remaining <= 0:
                    raise SmokeTemporaryFailure("Gemini import operation timed out")
                try:
                    async with asyncio.timeout(remaining):
                        operation = await client.operations.get(operation)
                except TimeoutError:
                    if self._diagnostic_sink is not None:
                        self._diagnostic_sink.capture_exception(
                            "wait_for_import.timeout",
                            TimeoutError("Gemini import operation timed out"),
                        )
                    raise SmokeTemporaryFailure("Gemini import operation timed out") from None
                except GeminiProviderError:
                    raise
                except Exception as error:
                    if self._diagnostic_sink is not None:
                        self._diagnostic_sink.capture_exception(
                            "wait_for_import.request_failed",
                            error,
                        )
                    raise translate_gemini_error(error) from None
                if self._diagnostic_sink is not None:
                    self._diagnostic_sink.capture("wait_for_import.response", operation)
                if bool(_field(operation, "done")):
                    break
                await asyncio.sleep(self._config.operation_poll_seconds)
        operation_error = _field(operation, "error")
        if operation_error:
            if self._diagnostic_sink is not None:
                self._diagnostic_sink.capture("wait_for_import.operation_error", operation_error)
            status = _field(operation_error, "code")
            raise translate_gemini_error(
                _OperationFailure(status if isinstance(status, int) else None)
            ) from None
        response = _field(operation, "response")
        document_name = _provider_identity(response, "document", "document_name")
        if self._store_name is not None:
            parent = _field(response, "parent")
            if parent is not None and (
                not isinstance(parent, str)
                or not _resource_identity_matches(parent, self._store_name)
            ):
                raise SmokeContractError("Gemini document parent did not match the store")
            prefix = f"{self._store_name}/documents/"
            if "/" not in document_name:
                document_name = f"{prefix}{document_name}"
            elif not document_name.startswith(prefix) or "/" in document_name[len(prefix) :]:
                raise SmokeContractError("Gemini document identity did not match the store")
        self._document_name = document_name
        return self._document_name

    async def query(
        self,
        store_name: str,
        prompt: str,
        scope: SmokeScope,
        *,
        response_schema: type[SmokeAnswer] | None,
        omit_thinking: bool,
    ) -> SmokeQueryResult:
        if not omit_thinking:
            raise SmokeContractError("Task 2.8 smoke requires omitted thinking configuration")
        body: dict[str, object] = {
            "model": self._config.file_search_model,
            "input": prompt,
            "store": False,
            "tools": [
                {
                    "type": "file_search",
                    "file_search_store_names": [store_name],
                    "metadata_filter": _scope_filter(scope),
                }
            ],
        }
        if response_schema is not None:
            body["response_format"] = {
                "type": "text",
                "mime_type": "application/json",
                "schema": response_schema.model_json_schema(),
            }
        if self._diagnostic_sink is not None:
            self._diagnostic_sink.capture("query.request", body)
        async with self._clients.client() as client:
            response = await _provider_call(
                lambda: client.interactions.create(**body),
                diagnostic_sink=self._diagnostic_sink,
                label="query.response",
            )
        output_text = _interaction_output(response)
        if response_schema is None:
            answer = {"answer": output_text, "supported": True}
        else:
            try:
                answer = response_schema.model_validate_json(output_text).model_dump(mode="json")
            except ValidationError:
                raise SmokeContractError(
                    "Gemini structured output did not match the required schema",
                    reason="structured_output_invalid",
                ) from None
        usage = _audit_usage(_field(response, "usage"))
        citations = _audit_citations(
            response,
            scope,
            self._document_name,
            self._file_name,
        )
        return SmokeQueryResult(
            answer=answer,
            citations=citations.citations,
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            citation_checks=tuple(citations.checks.items()),
            usage_checks=tuple(usage.checks.items()),
        )

    async def list_documents(self, store_name: str) -> tuple[str, ...]:
        async with self._clients.client() as client:
            listed = await _provider_call(
                lambda: client.file_search_stores.documents.list(parent=store_name),
                diagnostic_sink=self._diagnostic_sink,
                label="list_documents",
            )
            documents = await _collect(listed)
        return tuple(sorted(_provider_identity(item, "document") for item in documents))

    async def delete_document(self, document_name: str) -> None:
        await self._delete_resource(
            "document",
            lambda client: client.file_search_stores.documents.delete(
                    name=document_name,
                    config={"force": True},
            ),
        )

    async def delete_file(self, file_name: str) -> None:
        await self._delete_resource(
            "file",
            lambda client: client.files.delete(name=file_name),
        )

    async def delete_store(self, store_name: str) -> None:
        await self._delete_resource(
            "store",
            lambda client: client.file_search_stores.delete(
                    name=store_name,
                    config={"force": True},
            ),
        )

    async def _delete_resource(
        self,
        label: str,
        request: Callable[[Any], Awaitable[Any]],
    ) -> None:
        try:
            sdk_client = self._clients._build_sdk_client()  # noqa: SLF001
            client = sdk_client.aio
            close = client.aclose
        except Exception as error:
            if self._diagnostic_sink is not None:
                self._diagnostic_sink.capture_exception(
                    f"cleanup.{label}.client_setup_failed",
                    error,
                )
            raise translate_gemini_error(error) from None
        request_error: GeminiProviderError | None = None
        try:
            try:
                response = await request(client)
            except Exception as error:
                if self._diagnostic_sink is not None:
                    self._diagnostic_sink.capture_exception(
                        f"cleanup.{label}.delete_request_failed",
                        error,
                    )
                request_error = translate_gemini_error(error)
            else:
                if self._diagnostic_sink is not None:
                    self._diagnostic_sink.capture(
                        f"cleanup.{label}.delete_response",
                        response,
                    )
        finally:
            try:
                await close()
            except Exception as error:
                if self._diagnostic_sink is not None:
                    self._diagnostic_sink.capture_exception(
                        f"cleanup.{label}.context_close_failed",
                        error,
                    )
                if request_error is None:
                    request_error = translate_gemini_error(error)
        if request_error is not None:
            raise request_error from None


async def _provider_call(
    request: Callable[[], Awaitable[Any]],
    *,
    diagnostic_sink: DiagnosticSink | None = None,
    label: str = "provider_call",
) -> Any:
    try:
        response = await request()
    except GeminiProviderError:
        raise
    except Exception as error:
        if diagnostic_sink is not None:
            diagnostic_sink.capture_exception(f"{label}.failed", error)
        raise translate_gemini_error(error) from None
    if diagnostic_sink is not None:
        diagnostic_sink.capture(label, response)
    return response


def _provider_identity(value: object, label: str, field: str = "name") -> str:
    identity = _field(value, field)
    if not isinstance(identity, str) or not identity.strip():
        raise SmokeContractError(f"Gemini {label} identity was unavailable")
    normalized = identity.strip()
    if len(normalized) > 500 or not normalized.isprintable():
        raise SmokeContractError(f"Gemini {label} identity was invalid")
    return normalized


def _resource_identity_matches(actual: str, expected: str) -> bool:
    return actual == expected or ("/" not in actual and expected.endswith(f"/{actual}"))


def _field(value: object, name: str) -> object | None:
    if isinstance(value, Mapping):
        return value.get(name)
    try:
        return getattr(value, name, None)
    except Exception:
        return None


def _synthetic_diagnostic_request(output_path: Path) -> _SyntheticDiagnosticRequest:
    return _SyntheticDiagnosticRequest(
        output_path=output_path,
        course_id=SYNTHETIC_COURSE_ID,
        exam_id=SYNTHETIC_EXAM_ID,
        lecture_id=SYNTHETIC_LECTURE_ID,
        source_revision_id=SYNTHETIC_REVISION_ID,
        fixture_sha256=hashlib.sha256(synthetic_pdf_bytes()).hexdigest(),
    )


def _validate_diagnostic_request(request: _SyntheticDiagnosticRequest) -> None:
    expected = _synthetic_diagnostic_request(request.output_path)
    if request != expected:
        raise LiveSmokeBlocked("synthetic diagnostic scope mismatch")
    output = request.output_path
    if not output.is_absolute() or not output.parent.is_dir():
        raise LiveSmokeBlocked("synthetic diagnostic output path is unavailable")
    try:
        output.resolve().relative_to(_REPOSITORY_ROOT.resolve())
    except ValueError:
        pass
    else:
        raise LiveSmokeBlocked("synthetic diagnostic output must be outside the repository")


def _diagnostic_value(value: object, secrets: tuple[str, ...]) -> object:
    if isinstance(value, SecretStr):
        return "[REDACTED]"
    if isinstance(value, BaseModel):
        return _diagnostic_value(value.model_dump(mode="json"), secrets)
    if isinstance(value, Mapping):
        cleaned: dict[str, object] = {}
        for key, item in value.items():
            name = str(key)
            normalized = name.casefold().replace("_", "-")
            if normalized == "headers":
                cleaned[name] = "[REDACTED]"
            elif (
                normalized in {
                    "api-key",
                    "authorization",
                    "cookie",
                    "credentials",
                    "set-cookie",
                    "x-goog-api-key",
                }
                or normalized.endswith("-credential")
                or normalized.endswith("-secret")
            ):
                cleaned[name] = "[REDACTED]"
            else:
                cleaned[name] = _diagnostic_value(item, secrets)
        return cleaned
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_diagnostic_value(item, secrets) for item in value]
    if isinstance(value, bytes):
        cleaned_bytes = value
        for secret in secrets:
            cleaned_bytes = cleaned_bytes.replace(
                secret.encode("utf-8"),
                b"[REDACTED]",
            )
        return {"base64": base64.b64encode(cleaned_bytes).decode("ascii")}
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        cleaned_text = value
        for secret in secrets:
            cleaned_text = cleaned_text.replace(secret, "[REDACTED]")
        return cleaned_text
    attributes = getattr(value, "__dict__", None)
    if isinstance(attributes, Mapping):
        return _diagnostic_value(attributes, secrets)
    return _diagnostic_value(repr(value), secrets)


def _response_body(response: object | None) -> object | None:
    if response is None:
        return None
    content = _field(response, "content")
    if content is not None:
        return content
    return _field(response, "text")


def _scope_filter(scope: SmokeScope) -> str:
    values = {
        "course_id": scope.course_id,
        "exam_id": scope.exam_id,
        "lecture_id": scope.lecture_id,
    }
    if any(
        not value
        or len(value) > 128
        or not all(character.isalnum() or character in ".:_-" for character in value)
        for value in values.values()
    ):
        raise SmokeContractError("Gemini metadata scope was invalid")
    return " AND ".join(f'{key}="{value}"' for key, value in values.items())


def _interaction_output(response: object) -> str:
    output = _field(response, "output_text")
    if output is None:
        raise SmokeContractError(
            "Gemini structured output was absent",
            reason="structured_output_absent",
        )
    if not isinstance(output, str) or not output:
        raise SmokeContractError(
            "Gemini structured output was invalid",
            reason="structured_output_invalid",
        )
    return output


def _audit_usage(value: object | None) -> UsageAudit:
    checks: dict[str, str] = {}
    counts: list[int | None] = []
    for field_name, check_name in (
        ("total_input_tokens", "usage_input"),
        ("total_output_tokens", "usage_output"),
    ):
        raw = _field(value, field_name) if value is not None else None
        if raw is None:
            counts.append(None)
            checks[check_name] = f"{check_name}_absent"
        elif isinstance(raw, bool) or not isinstance(raw, int) or raw < 0:
            counts.append(None)
            checks[check_name] = f"{check_name}_invalid"
        else:
            counts.append(raw)
            checks[check_name] = "passed"
    return UsageAudit(counts[0], counts[1], checks)


def _audit_citations(
    response: object,
    scope: SmokeScope,
    document_name: str | None,
    file_name: str | None,
) -> CitationAudit:
    steps = _field(response, "steps")
    blocked = {name: "blocked_by_citation_presence" for name in _CITATION_CHECKS}
    if steps is None:
        blocked["citation_presence"] = "citation_steps_absent"
        return CitationAudit((), blocked)
    if not isinstance(steps, Iterable) or isinstance(steps, (str, bytes, Mapping)):
        blocked["citation_presence"] = "citation_steps_invalid"
        return CitationAudit((), blocked)
    found: list[SmokeCitation] = []
    results: dict[str, list[str]] = {name: [] for name in _CITATION_CHECKS}
    expected = {
        "course_id": scope.course_id,
        "exam_id": scope.exam_id,
        "lecture_id": scope.lecture_id,
        "source_revision_id": SYNTHETIC_REVISION_ID,
    }
    store_name = None
    if document_name is not None:
        candidate, separator, suffix = document_name.partition("/documents/")
        if separator and candidate and suffix and "/" not in suffix:
            store_name = candidate
    saw_model_output = False
    saw_content = False
    saw_annotations = False
    invalid_content = False
    invalid_annotations = False
    for step in steps:
        if _field(step, "type") != "model_output":
            continue
        saw_model_output = True
        contents = _field(step, "content")
        if contents is None:
            continue
        if not isinstance(contents, Iterable) or isinstance(
            contents, (str, bytes, Mapping)
        ):
            invalid_content = True
            continue
        saw_content = True
        for content in contents:
            if _field(content, "type") != "text":
                continue
            annotations = _field(content, "annotations")
            if annotations is None:
                continue
            if not isinstance(annotations, Iterable) or isinstance(
                annotations, (str, bytes, Mapping)
            ):
                invalid_annotations = True
                continue
            saw_annotations = True
            for annotation in annotations:
                if _field(annotation, "type") != "file_citation":
                    continue
                results["citation_presence"].append("passed")
                current: dict[str, str] = {}
                raw_metadata = _field(annotation, "custom_metadata")
                if raw_metadata is None:
                    current["citation_scope_binding"] = "citation_metadata_absent"
                elif isinstance(raw_metadata, (str, bytes)) or not isinstance(
                    raw_metadata,
                    (Mapping, Iterable),
                ):
                    current["citation_scope_binding"] = "citation_metadata_invalid"
                else:
                    try:
                        actual = _string_metadata(raw_metadata)
                    except SmokeContractError:
                        current["citation_scope_binding"] = "citation_metadata_invalid"
                    else:
                        current["citation_scope_binding"] = (
                            "passed"
                            if all(actual.get(key) == value for key, value in expected.items())
                            else "citation_scope_mismatch"
                        )
                if document_name is None or store_name is None:
                    current["citation_document_binding"] = (
                        "citation_document_identity_unavailable"
                    )
                locator = _field(annotation, "document_uri")
                if document_name is not None and store_name is not None:
                    if locator is None:
                        current["citation_document_binding"] = (
                            "citation_document_uri_absent"
                        )
                    elif (
                        not isinstance(locator, str)
                        or not locator
                        or len(locator) > 500
                        or not locator.isprintable()
                    ):
                        current["citation_document_binding"] = (
                            "citation_document_uri_invalid"
                        )
                    else:
                        current["citation_document_binding"] = (
                            "passed"
                            if locator in {store_name, document_name}
                            else "citation_wrong_document"
                        )
                cited_file = _field(annotation, "file_name")
                if cited_file is None:
                    current["citation_file_binding"] = "citation_file_absent"
                elif (
                    not isinstance(cited_file, str)
                    or not cited_file
                    or len(cited_file) > 500
                    or not cited_file.isprintable()
                ):
                    current["citation_file_binding"] = "citation_file_invalid"
                else:
                    current["citation_file_binding"] = (
                        "passed"
                        if file_name is not None
                        and _resource_identity_matches(cited_file, file_name)
                        else "citation_wrong_file"
                    )
                page_value = _field(annotation, "page_number")
                if page_value is None:
                    page = None
                    current["citation_page_binding"] = "citation_page_absent"
                else:
                    try:
                        page = _optional_page(page_value)
                    except SmokeContractError:
                        page = None
                        current["citation_page_binding"] = "citation_page_invalid"
                    else:
                        current["citation_page_binding"] = (
                            "passed" if page == 1 else "citation_page_mismatch"
                        )
                try:
                    excerpt = _citation_excerpt(annotation, _field(content, "text"))
                except SmokeContractError as error:
                    excerpt = ""
                    current["citation_excerpt_binding"] = (
                        "citation_excerpt_absent"
                        if error.reason == "citation_excerpt_unavailable"
                        else error.reason or "citation_excerpt_invalid"
                    )
                else:
                    current["citation_excerpt_binding"] = (
                        "passed" if SYNTHETIC_FACT in excerpt else "citation_excerpt_mismatch"
                    )
                for name, outcome in current.items():
                    results[name].append(outcome)
                if document_name is not None and all(
                    current.get(name) == "passed"
                    or (
                        name == "citation_document_binding"
                        and current.get(name) == "citation_document_uri_absent"
                    )
                    for name in _CITATION_CHECKS
                    if name != "citation_presence"
                ):
                    found.append(
                        SmokeCitation(
                            document_name=document_name,
                            page_number=page,
                            excerpt=excerpt,
                        )
                    )
    if not results["citation_presence"]:
        if not saw_model_output:
            diagnosis = "citation_steps_absent"
        elif invalid_content:
            diagnosis = "citation_content_invalid"
        elif not saw_content:
            diagnosis = "citation_content_absent"
        elif invalid_annotations:
            diagnosis = "citation_annotations_invalid"
        elif not saw_annotations:
            diagnosis = "citation_annotations_absent"
        else:
            diagnosis = "positive_citation_missing"
        blocked["citation_presence"] = diagnosis
        return CitationAudit((), blocked)
    checks: dict[str, str] = {}
    for name in _CITATION_CHECKS:
        outcomes = results[name]
        checks[name] = "passed" if "passed" in outcomes else outcomes[0]
    return CitationAudit(tuple(found), checks)


def _citations(
    response: object,
    store_name: str,
    scope: SmokeScope,
    document_name: str | None,
    file_display_name: str | None,
) -> tuple[SmokeCitation, ...]:
    del store_name
    audit = _audit_citations(response, scope, document_name, file_display_name)
    for name in (
        "citation_scope_binding",
        "citation_file_binding",
        "citation_document_binding",
        "citation_page_binding",
        "citation_excerpt_binding",
    ):
        reason = audit.checks[name]
        if reason not in {
            "passed",
            "blocked_by_citation_presence",
            "citation_document_uri_absent",
        }:
            messages = {
                "citation_scope_binding": (
                    "Gemini citation metadata did not match the requested scope"
                ),
                "citation_file_binding": "Gemini citation referenced the wrong file",
                "citation_document_binding": "Gemini citation referenced the wrong document",
                "citation_page_binding": "Gemini citation page did not satisfy the contract",
                "citation_excerpt_binding": "Gemini citation excerpt did not satisfy the contract",
            }
            raise SmokeContractError(messages[name], reason=reason)
    return audit.citations


def _string_metadata(value: object) -> dict[str, str]:
    if isinstance(value, Mapping):
        if len(value) > 16 or not all(
            isinstance(key, str)
            and 0 < len(key) <= 64
            and key.isprintable()
            and isinstance(text, str)
            and 0 < len(text) <= 512
            and text.isprintable()
            for key, text in value.items()
        ):
            raise SmokeContractError(
                "Gemini citation metadata was invalid",
                reason="citation_metadata_invalid",
            )
        return dict(value)
    if not isinstance(value, Iterable) or isinstance(value, (str, bytes)):
        return {}
    metadata: dict[str, str] = {}
    for item in value:
        if len(metadata) >= 16:
            raise SmokeContractError(
                "Gemini citation metadata was invalid",
                reason="citation_metadata_invalid",
            )
        key = _field(item, "key")
        text = _field(item, "string_value")
        if (
            not isinstance(key, str)
            or not 0 < len(key) <= 64
            or not key.isprintable()
            or not isinstance(text, str)
            or not 0 < len(text) <= 512
            or not text.isprintable()
            or key in metadata
        ):
            raise SmokeContractError(
                "Gemini citation metadata was invalid",
                reason="citation_metadata_invalid",
            )
        metadata[key] = text
    return metadata


def _citation_excerpt(annotation: object, content_text: object) -> str:
    source = _field(annotation, "source")
    if isinstance(source, str) and source:
        return _bounded_excerpt(source)
    if not isinstance(content_text, str):
        raise SmokeContractError(
            "Gemini citation excerpt was unavailable",
            reason="citation_excerpt_unavailable",
        )
    start = _field(annotation, "start_index")
    end = _field(annotation, "end_index")
    encoded = content_text.encode("utf-8")
    if (
        isinstance(start, bool)
        or isinstance(end, bool)
        or not isinstance(start, int)
        or not isinstance(end, int)
        or not 0 <= start < end <= len(encoded)
    ):
        raise SmokeContractError(
            "Gemini citation excerpt was unavailable",
            reason="citation_excerpt_unavailable",
        )
    try:
        return _bounded_excerpt(encoded[start:end].decode("utf-8"))
    except UnicodeDecodeError:
        raise SmokeContractError(
            "Gemini citation excerpt was invalid",
            reason="citation_excerpt_invalid",
        ) from None


def _bounded_excerpt(value: str) -> str:
    normalized = value.strip()
    if (
        not normalized
        or len(value) > 4096
        or any(not character.isprintable() and character not in "\r\n\t" for character in value)
    ):
        raise SmokeContractError(
            "Gemini citation excerpt was invalid",
            reason="citation_excerpt_invalid",
        )
    return normalized


def _optional_page(value: object) -> int | None:
    if value is None:
        return None
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not 1 <= value <= 1_000_000
    ):
        raise SmokeContractError(
            "Gemini citation page number was invalid",
            reason="citation_page_invalid",
        )
    return value


def _optional_count(value: object) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise SmokeContractError(
            "Gemini usage count was invalid",
            reason="usage_count_invalid",
        )
    return value


async def _collect(value: object) -> tuple[object, ...]:
    if isinstance(value, AsyncIterable):
        items = [item async for item in value]
        return tuple(items)
    if isinstance(value, Iterable) and not isinstance(value, (str, bytes, Mapping)):
        return tuple(value)
    raise SmokeContractError("Gemini document listing was unavailable")


class _TemporaryRetryService:
    def __init__(self) -> None:
        self.calls = 0

    async def index_revision(self, source_revision_id: str) -> IndexResult:
        from oms_hub.indexing.models import IndexState
        from oms_hub.indexing.service import IndexResult
        from oms_hub.providers.gemini.errors import GeminiTransientError

        self.calls += 1
        if self.calls == 1:
            raise GeminiTransientError("synthetic temporary failure")
        return IndexResult(source_revision_id, IndexState.READY)


def synthetic_pdf_bytes() -> bytes:
    output = BytesIO()
    page = Canvas(output, pagesize=letter, invariant=1, pageCompression=0)
    page.setTitle("Task 2.8 synthetic Gemini contract fixture")
    page.drawString(72, 720, SYNTHETIC_FACT)
    page.save()
    return output.getvalue()


async def run_contract_smoke(
    session: SmokeSession,
    *,
    clock: Callable[[], float] = monotonic,
    failure_evidence: dict[str, object] | None = None,
    diagnostic_sink: DiagnosticSink | None = None,
) -> dict[str, object]:
    pdf = synthetic_pdf_bytes()
    digest = hashlib.sha256(pdf).hexdigest()
    metadata = (
        ("authority_class", "course_material"),
        ("course_id", SYNTHETIC_COURSE_ID),
        ("exam_id", SYNTHETIC_EXAM_ID),
        ("lecture_id", SYNTHETIC_LECTURE_ID),
        ("source_revision_id", SYNTHETIC_REVISION_ID),
        ("input_key", "pdf"),
        ("input_kind", "pdf"),
        ("input_sha256", digest),
    )
    store_name: str | None = None
    file_name: str | None = None
    operation_name: str | None = None
    document_name: str | None = None
    resource_states = {
        "document": "not_started",
        "file": "not_started",
        "store": "not_started",
    }
    check_names = (
        "create_store",
        "upload_pdf",
        "import_file",
        "wait_for_import",
        "positive_answer",
        *_CITATION_CHECKS,
        "usage_input",
        "usage_output",
        "negative_structured_output",
        "wrong_lecture_filtering",
        "document_listing",
        "cleanup_document",
        "cleanup_file",
        "cleanup_store",
    )
    checks = dict.fromkeys(check_names, "not_run")
    errors: list[Exception] = []
    first_failure_stage: str | None = None
    cleanup_error: SmokeContractError | None = None
    cleanup_status = "not_started"
    positive: SmokeQueryResult | None = None
    negative_answer: SmokeAnswer | None = None
    citation: SmokeCitation | None = None
    started = clock()
    if failure_evidence is not None:
        failure_evidence.clear()
        failure_evidence["failure_stage"] = "create_store"

    def fail(check: str, diagnosis: str, error: Exception, stage: str) -> None:
        nonlocal first_failure_stage
        checks[check] = diagnosis
        errors.append(error)
        if first_failure_stage is None:
            first_failure_stage = stage

    def block_after(check: str, diagnosis: str) -> None:
        seen = False
        for name in check_names:
            if name == check:
                seen = True
                continue
            if seen and not name.startswith("cleanup_"):
                checks[name] = diagnosis

    if diagnostic_sink is not None:
        diagnostic_sink.capture(
            "contract.expected",
            {
                "course_id": SYNTHETIC_COURSE_ID,
                "exam_id": SYNTHETIC_EXAM_ID,
                "lecture_id": SYNTHETIC_LECTURE_ID,
                "source_revision_id": SYNTHETIC_REVISION_ID,
                "fixture_sha256": digest,
            },
        )
    try:
        resource_states["store"] = "unknown"
        try:
            store_name = await session.create_store(
                "Study Hub Task 2.8 synthetic contract",
                "models/gemini-embedding-2",
            )
        except Exception as error:
            fail("create_store", "create_store_failed", error, "create_store")
            block_after("create_store", "blocked_by_create_store")
        else:
            checks["create_store"] = "passed"
            resource_states["store"] = "confirmed"
        if store_name is not None:
            resource_states["file"] = "unknown"
            try:
                file_name = await session.upload_pdf("task-2-8-synthetic.pdf", pdf)
            except Exception as error:
                fail("upload_pdf", "upload_pdf_failed", error, "upload_pdf")
                block_after("upload_pdf", "blocked_by_upload_pdf")
            else:
                checks["upload_pdf"] = "passed"
                resource_states["file"] = "confirmed"
        if store_name is not None and file_name is not None:
            resource_states["document"] = "unknown"
            try:
                operation_name = await session.import_file(store_name, file_name, metadata)
            except Exception as error:
                fail("import_file", "import_file_failed", error, "import_file")
                block_after("import_file", "blocked_by_import_file")
            else:
                checks["import_file"] = "passed"
        if operation_name is not None:
            try:
                document_name = await session.wait_for_import(operation_name)
            except Exception as error:
                fail("wait_for_import", "wait_for_import_failed", error, "wait_for_import")
                block_after("wait_for_import", "blocked_by_wait_for_import")
            else:
                checks["wait_for_import"] = "passed"
                resource_states["document"] = "confirmed"
        if store_name is not None and document_name is not None:
            try:
                positive = await session.query(
                    store_name,
                    "Return only the Task 2.8 synthetic marker value stated in the indexed PDF.",
                    SmokeScope(
                        SYNTHETIC_COURSE_ID,
                        SYNTHETIC_EXAM_ID,
                        SYNTHETIC_LECTURE_ID,
                    ),
                    response_schema=None,
                    omit_thinking=True,
                )
            except Exception as error:
                fail("positive_answer", "positive_query_failed", error, "positive_query")
                for name in (*_CITATION_CHECKS, "usage_input", "usage_output"):
                    checks[name] = "blocked_by_positive_query"
            else:
                try:
                    answer = SmokeAnswer.model_validate(positive.answer)
                except ValidationError:
                    contract_error = SmokeContractError(
                        "Gemini structured output did not match the required schema",
                        reason="structured_output_invalid",
                    )
                    fail(
                        "positive_answer",
                        "structured_output_invalid",
                        contract_error,
                        "positive_validation",
                    )
                else:
                    if not answer.supported:
                        contract_error = SmokeContractError(
                            "structured output did not report grounded support",
                            reason="positive_answer_unsupported",
                        )
                        fail(
                            "positive_answer",
                            "positive_answer_unsupported",
                            contract_error,
                            "positive_validation",
                        )
                    elif SYNTHETIC_MARKER not in answer.answer:
                        contract_error = SmokeContractError(
                            "structured output did not preserve the synthetic marker",
                            reason="positive_answer_missing_marker",
                        )
                        fail(
                            "positive_answer",
                            "positive_answer_missing_marker",
                            contract_error,
                            "positive_validation",
                        )
                    else:
                        checks["positive_answer"] = "passed"
                citation_checks = dict(positive.citation_checks)
                if not citation_checks:
                    citation_checks = {
                        name: "passed" if positive.citations else "blocked_by_citation_presence"
                        for name in _CITATION_CHECKS
                    }
                    citation_checks["citation_presence"] = (
                        "passed" if positive.citations else "positive_citation_missing"
                    )
                checks.update(citation_checks)
                for name in _CITATION_CHECKS:
                    diagnosis = checks[name]
                    if diagnosis not in {
                        "passed",
                        "blocked_by_citation_presence",
                        "citation_document_uri_absent",
                    }:
                        contract_error = SmokeContractError(
                            "positive citation did not satisfy the contract",
                            reason=diagnosis,
                        )
                        fail(name, diagnosis, contract_error, "positive_validation")
                usage_checks = dict(positive.usage_checks)
                if not usage_checks:
                    usage_checks = {
                        "usage_input": (
                            "passed"
                            if positive.input_tokens is not None
                            else "usage_input_absent"
                        ),
                        "usage_output": (
                            "passed"
                            if positive.output_tokens is not None
                            else "usage_output_absent"
                        ),
                    }
                checks.update(usage_checks)
                citation = positive.citations[0] if positive.citations else None

            try:
                negative = await session.query(
                    store_name,
                    "Use only indexed files. If no matching source exists, return an empty "
                    "answer and supported=false.",
                    SmokeScope(
                        SYNTHETIC_COURSE_ID,
                        SYNTHETIC_EXAM_ID,
                        WRONG_LECTURE_ID,
                    ),
                    response_schema=SmokeAnswer,
                    omit_thinking=True,
                )
            except Exception as error:
                fail(
                    "negative_structured_output",
                    "negative_query_failed",
                    error,
                    "negative_query",
                )
                checks["wrong_lecture_filtering"] = "blocked_by_negative_query"
            else:
                try:
                    negative_answer = SmokeAnswer.model_validate(negative.answer)
                except ValidationError:
                    contract_error = SmokeContractError(
                        "Gemini structured output did not match the required schema",
                        reason="structured_output_invalid",
                    )
                    fail(
                        "negative_structured_output",
                        "structured_output_invalid",
                        contract_error,
                        "negative_validation",
                    )
                    checks["wrong_lecture_filtering"] = (
                        "blocked_by_negative_structured_output"
                    )
                else:
                    checks["negative_structured_output"] = "passed"
                    retrieved = (
                        bool(negative.citations)
                        or dict(negative.citation_checks).get("citation_presence")
                        == "passed"
                    )
                    if (
                        retrieved
                        or negative_answer.supported
                        or negative_answer.answer != ""
                    ):
                        contract_error = SmokeContractError(
                            "wrong-lecture metadata filter returned supported source content",
                            reason="negative_answer_invalid",
                        )
                        fail(
                            "wrong_lecture_filtering",
                            "negative_answer_invalid",
                            contract_error,
                            "negative_validation",
                        )
                    else:
                        checks["wrong_lecture_filtering"] = "passed"

            try:
                listed = await session.list_documents(store_name)
            except Exception as error:
                fail(
                    "document_listing",
                    "document_listing_failed",
                    error,
                    "list_documents",
                )
            else:
                if listed == (document_name,):
                    checks["document_listing"] = "passed"
                else:
                    contract_error = SmokeContractError(
                        "document listing did not round-trip the imported document"
                    )
                    fail(
                        "document_listing",
                        "document_listing_mismatch",
                        contract_error,
                        "list_documents",
                    )
    finally:
        try:
            await _cleanup(
                session,
                document_name,
                file_name,
                store_name,
                checks=checks,
            )
        except SmokeContractError as error:
            cleanup_error = error
            cleanup_status = "failed"
        else:
            cleanup_status = (
                "unknown" if "unknown" in resource_states.values() else "completed"
            )

    if cleanup_error is not None and not errors:
        errors.append(cleanup_error)
        first_failure_stage = "cleanup"
    if failure_evidence is not None:
        failure_evidence["failure_stage"] = first_failure_stage or "cleanup"
        failure_evidence["resources_created"] = dict(resource_states)
        failure_evidence["cleanup"] = {
            "attempted": sum(
                value is not None for value in (document_name, file_name, store_name)
            ),
            "status": cleanup_status,
        }
        failure_evidence["checks"] = dict(checks)
    if diagnostic_sink is not None:
        diagnostic_sink.capture("contract.check_matrix", checks)
    if errors:
        raise errors[0]
    if positive is None or negative_answer is None or citation is None:
        raise SmokeContractError("Task 2.8 smoke result was incomplete")
    assert store_name is not None
    assert file_name is not None
    assert operation_name is not None
    assert document_name is not None
    duration_ms = round((clock() - started) * 1000)
    return {
        "schema_version": 1,
        "status": "passed",
        "sdk_version": "2.14.0",
        "model": "gemini-3.7-flash",
        "embedding_model": "models/gemini-embedding-2",
        "source_revision_hash": hashlib.sha256(
            SYNTHETIC_REVISION_ID.encode("utf-8")
        ).hexdigest(),
        "document_types": ["pdf"],
        "page_count": 1,
        "operation_states": ["done"],
        "citation_resolution_rate": 1.0,
        "citation": {
            "page_number": citation.page_number,
            "excerpt_sha256": hashlib.sha256(citation.excerpt.encode("utf-8")).hexdigest(),
        },
        "negative_scope_retrieved": False,
        "structured_output": {
            "schema": type(negative_answer).__name__,
            "validated": True,
            "answer_sha256": hashlib.sha256(
                negative_answer.answer.encode("utf-8")
            ).hexdigest(),
        },
        "thinking_configuration": "omitted",
        "duration_ms": duration_ms,
        "usage": {
            "indexed_bytes": len(pdf),
            "input_tokens": positive.input_tokens,
            "output_tokens": positive.output_tokens,
        },
        "provider_ids": {
            "store": _redacted_identity(store_name),
            "file": _redacted_identity(file_name),
            "operation": _redacted_identity(operation_name),
            "document": _redacted_identity(document_name),
        },
        "warnings": [],
        "cleanup": {"attempted": 3, "status": "completed"},
    }


async def _cleanup(
    session: SmokeSession,
    document_name: str | None,
    file_name: str | None,
    store_name: str | None,
    *,
    checks: dict[str, str] | None = None,
) -> None:
    failures: list[str] = []
    for label, method, value in (
        ("document", session.delete_document, document_name),
        ("file", session.delete_file, file_name),
        ("store", session.delete_store, store_name),
    ):
        if value is None:
            if checks is not None:
                checks[f"cleanup_{label}"] = "not_available"
            continue
        try:
            await method(value)
        except Exception as error:  # noqa: PERF203 - cleanup must attempt every resource
            failures.append(type(error).__name__)
            if checks is not None:
                checks[f"cleanup_{label}"] = "cleanup_delete_failed"
        else:
            if checks is not None:
                checks[f"cleanup_{label}"] = "passed"
    if failures:
        raise SmokeContractError(f"provider cleanup failed: {', '.join(failures)}")


def _redacted_identity(value: str) -> str:
    return f"sha256:{hashlib.sha256(value.encode('utf-8')).hexdigest()[:16]}"


def _failure_record(
    error: GeminiProviderError | SmokeContractError | SmokeTemporaryFailure,
    evidence: Mapping[str, object],
) -> dict[str, object]:
    if isinstance(error, GeminiProviderError):
        category = error.category
        retryable = error.retryable
        status_code = error.provider_status_code
    elif isinstance(error, SmokeTemporaryFailure):
        category = "transient"
        retryable = True
        status_code = None
    else:
        category = "contract"
        retryable = False
        status_code = None
    if (
        isinstance(status_code, bool)
        or not isinstance(status_code, int)
        or not 100 <= status_code <= 599
    ):
        status_code = None
    record: dict[str, object] = {
        "schema_version": 1,
        "status": "failed",
        "failure_stage": evidence.get("failure_stage", "unknown"),
        "error_category": category,
        "provider_status_code": status_code,
        "retryable": retryable,
        "resources_created": evidence.get("resources_created", {}),
        "cleanup": evidence.get("cleanup", {"attempted": 0, "status": "not_started"}),
        "checks": evidence.get("checks", {}),
    }
    if isinstance(error, SmokeContractError) and error.reason is not None:
        record["contract_reason"] = error.reason
    if isinstance(error, GeminiProviderError) and error.diagnostic_code is not None:
        record["provider_reason"] = error.diagnostic_code
    return record


def run_temporary_failure_fixture() -> dict[str, object]:
    from oms_hub.db import Database
    from oms_hub.indexing.models import IndexJob, ProviderStore, StoreKey
    from oms_hub.indexing.repository import IndexRepository
    from oms_hub.indexing.worker import IndexWorker

    database = Database("sqlite://")
    database.create_schema()
    try:
        repository = IndexRepository(database)
        key = StoreKey.course(SYNTHETIC_COURSE_ID, SYNTHETIC_EXAM_ID)
        store = repository.create_store(
            ProviderStore(
                store_key=key,
                provider="gemini",
                provider_store_name="offlineFakeStores/task-2-8",
                embedding_model="models/gemini-embedding-2",
                authority_namespace=key.authority_namespace,
                course_id=key.course_id,
                exam_id=key.exam_id,
            )
        )
        job = repository.save_job(
            IndexJob(store_id=store.id, source_revision_id=SYNTHETIC_REVISION_ID)
        )
        clock = [datetime(2026, 8, 26, 12, 0, tzinfo=UTC)]
        service = _TemporaryRetryService()
        worker = IndexWorker(
            repository,
            service,
            worker_id="task-2-8-offline-retry",
            lease_seconds=60,
            now=lambda: clock[0],
        )

        worker.run_once()
        retry = repository.get_job(job.id)
        assert retry is not None and retry.next_attempt_at is not None
        next_attempt = datetime.fromisoformat(retry.next_attempt_at)
        backoff_seconds = round((next_attempt - clock[0]).total_seconds())
        clock[0] = next_attempt
        worker.run_once()
        resumed = repository.get_job(job.id)
        assert resumed is not None
        return {
            "first_state": retry.state.value,
            "retry_count": retry.retry_count,
            "error_category": retry.last_error_category,
            "backoff_seconds": backoff_seconds,
            "resumed_state": resumed.state.value,
            "service_calls": service.calls,
        }
    finally:
        database.close()


async def run_authorized_live_smoke(
    *,
    secret_store: SecretStore | None = None,
    session_factory: Callable[[str], SmokeSession] | None = None,
    failure_evidence: dict[str, object] | None = None,
    diagnostic_request: _SyntheticDiagnosticRequest | None = None,
) -> dict[str, object]:
    diagnostic_sink: _SyntheticDiagnosticSink | None = None
    if diagnostic_request is not None:
        _validate_diagnostic_request(diagnostic_request)
        if session_factory is not None:
            raise LiveSmokeBlocked(
                "synthetic diagnostics require the default synthetic session"
            )
    if os.getenv("RUN_LIVE_GEMINI_TESTS") != "1":
        raise LiveSmokeBlocked("RUN_LIVE_GEMINI_TESTS=1 is required for a live smoke")
    if diagnostic_request is not None:
        diagnostic_sink = _SyntheticDiagnosticSink.open(diagnostic_request)
    if secret_store is None:
        from oms_hub.security.secret_store import KeyringSecretStore

        secret_store = KeyringSecretStore()
    try:
        api_key = secret_store.get("gemini-api-key")
    except Exception:
        raise LiveSmokeBlocked("stored Gemini credential is unavailable") from None
    if not isinstance(api_key, str) or not api_key.strip():
        raise LiveSmokeBlocked("stored Gemini credential is unavailable")
    normalized_key = api_key.strip()
    if diagnostic_sink is not None:
        diagnostic_sink.add_secret(normalized_key)
    if session_factory is None:
        session: SmokeSession = GoogleGenaiSmokeSession(
            normalized_key,
            diagnostic_sink=diagnostic_sink,
        )
    else:
        session = session_factory(normalized_key)
    try:
        return await run_contract_smoke(
            session,
            failure_evidence=failure_evidence,
            diagnostic_sink=diagnostic_sink,
        )
    except BaseException as error:
        if diagnostic_sink is not None:
            diagnostic_sink.capture_exception("contract.failure", error)
        raise
    finally:
        if diagnostic_sink is not None:
            diagnostic_sink.close()


def _plan() -> dict[str, object]:
    return {
        "status": "ready_after_independent_review",
        "calls_provider": False,
        "reads_secrets": False,
        "required_flag": "RUN_LIVE_GEMINI_TESTS=1",
        "required_command": "python scripts/run-gemini-contract-smoke.py --execute-live",
        "required_authorization": (
            "Connor must explicitly authorize one synthetic Gemini smoke, disposable provider "
            "create/query/delete operations, quota/cost, and approved secret-store access."
        ),
        "required_owner_action": (
            "Independent specification and quality/security reviews must approve the exact "
            "adapter commit before run_authorized_live_smoke crosses the provider boundary."
        ),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute-live", action="store_true")
    parser.add_argument("--synthetic-diagnostic-output", type=Path)
    args = parser.parse_args(argv)
    if not args.execute_live:
        print(json.dumps(_plan(), indent=2, sort_keys=True))
        return 0
    failure_evidence: dict[str, object] = {}
    diagnostic_request = (
        _synthetic_diagnostic_request(args.synthetic_diagnostic_output)
        if args.synthetic_diagnostic_output is not None
        else None
    )
    try:
        if diagnostic_request is None:
            record = asyncio.run(
                run_authorized_live_smoke(failure_evidence=failure_evidence)
            )
        else:
            record = asyncio.run(
                run_authorized_live_smoke(
                    failure_evidence=failure_evidence,
                    diagnostic_request=diagnostic_request,
                )
            )
    except LiveSmokeBlocked as error:
        parser.error(str(error))
    except (GeminiProviderError, SmokeContractError, SmokeTemporaryFailure) as error:
        print(json.dumps(_failure_record(error, failure_evidence), indent=2, sort_keys=True))
        return 1
    print(json.dumps(record, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
