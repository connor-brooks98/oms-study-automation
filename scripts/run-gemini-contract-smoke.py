#!/usr/bin/env python3
"""Offline-tested orchestration for the explicitly authorized Task 2.8 live smoke."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
from collections.abc import AsyncIterable, Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from importlib import import_module
from io import BytesIO
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

if TYPE_CHECKING:
    from oms_hub.indexing.service import IndexResult
    from oms_hub.security.secret_store import SecretStore

SYNTHETIC_COURSE_ID = "task-2-8-synthetic-course"
SYNTHETIC_EXAM_ID = "task-2-8-synthetic-exam"
SYNTHETIC_LECTURE_ID = "task-2-8-synthetic-lecture"
SYNTHETIC_REVISION_ID = "sr_aaaaaaaaaaaaaaaaaaaaaaaaaa"
SYNTHETIC_FACT = "The Task 2.8 synthetic marker is cobalt-otter-28."
WRONG_LECTURE_ID = "task-2-8-wrong-lecture"


class SmokeContractError(RuntimeError):
    pass


class SmokeTemporaryFailure(RuntimeError):
    pass


class LiveSmokeBlocked(RuntimeError):
    pass


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
        response_schema: type[SmokeAnswer],
        omit_thinking: bool,
    ) -> SmokeQueryResult: ...

    async def list_documents(self, store_name: str) -> tuple[str, ...]: ...

    async def delete_document(self, document_name: str) -> None: ...

    async def delete_file(self, file_name: str) -> None: ...

    async def delete_store(self, store_name: str) -> None: ...


class GoogleGenaiSmokeSession:
    """Minimum live adapter for the pinned google-genai 2.14.0 contract."""

    def __init__(self, api_key: str, *, sdk_factory: SdkFactory | None = None) -> None:
        self._config = GeminiConfig(api_key=SecretStr(api_key))
        self._clients = GeminiClientFactory(self._config, sdk_factory=sdk_factory)
        self._document_name: str | None = None

    async def create_store(self, display_name: str, embedding_model: str) -> str:
        async with self._clients.client() as client:
            created = await _provider_call(
                lambda: client.file_search_stores.create(
                    config={
                        "display_name": display_name,
                        "embedding_model": embedding_model,
                    }
                )
            )
        return _provider_identity(created, "store")

    async def upload_pdf(self, display_name: str, content: bytes) -> str:
        async with self._clients.client() as client:
            uploaded = await _provider_call(
                lambda: client.files.upload(
                    file=BytesIO(content),
                    config={
                        "display_name": display_name,
                        "mime_type": "application/pdf",
                    },
                )
            )
        return _provider_identity(uploaded, "file")

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
                )
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
                    raise SmokeTemporaryFailure("Gemini import operation timed out") from None
                except GeminiProviderError:
                    raise
                except Exception as error:
                    raise translate_gemini_error(error) from None
                if bool(_field(operation, "done")):
                    break
                await asyncio.sleep(self._config.operation_poll_seconds)
        operation_error = _field(operation, "error")
        if operation_error:
            status = _field(operation_error, "code")
            raise translate_gemini_error(
                _OperationFailure(status if isinstance(status, int) else None)
            ) from None
        response = _field(operation, "response")
        self._document_name = _provider_identity(response, "document", "document_name")
        return self._document_name

    async def query(
        self,
        store_name: str,
        prompt: str,
        scope: SmokeScope,
        *,
        response_schema: type[SmokeAnswer],
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
            "response_format": {
                "type": "text",
                "mime_type": "application/json",
                "schema": response_schema.model_json_schema(),
            },
        }
        async with self._clients.client() as client:
            response = await _provider_call(
                lambda: client.interactions.create(**body)
            )
        output_text = _field(response, "output_text")
        if not isinstance(output_text, str) or not output_text:
            raise SmokeContractError("Gemini structured output was unavailable")
        try:
            answer = response_schema.model_validate_json(output_text).model_dump(mode="json")
        except ValidationError:
            raise SmokeContractError(
                "Gemini structured output did not match the required schema"
            ) from None
        usage = _field(response, "usage_metadata")
        if usage is None:
            usage = _field(response, "usage")
        return SmokeQueryResult(
            answer=answer,
            citations=_citations(response, store_name, scope, self._document_name),
            input_tokens=_optional_count(
                _field(usage, "total_input_tokens")
                or _field(usage, "prompt_token_count")
            ),
            output_tokens=_optional_count(
                _field(usage, "total_output_tokens")
                or _field(usage, "candidates_token_count")
            ),
        )

    async def list_documents(self, store_name: str) -> tuple[str, ...]:
        async with self._clients.client() as client:
            listed = await _provider_call(
                lambda: client.file_search_stores.documents.list(parent=store_name)
            )
            documents = await _collect(listed)
        return tuple(sorted(_provider_identity(item, "document") for item in documents))

    async def delete_document(self, document_name: str) -> None:
        async with self._clients.client() as client:
            await _provider_call(
                lambda: client.file_search_stores.documents.delete(
                    name=document_name,
                    config={"force": True},
                )
            )

    async def delete_file(self, file_name: str) -> None:
        async with self._clients.client() as client:
            await _provider_call(lambda: client.files.delete(name=file_name))

    async def delete_store(self, store_name: str) -> None:
        async with self._clients.client() as client:
            await _provider_call(
                lambda: client.file_search_stores.delete(
                    name=store_name,
                    config={"force": True},
                )
            )


async def _provider_call(request: Callable[[], Awaitable[Any]]) -> Any:
    try:
        return await request()
    except GeminiProviderError:
        raise
    except Exception as error:
        raise translate_gemini_error(error) from None


def _provider_identity(value: object, label: str, field: str = "name") -> str:
    identity = _field(value, field)
    if not isinstance(identity, str) or not identity.strip():
        raise SmokeContractError(f"Gemini {label} identity was unavailable")
    normalized = identity.strip()
    if len(normalized) > 500 or not normalized.isprintable():
        raise SmokeContractError(f"Gemini {label} identity was invalid")
    return normalized


def _field(value: object, name: str) -> object | None:
    if isinstance(value, Mapping):
        return value.get(name)
    try:
        return getattr(value, name, None)
    except Exception:
        return None


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


def _citations(
    response: object,
    store_name: str,
    scope: SmokeScope,
    document_name: str | None,
) -> tuple[SmokeCitation, ...]:
    del store_name
    steps = _field(response, "steps")
    if not isinstance(steps, Iterable) or isinstance(steps, (str, bytes, Mapping)):
        return ()
    found: list[SmokeCitation] = []
    expected = {
        "course_id": scope.course_id,
        "exam_id": scope.exam_id,
        "lecture_id": scope.lecture_id,
        "source_revision_id": SYNTHETIC_REVISION_ID,
    }
    for step in steps:
        if _field(step, "type") != "model_output":
            continue
        contents = _field(step, "content")
        if not isinstance(contents, Iterable) or isinstance(
            contents, (str, bytes, Mapping)
        ):
            continue
        for content in contents:
            if _field(content, "type") != "text":
                continue
            annotations = _field(content, "annotations")
            if not isinstance(annotations, Iterable) or isinstance(
                annotations, (str, bytes, Mapping)
            ):
                continue
            for annotation in annotations:
                if _field(annotation, "type") != "file_citation":
                    continue
                actual = _string_metadata(_field(annotation, "custom_metadata"))
                if any(actual.get(key) != value for key, value in expected.items()):
                    raise SmokeContractError(
                        "Gemini citation metadata did not match the requested scope"
                    )
                if document_name is None:
                    raise SmokeContractError(
                        "Gemini citation arrived before import identity was known"
                    )
                locator = _field(annotation, "document_uri") or _field(
                    annotation, "file_name"
                )
                if (
                    not isinstance(locator, str)
                    or not locator
                    or len(locator) > 500
                    or not locator.isprintable()
                ):
                    raise SmokeContractError("Gemini citation locator was unavailable")
                excerpt = _citation_excerpt(annotation, _field(content, "text"))
                found.append(
                    SmokeCitation(
                        document_name=document_name,
                        page_number=_optional_page(_field(annotation, "page_number")),
                        excerpt=excerpt,
                    )
                )
    return tuple(found)


def _string_metadata(value: object) -> dict[str, str]:
    if isinstance(value, Mapping):
        if not all(isinstance(key, str) and isinstance(text, str) for key, text in value.items()):
            raise SmokeContractError("Gemini citation metadata was invalid")
        return dict(value)
    if not isinstance(value, Iterable) or isinstance(value, (str, bytes)):
        return {}
    metadata: dict[str, str] = {}
    for item in value:
        key = _field(item, "key")
        text = _field(item, "string_value")
        if not isinstance(key, str) or not isinstance(text, str) or key in metadata:
            raise SmokeContractError("Gemini citation metadata was invalid")
        metadata[key] = text
    return metadata


def _citation_excerpt(annotation: object, content_text: object) -> str:
    source = _field(annotation, "source")
    if isinstance(source, str) and source:
        return source
    if not isinstance(content_text, str):
        raise SmokeContractError("Gemini citation excerpt was unavailable")
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
        raise SmokeContractError("Gemini citation excerpt was unavailable")
    try:
        return encoded[start:end].decode("utf-8")
    except UnicodeDecodeError:
        raise SmokeContractError("Gemini citation excerpt was invalid") from None


def _optional_page(value: object) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise SmokeContractError("Gemini citation page number was invalid")
    return value


def _optional_count(value: object) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise SmokeContractError("Gemini usage count was invalid")
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
    document_name: str | None = None
    resource_states = {
        "document": "not_started",
        "file": "not_started",
        "store": "not_started",
    }
    started = clock()
    if failure_evidence is not None:
        failure_evidence.clear()
        failure_evidence["failure_stage"] = "create_store"
    try:
        resource_states["store"] = "unknown"
        store_name = await session.create_store(
            "Study Hub Task 2.8 synthetic contract",
            "models/gemini-embedding-2",
        )
        resource_states["store"] = "confirmed"
        if failure_evidence is not None:
            failure_evidence["failure_stage"] = "upload_pdf"
        resource_states["file"] = "unknown"
        file_name = await session.upload_pdf("task-2-8-synthetic.pdf", pdf)
        resource_states["file"] = "confirmed"
        if failure_evidence is not None:
            failure_evidence["failure_stage"] = "import_file"
        resource_states["document"] = "unknown"
        operation_name = await session.import_file(store_name, file_name, metadata)
        if failure_evidence is not None:
            failure_evidence["failure_stage"] = "wait_for_import"
        document_name = await session.wait_for_import(operation_name)
        resource_states["document"] = "confirmed"
        if failure_evidence is not None:
            failure_evidence["failure_stage"] = "positive_query"
        positive = await session.query(
            store_name,
            f"Return the exact synthetic marker stated in the indexed PDF: {SYNTHETIC_FACT}",
            SmokeScope(SYNTHETIC_COURSE_ID, SYNTHETIC_EXAM_ID, SYNTHETIC_LECTURE_ID),
            response_schema=SmokeAnswer,
            omit_thinking=True,
        )
        if failure_evidence is not None:
            failure_evidence["failure_stage"] = "positive_validation"
        try:
            answer = SmokeAnswer.model_validate(positive.answer)
        except ValidationError:
            raise SmokeContractError(
                "Gemini structured output did not match the required schema"
            ) from None
        if answer.answer != SYNTHETIC_FACT or not answer.supported:
            raise SmokeContractError("structured output did not preserve the synthetic fact")
        if len(positive.citations) != 1:
            raise SmokeContractError("positive query did not return exactly one citation")
        citation = positive.citations[0]
        if (
            citation.document_name != document_name
            or citation.page_number != 1
            or SYNTHETIC_FACT not in citation.excerpt
        ):
            raise SmokeContractError("positive citation did not resolve to PDF page one")
        if failure_evidence is not None:
            failure_evidence["failure_stage"] = "negative_query"
        negative = await session.query(
            store_name,
            "Return the indexed synthetic marker.",
            SmokeScope(SYNTHETIC_COURSE_ID, SYNTHETIC_EXAM_ID, WRONG_LECTURE_ID),
            response_schema=SmokeAnswer,
            omit_thinking=True,
        )
        if failure_evidence is not None:
            failure_evidence["failure_stage"] = "negative_validation"
        if negative.citations:
            raise SmokeContractError("wrong-lecture metadata filter retrieved the document")
        if failure_evidence is not None:
            failure_evidence["failure_stage"] = "list_documents"
        if await session.list_documents(store_name) != (document_name,):
            raise SmokeContractError("document listing did not round-trip the imported document")
        duration_ms = round((clock() - started) * 1000)
        record = {
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
                "schema": type(answer).__name__,
                "validated": True,
                "answer_sha256": hashlib.sha256(answer.answer.encode("utf-8")).hexdigest(),
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
        }
    except BaseException:
        cleanup_status = "completed"
        try:
            await _cleanup(session, document_name, file_name, store_name)
        except SmokeContractError:
            cleanup_status = "failed"
        else:
            if "unknown" in resource_states.values():
                cleanup_status = "unknown"
        if failure_evidence is not None:
            failure_evidence["resources_created"] = dict(resource_states)
            failure_evidence["cleanup"] = {
                "attempted": sum(
                    value is not None for value in (document_name, file_name, store_name)
                ),
                "status": cleanup_status,
            }
        raise
    if failure_evidence is not None:
        failure_evidence["failure_stage"] = "cleanup"
    try:
        await _cleanup(session, document_name, file_name, store_name)
    except SmokeContractError:
        if failure_evidence is not None:
            failure_evidence["resources_created"] = dict(resource_states)
            failure_evidence["cleanup"] = {"attempted": 3, "status": "failed"}
        raise
    record["cleanup"] = {"attempted": 3, "status": "completed"}
    return record


async def _cleanup(
    session: SmokeSession,
    document_name: str | None,
    file_name: str | None,
    store_name: str | None,
) -> None:
    failures: list[str] = []
    for method, value in (
        (session.delete_document, document_name),
        (session.delete_file, file_name),
        (session.delete_store, store_name),
    ):
        if value is None:
            continue
        try:
            await method(value)
        except Exception as error:  # noqa: PERF203 - cleanup must attempt every resource
            failures.append(type(error).__name__)
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
    return {
        "schema_version": 1,
        "status": "failed",
        "failure_stage": evidence.get("failure_stage", "unknown"),
        "error_category": category,
        "provider_status_code": status_code,
        "retryable": retryable,
        "resources_created": evidence.get("resources_created", {}),
        "cleanup": evidence.get("cleanup", {"attempted": 0, "status": "not_started"}),
    }


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
) -> dict[str, object]:
    if os.getenv("RUN_LIVE_GEMINI_TESTS") != "1":
        raise LiveSmokeBlocked("RUN_LIVE_GEMINI_TESTS=1 is required for a live smoke")
    if secret_store is None:
        from oms_hub.security.secret_store import KeyringSecretStore

        secret_store = KeyringSecretStore()
    try:
        api_key = secret_store.get("gemini-api-key")
    except Exception:
        raise LiveSmokeBlocked("stored Gemini credential is unavailable") from None
    if not isinstance(api_key, str) or not api_key.strip():
        raise LiveSmokeBlocked("stored Gemini credential is unavailable")
    build_session = session_factory or GoogleGenaiSmokeSession
    return await run_contract_smoke(
        build_session(api_key.strip()),
        failure_evidence=failure_evidence,
    )


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
    args = parser.parse_args(argv)
    if not args.execute_live:
        print(json.dumps(_plan(), indent=2, sort_keys=True))
        return 0
    failure_evidence: dict[str, object] = {}
    try:
        record = asyncio.run(run_authorized_live_smoke(failure_evidence=failure_evidence))
    except LiveSmokeBlocked as error:
        parser.error(str(error))
    except (GeminiProviderError, SmokeContractError, SmokeTemporaryFailure) as error:
        print(json.dumps(_failure_record(error, failure_evidence), indent=2, sort_keys=True))
        return 1
    print(json.dumps(record, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
