"""Fail-closed private capture primitives for the one authorized shadow run.

The capture store is deliberately separate from ordinary rehearsal evidence.  It
contains the raw replay payloads needed by a later deterministic run; the ZIP
only receives hashes and completion metadata.
"""

from __future__ import annotations

import hashlib
import io
import ipaddress
import json
import math
import os
import stat
import subprocess
import tempfile
import threading
from collections.abc import Sequence
from contextvars import ContextVar
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import Any, cast
from urllib.parse import quote, urlsplit
from uuid import UUID

import numpy as np

from oms_hub.anki.card_centric import CardCentricLedgerAttempt
from oms_hub.anki.provider_attempts import (
    ProviderEventEvidence,
    _bounded_redacted,
    begin_provider_call,
    current_provider_attempt_identity,
    emit_provider_event,
    provider_attempt_identity_document,
    provider_replay_identity_document,
)
from oms_hub.anki.rehearsal.structured import structured_request_key_from_hashes
from oms_hub.anki.rehearsal.vectors import ReplayEmbeddingClient
from oms_hub.anki.repository import AnkiCurationRepository
from oms_hub.anki.semantic.domain import FloatMatrix, InputType
from oms_hub.anki.semantic.voyage import VoyageEmbeddingClient
from oms_hub.llm.domain import (
    DEFAULT_GENERATION_OPTIONS,
    GeneratedText,
    GenerationOptions,
    ProviderName,
)
from oms_hub.llm.structured import (
    StructuredTextGenerator,
    StructuredTextService,
    sanitize_model_text,
)
from oms_hub.security.secret_store import SecretStore


class CaptureDenied(RuntimeError):
    """The live call is outside the single operator-authorized capture."""


class CaptureIndeterminate(RuntimeError):
    """A provider response arrived but could not be retained durably."""


class CaptureAnkiCurationRepository(AnkiCurationRepository):
    """Capture-only repository that never persists raw provider response text."""

    def record_provider_attempt_event(
        self,
        evidence: ProviderEventEvidence,
        *,
        lease_owner: str | None,
        now: datetime | None = None,
    ) -> None:
        super().record_provider_attempt_event(
            replace(evidence, response_text=None), lease_owner=lease_owner, now=now
        )

    def record_card_ledger_attempt(
        self,
        job_id: UUID,
        attempt: CardCentricLedgerAttempt,
        *,
        expected_stage_attempt: int,
        lease_owner: str | None,
        now: datetime | None = None,
    ) -> None:
        super().record_card_ledger_attempt(
            job_id,
            replace(attempt, invalid_response=None),
            expected_stage_attempt=expected_stage_attempt,
            lease_owner=lease_owner,
            now=now,
        )

    def _allow_hash_only_card_ledger_failure(self) -> bool:
        return True


_HEX = frozenset("0123456789abcdef")
_AUDIT_PATH_SENTINELS = frozenset({"<invalid-raw-path>", "<invalid-canonical-path>"})
_AUDIT_QUERY_STATES = frozenset({"empty", "present", "<invalid-query-string>"})
_EVIDENCE_SECRET_MARKERS = (
    "KEY",
    "TOKEN",
    "SECRET",
    "PASSWORD",
    "COOKIE",
    "CREDENTIAL",
    "AUTHORIZATION",
)
_CAPTURE_QUERY_STAGES = frozenset({"card_prefilter", "card_residual"})
_CAPTURE_PROPOSAL_STAGES = frozenset({"dedupe"})
_CAPTURE_STRUCTURED_KINDS = {
    "card_ledger": frozenset({"primary", "repair"}),
    "card_fast_classify": frozenset({"primary"}),
    "card_classify": frozenset({"primary", "repair"}),
    "card_residual": frozenset({"primary", "repair"}),
    "card_gap_fill": frozenset({"primary"}),
}
_capture_replay_generation_options: ContextVar[GenerationOptions | None] = ContextVar(
    "capture_replay_generation_options", default=None
)


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _is_sha256(value: object) -> bool:
    return isinstance(value, str) and len(value) == 64 and set(value) <= _HEX


def _is_uuid(value: object) -> bool:
    if not isinstance(value, str):
        return False
    try:
        return str(UUID(value)) == value
    except ValueError:
        return False


def _is_audit_path(value: object) -> bool:
    return isinstance(value, str) and (value in _AUDIT_PATH_SENTINELS or value.startswith("/"))


def _canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def evidence_redact(value: Any) -> Any:
    """The one redaction transform used for ZIP records and capture audit hashes."""
    if isinstance(value, dict):
        return {
            key: (
                "[REDACTED]"
                if any(marker in key.upper() for marker in _EVIDENCE_SECRET_MARKERS)
                else evidence_redact(item)
            )
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [evidence_redact(item) for item in value]
    if isinstance(value, str):
        return _bounded_redacted(value)
    return value


def serialize_evidence_record(value: object) -> bytes:
    """Return the exact redacted bytes published for one deterministic ZIP entry."""
    return json.dumps(
        evidence_redact(value), sort_keys=True, separators=(",", ":"), default=str
    ).encode() + b"\n"


def _provider_response_sha256(response_text: str) -> str:
    """Match exactly the digest emitted by ``emit_provider_event``."""
    bounded = _bounded_redacted(response_text)
    assert bounded is not None
    return _sha256(bounded.encode())


@dataclass(frozen=True, slots=True)
class CaptureAuthorization:
    """The exact, deliberately small authorization document accepted by capture."""

    document: dict[str, Any]
    sha256: str

    @classmethod
    def load(
        cls,
        path: Path,
        expected_sha256: str,
        *,
        commit: str,
        tree: str,
        capsule_sha256: str,
        failed_job_id: str,
    ) -> CaptureAuthorization:
        if not _is_sha256(expected_sha256) or path.is_symlink() or not path.is_file():
            raise CaptureDenied("capture authorization manifest is unavailable")
        raw = path.read_bytes()
        if _sha256(raw) != expected_sha256:
            raise CaptureDenied("capture authorization manifest SHA-256 does not match")
        try:
            document = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise CaptureDenied("capture authorization manifest is invalid") from exc
        if not isinstance(document, dict):
            raise CaptureDenied("capture authorization manifest is invalid")
        required = {
            "schema_version",
            "candidate",
            "capsule_manifest_sha256",
            "phase_b8",
            "failed_job",
            "replay_namespace",
            "structured",
            "voyage",
            "egress_pins",
            "maxima",
        }
        if set(document) != required or document.get("schema_version") != 1:
            raise CaptureDenied("capture authorization manifest has an unsupported schema")
        candidate = document["candidate"]
        failed = document["failed_job"]
        phase_b8 = document["phase_b8"]
        if (
            not isinstance(candidate, dict)
            or set(candidate) != {"commit", "tree"}
            or candidate != {"commit": commit, "tree": tree}
            or document["capsule_manifest_sha256"] != capsule_sha256
            or not isinstance(failed, dict)
            or failed.get("id") != failed_job_id
            or not isinstance(phase_b8, dict)
            or set(phase_b8) != {"evidence_sha256", "lineage_sha256"}
            or not all(_is_sha256(phase_b8[key]) for key in phase_b8)
            or not isinstance(document["replay_namespace"], str)
            or not document["replay_namespace"]
        ):
            raise CaptureDenied("capture authorization identity does not match this run")
        _validate_authorized_routes(document)
        return cls(document=document, sha256=expected_sha256)

    @property
    def maxima(self) -> dict[str, int]:
        return cast(dict[str, int], self.document["maxima"])

    def allows_structured(self, provider: ProviderName, model: str, endpoint: str) -> None:
        routes = self.document["structured"]
        if not any(
            row["provider"] == provider.value
            and row["model"] == model
            and row["endpoint"] == endpoint
            for row in routes
        ):
            raise CaptureDenied("structured provider, model, or endpoint is not authorized")

    def allows_voyage(self, model: str, dimensions: int, endpoint: str, scope: str) -> None:
        voyage = self.document["voyage"]
        if (
            voyage["model"] != model
            or voyage["dimensions"] != dimensions
            or voyage["endpoint"] != endpoint
            or scope not in {"query_embedding", "proposal_embedding"}
        ):
            raise CaptureDenied(
                "Voyage provider, model, dimensions, endpoint, or scope is not authorized"
            )

    def structured_route(self, provider: ProviderName, model: str, endpoint: str) -> dict[str, int]:
        for row in self.document["structured"]:
            if (
                row["provider"] == provider.value
                and row["model"] == model
                and row["endpoint"] == endpoint
            ):
                return {
                    key: cast(int, row[key])
                    for key in (
                        "max_input_bytes",
                        "max_output_tokens",
                        "max_reserved_microusd",
                        "input_microusd_per_million",
                        "output_microusd_per_million",
                    )
                }
        raise CaptureDenied("structured provider, model, or endpoint is not authorized")

    def voyage_reservation(self) -> int:
        voyage = cast(dict[str, object], self.document["voyage"])
        return cast(int, voyage["max_reserved_microusd"])


def _validate_authorized_routes(document: dict[str, Any]) -> None:
    maxima = document["maxima"]
    maxima_keys = {
        "structured_calls",
        "embedding_batches",
        "embedding_rows",
        "embedding_input_bytes",
        "output_tokens",
        "total_reserved_microusd",
    }
    if (
        not isinstance(maxima, dict)
        or set(maxima) != maxima_keys
        or any(type(value) is not int or value < 0 for value in maxima.values())
    ):
        raise CaptureDenied("capture authorization maxima are invalid")
    structured = document["structured"]
    if not isinstance(structured, list) or not structured:
        raise CaptureDenied("capture authorization structured routes are invalid")
    seen: set[tuple[str, str, str]] = set()
    endpoint_hosts: set[str] = set()
    for row in structured:
        if not isinstance(row, dict) or set(row) != {
            "provider",
            "model",
            "endpoint",
            "max_output_tokens",
            "max_input_bytes",
            "max_reserved_microusd",
            "input_microusd_per_million",
            "output_microusd_per_million",
        }:
            raise CaptureDenied("capture authorization structured routes are invalid")
        provider, model, endpoint = row["provider"], row["model"], row["endpoint"]
        if (
            not isinstance(provider, str)
            or provider not in {item.value for item in ProviderName}
            or not isinstance(model, str)
            or not model
            or not _https_endpoint(endpoint)
            or (provider, model, endpoint) in seen
            or type(row["max_output_tokens"]) is not int
            or row["max_output_tokens"] < 0
            or type(row["max_reserved_microusd"]) is not int
            or row["max_reserved_microusd"] < 0
            or type(row["max_input_bytes"]) is not int
            or row["max_input_bytes"] < 0
            or type(row["input_microusd_per_million"]) is not int
            or row["input_microusd_per_million"] < 0
            or type(row["output_microusd_per_million"]) is not int
            or row["output_microusd_per_million"] < 0
        ):
            raise CaptureDenied("capture authorization structured routes are invalid")
        seen.add((provider, model, endpoint))
        endpoint_hosts.add(_endpoint_host(cast(str, endpoint)))
    voyage = document["voyage"]
    if (
        not isinstance(voyage, dict)
        or set(voyage) != {"model", "dimensions", "endpoint", "max_reserved_microusd"}
        or not isinstance(voyage["model"], str)
        or type(voyage["dimensions"]) is not int
        or voyage["dimensions"] < 1
        or not _https_endpoint(voyage["endpoint"])
        or type(voyage["max_reserved_microusd"]) is not int
        or voyage["max_reserved_microusd"] < 0
    ):
        raise CaptureDenied("capture authorization Voyage route is invalid")
    endpoint_hosts.add(_endpoint_host(cast(str, voyage["endpoint"])))
    pins = document["egress_pins"]
    if not isinstance(pins, dict) or not pins:
        raise CaptureDenied("capture authorization egress pins are invalid")
    if set(pins) != endpoint_hosts:
        raise CaptureDenied("capture authorization pins do not exactly close authorized endpoints")
    for host, addresses in pins.items():
        if (
            not isinstance(host, str)
            or host != host.casefold().rstrip(".")
            or host not in endpoint_hosts
            or not isinstance(addresses, list)
            or not addresses
        ):
            raise CaptureDenied("capture authorization egress pins are invalid")
        try:
            if any(
                not isinstance(address, str) or not ipaddress.ip_address(address)
                for address in addresses
            ):
                raise ValueError
        except ValueError:
            raise CaptureDenied("capture authorization egress pins are invalid") from None


def _https_endpoint(value: object) -> bool:
    if not isinstance(value, str):
        return False
    parsed = urlsplit(value)
    if not (
        parsed.scheme == "https"
        and parsed.hostname is not None
        and parsed.port in {None, 443}
        and parsed.username is None
        and parsed.password is None
        and not parsed.query
        and not parsed.fragment
    ):
        return False
    try:
        ipaddress.ip_address(parsed.hostname)
    except ValueError:
        return parsed.hostname.casefold().rstrip(".") not in {"localhost", "localhost.localdomain"}
    return False


def _endpoint_host(endpoint: str) -> str:
    host = urlsplit(endpoint).hostname
    if host is None:
        raise CaptureDenied("capture authorization endpoint is invalid")
    return host.casefold().rstrip(".")


class CaptureSecretStore:
    """Read only the manifest-selected native keyring secrets, never environment values."""

    def __init__(self, native: SecretStore, allowed: frozenset[str]) -> None:
        self._native = native
        self._allowed = allowed

    def get(self, key: str) -> str | None:
        if key not in self._allowed:
            raise CaptureDenied("capture credential key is not authorized")
        return self._native.get(key)

    def set(self, key: str, value: str) -> None:
        del key, value
        raise CaptureDenied("credential mutation is disabled during capture")

    def delete(self, key: str) -> None:
        del key
        raise CaptureDenied("credential mutation is disabled during capture")


class CaptureStore:
    """Private, fresh and durable response/vector store plus reservation ledger."""

    def __init__(self, root: Path, authorization: CaptureAuthorization) -> None:
        self.root = root
        self.authorization = authorization
        self.pack = root / "replay-supplement"
        self._ledger = root / "capture-ledger.json"
        self._server_audit = root / "capture-server-audit.json"
        self._server_audit_poison = root / "capture-server-audit.poisoned.json"
        self._lock = threading.RLock()

    def prepare(self) -> None:
        with self._lock:
            self._prepare()

    def _prepare(self) -> None:
        if self.root.exists() or self.root.is_symlink():
            raise CaptureDenied("capture store destination must be absent")
        parent = self.root.parent
        if not parent.is_dir() or parent.is_symlink():
            raise CaptureDenied("capture store parent must already exist and be direct")
        self._reject_indirect(parent)
        self.root.mkdir(mode=0o700)
        self._reject_indirect(self.root)
        if os.name == "nt":
            self._lock_windows_acl()
        elif stat.S_IMODE(self.root.stat().st_mode) != 0o700:
            raise CaptureDenied("capture store owner-only permissions cannot be proven")
        self.pack.mkdir(mode=0o700)
        (self.pack / "vectors").mkdir(mode=0o700)
        self._write_json(
            self._ledger,
            {
                "schema_version": 1,
                "authorization_sha256": self.authorization.sha256,
                "calls": [],
                "reserved_microusd": 0,
                "observed_microusd": 0,
            },
        )

    def verify_prepared(self) -> None:
        with self._lock:
            if not self.root.is_dir() or _is_indirect(self.root):
                raise CaptureDenied("prepared capture store is unavailable or indirect")
            self._reject_indirect(self.root)
            if os.name == "nt":
                owner = _windows_current_principal()
                completed = subprocess.run(
                    ["icacls", str(self.root)],
                    check=False,
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
                if completed.returncode != 0 or not _windows_acl_is_owner_only(
                    completed.stdout, owner, self.root
                ):
                    raise CaptureDenied("prepared capture store owner-only ACL cannot be proven")
            else:
                self._verify_posix_private_topology()
            self._read_ledger()

    def _verify_posix_private_topology(self) -> None:
        owner = getattr(os, "geteuid", lambda: None)()
        self._verify_posix_private_directory(self.root, owner)
        self._verify_posix_private_directory(self.pack, owner)
        self._verify_posix_private_directory(self.pack / "vectors", owner)
        self._verify_posix_private_file(self._ledger, owner)
        for path in (self._server_audit, self._server_audit_poison):
            if path.exists():
                self._verify_posix_private_file(path, owner)

    def _verify_posix_private_directory(self, path: Path, owner: int | None) -> None:
        if not path.is_dir() or _is_indirect(path):
            raise CaptureDenied(
                "prepared capture store private directory is unavailable or indirect"
            )
        self._reject_indirect(path)
        metadata = path.stat()
        if stat.S_IMODE(metadata.st_mode) != 0o700 or (
            owner is not None and metadata.st_uid != owner
        ):
            raise CaptureDenied(
                "prepared capture store private directory permissions cannot be proven"
            )
        try:
            path.relative_to(self.root)
        except ValueError as exc:
            raise CaptureDenied(
                "prepared capture store directory escaped its private root"
            ) from exc

    def _verify_posix_private_file(self, path: Path, owner: int | None) -> None:
        if not path.is_file() or _is_indirect(path):
            raise CaptureDenied("prepared capture store private file is unavailable or indirect")
        self._reject_indirect(path)
        metadata = path.stat()
        if stat.S_IMODE(metadata.st_mode) != 0o600 or (
            owner is not None and metadata.st_uid != owner
        ):
            raise CaptureDenied("prepared capture store private file permissions cannot be proven")
        try:
            path.relative_to(self.root)
        except ValueError as exc:
            raise CaptureDenied("prepared capture store file escaped its private root") from exc

    def initialize_server_audit(self, capability: str) -> str:
        """Bind the child-only control-plane capability without retaining it."""
        if len(capability) < 32:
            raise CaptureDenied("capture control-plane capability is unavailable")
        with self._lock:
            if self._server_audit.exists():
                raise CaptureDenied("capture server audit is already initialized")
            digest = _sha256(capability.encode("utf-8"))
            self._write_json(
                self._server_audit,
                {
                    "schema_version": 1,
                    "authorization_sha256": self.authorization.sha256,
                    "capability_sha256": digest,
                    "entries": [],
                },
            )
            return digest

    def record_server_request(
        self,
        *,
        method: str,
        raw_path: str,
        canonical_path: str,
        authenticated: bool,
        allowed: bool,
        status: int,
        job_id: str | None,
        query_state: str,
    ) -> None:
        """Durably append one authoritative inbound capture request observation."""
        if (
            not method
            or not _is_audit_path(raw_path)
            or not _is_audit_path(canonical_path)
            or type(authenticated) is not bool
            or type(allowed) is not bool
            or type(status) is not int
            or status < 100
            or status > 599
            or (job_id is not None and not _is_uuid(job_id))
            or query_state not in _AUDIT_QUERY_STATES
        ):
            raise CaptureDenied("capture server audit entry is invalid")
        with self._lock:
            audit = self._read_server_audit()
            entries = audit["entries"]
            entries.append(
                {
                    "ordinal": len(entries) + 1,
                    "method": method,
                    "raw_path": raw_path,
                    "canonical_path": canonical_path,
                    "authenticated": authenticated,
                    "allowed": allowed,
                    "status": status,
                    "job_id": job_id,
                    "query_state": query_state,
                }
            )
            self._write_json(self._server_audit, audit)

    def poison_server_audit(self) -> None:
        """Make capture publication impossible after any failed audit append."""
        with self._lock:
            if self._server_audit_poison.exists():
                self._read_json(self._server_audit_poison, None)
                return
            self._write_json(
                self._server_audit_poison,
                {
                    "schema_version": 1,
                    "authorization_sha256": self.authorization.sha256,
                    "poisoned": True,
                },
            )

    def server_audit(self) -> dict[str, object]:
        with self._lock:
            return cast(dict[str, object], self._read_server_audit())

    def server_audit_sha256(self) -> str:
        with self._lock:
            return _sha256(serialize_evidence_record(self._server_audit_evidence_projection()))

    def server_audit_evidence_projection(self) -> dict[str, object]:
        with self._lock:
            return self._server_audit_evidence_projection()

    def _server_audit_evidence_projection(self) -> dict[str, object]:
        audit = self._read_server_audit()
        entries = audit["entries"]
        return {
            "schema_version": 1,
            "capture_binding_sha256": audit["authorization_sha256"],
            "capability_digest_sha256": audit["capability_sha256"],
            "requests": entries,
        }

    def reserve(
        self,
        *,
        kind: str,
        rows: int = 0,
        input_bytes: int = 0,
        output_tokens: int = 0,
        cost_microusd: int = 0,
        provider: str = "test",
        model: str = "test",
        request_sha256: str = "0" * 64,
        replay_identity: dict[str, object] | None = None,
        replay_request: dict[str, object] | None = None,
    ) -> int:
        with self._lock:
            return self._reserve(
                kind=kind,
                rows=rows,
                input_bytes=input_bytes,
                output_tokens=output_tokens,
                cost_microusd=cost_microusd,
                provider=provider,
                model=model,
                request_sha256=request_sha256,
                replay_identity=replay_identity,
                replay_request=replay_request,
            )

    def _reserve(
        self,
        *,
        kind: str,
        rows: int,
        input_bytes: int,
        output_tokens: int,
        cost_microusd: int,
        provider: str,
        model: str,
        request_sha256: str,
        replay_identity: dict[str, object] | None,
        replay_request: dict[str, object] | None,
    ) -> int:
        if (
            not _is_sha256(request_sha256)
            or not provider
            or not model
            or replay_identity is None
        ):
            raise CaptureDenied("capture reservation is missing a stable provider identity")
        ledger = self._read_ledger()
        maxima = self.authorization.maxima
        calls = ledger["calls"]
        structured = sum(call["kind"] == "structured" for call in calls)
        embedding = sum(call["kind"] != "structured" for call in calls)
        if (
            (kind == "structured" and structured + 1 > maxima["structured_calls"])
            or (kind != "structured" and embedding + 1 > maxima["embedding_batches"])
            or sum(call["rows"] for call in calls) + rows > maxima["embedding_rows"]
            or sum(call["input_bytes"] for call in calls) + input_bytes
            > maxima["embedding_input_bytes"]
            or sum(call["output_tokens"] for call in calls) + output_tokens
            > maxima["output_tokens"]
            or ledger["reserved_microusd"] + cost_microusd > maxima["total_reserved_microusd"]
        ):
            raise CaptureDenied("capture pre-dispatch budget would be exceeded")
        ordinal = len(calls) + 1
        calls.append(
            {
                "ordinal": ordinal,
                "kind": kind,
                "rows": rows,
                "input_bytes": input_bytes,
                "output_tokens": output_tokens,
                "reserved_microusd": cost_microusd,
                "observed_microusd": None,
                "stored": False,
                "provider": provider,
                "model": model,
                "request_sha256": request_sha256,
                "replay_identity": replay_identity,
                "replay_request": replay_request,
                "response_sha256": None,
                "private_response": None,
            }
        )
        ledger["reserved_microusd"] += cost_microusd
        self._write_json(self._ledger, ledger)
        return ordinal

    def complete(self, ordinal: int, *, observed_microusd: int, stored: bool) -> None:
        with self._lock:
            self._complete(ordinal, observed_microusd=observed_microusd, stored=stored)

    def _complete(self, ordinal: int, *, observed_microusd: int, stored: bool) -> None:
        ledger = self._read_ledger()
        call = _call_at(ledger, ordinal)
        if observed_microusd < 0 or observed_microusd > call["reserved_microusd"]:
            raise CaptureDenied("capture observed cost exceeds its reservation")
        call["observed_microusd"] = observed_microusd
        call["stored"] = stored
        ledger["observed_microusd"] = sum(
            value["observed_microusd"] or 0 for value in ledger["calls"]
        )
        self._write_json(self._ledger, ledger)

    def bind_private_response(
        self, ordinal: int, response_sha256: str, private_response: dict[str, object]
    ) -> None:
        with self._lock:
            if not _is_sha256(response_sha256):
                raise CaptureDenied("capture response digest is invalid")
            ledger = self._read_ledger()
            call = _call_at(ledger, ordinal)
            if (
                call["response_sha256"] is not None
                or call["private_response"] is not None
                or (
                    call.get("replay_request") is not None
                    and call.get("replay_request") != private_response
                )
            ):
                raise CaptureDenied("capture response digest is already bound")
            if call.get("replay_request") is None:
                call["replay_request"] = private_response
            call["response_sha256"] = response_sha256
            call["private_response"] = private_response
            self._write_json(self._ledger, ledger)

    def private_response_matches(
        self, call: dict[str, Any], response_event: dict[str, object]
    ) -> bool:
        with self._lock:
            response_sha256 = response_event.get("response_sha256")
            if not isinstance(response_sha256, str):
                return False
            if call.get("response_sha256") != response_sha256:
                return False
            reference = call.get("private_response")
            if not isinstance(reference, dict) or call.get("replay_request") != reference:
                return False
            kind = reference.get("kind")
            if kind == "structured":
                key = reference.get("key")
                required_request = {
                    "kind",
                    "key",
                    "provider",
                    "model",
                    "instruction_sha256",
                    "input_sha256",
                    "output_schema_sha256",
                    "cache_prefix_sha256",
                    "generation_parameters",
                    "replay_identity",
                }
                if (
                    set(reference) != required_request
                    or reference.get("provider") != call.get("provider")
                    or reference.get("model") != call.get("model")
                    or not isinstance(key, str)
                    or any(
                        not _is_sha256(reference.get(field))
                        for field in ("instruction_sha256", "input_sha256", "output_schema_sha256")
                    )
                    or (
                        reference.get("cache_prefix_sha256") is not None
                        and not _is_sha256(reference.get("cache_prefix_sha256"))
                    )
                    or not isinstance(reference.get("generation_parameters"), dict)
                    or not isinstance(reference.get("replay_identity"), dict)
                ):
                    return False
                expected_key = structured_request_key_from_hashes(
                    provider=cast(str, reference["provider"]),
                    model=cast(str, reference["model"]),
                    instruction_sha256=cast(str, reference["instruction_sha256"]),
                    input_sha256=cast(str, reference["input_sha256"]),
                    output_schema_sha256=cast(str, reference["output_schema_sha256"]),
                    cache_prefix_sha256=cast(str | None, reference["cache_prefix_sha256"]),
                    generation_parameters=cast(
                        dict[str, object], reference["generation_parameters"]
                    ),
                    attempt_identity=cast(dict[str, object], reference["replay_identity"]),
                )
                if (
                    key != expected_key
                    or call.get("request_sha256") != response_event.get("request_sha256")
                    or any(
                        response_event.get(field) != reference.get(field)
                        for field in (
                            "provider",
                            "model",
                            "instruction_sha256",
                            "input_sha256",
                            "output_schema_sha256",
                            "cache_prefix_sha256",
                        )
                    )
                ):
                    return False
                original_parameters = reference["generation_parameters"]
                actual_parameters = response_event.get("generation_parameters")
                if not isinstance(actual_parameters, dict):
                    return False
                parameter_fields = {
                    "thinking",
                    "thinking_budget_tokens",
                    "temperature",
                    "max_tokens",
                }
                if (
                    set(original_parameters) != parameter_fields
                    or set(actual_parameters) != parameter_fields
                    or any(
                        actual_parameters.get(field) != original_parameters.get(field)
                        for field in parameter_fields - {"max_tokens"}
                    )
                ):
                    return False
                original_max_tokens = original_parameters.get("max_tokens")
                actual_max_tokens = actual_parameters.get("max_tokens")
                if original_max_tokens is None:
                    routes = [
                        route
                        for route in self.authorization.document["structured"]
                        if route["provider"] == reference["provider"]
                        and route["model"] == reference["model"]
                    ]
                    if len(routes) != 1 or actual_max_tokens != routes[0]["max_output_tokens"]:
                        return False
                elif actual_max_tokens != original_max_tokens:
                    return False
                records = self._read_json(self.pack / "structured.json", {})
                record = (
                    records.get(key)
                    if isinstance(key, str) and isinstance(records, dict)
                    else None
                )
                required = {
                    "text",
                    "text_sha256",
                    "provider",
                    "model",
                    "request_id",
                    "input_tokens",
                    "output_tokens",
                    "cost_microusd",
                    "cache_creation_input_tokens",
                    "cache_read_input_tokens",
                }
                if not isinstance(record, dict) or set(record) != required:
                    return False
                text = record.get("text")
                if (
                    not isinstance(text, str)
                    or record.get("text_sha256") != _sha256(text.encode())
                    or record.get("provider") != call.get("provider")
                    or record.get("model") != call.get("model")
                    or not isinstance(record.get("request_id"), str)
                    or any(
                        type(record.get(field)) is not int or record[field] < 0
                        for field in (
                            "input_tokens",
                            "output_tokens",
                            "cost_microusd",
                            "cache_creation_input_tokens",
                            "cache_read_input_tokens",
                        )
                    )
                ):
                    return False
                if any(
                    record.get(key) != response_event.get(key)
                    for key in (
                        "request_id",
                        "input_tokens",
                        "output_tokens",
                        "cost_microusd",
                        "cache_creation_input_tokens",
                        "cache_read_input_tokens",
                    )
                ):
                    return False
                return _provider_response_sha256(sanitize_model_text(text)) == response_sha256
            if kind == "vectors":
                keys = reference.get("keys")
                input_type = reference.get("input_type")
                dimensions = reference.get("dimensions")
                text_sha256 = reference.get("text_sha256")
                normalized_texts = reference.get("normalized_texts")
                manifest = self._read_json(self.pack / "vectors" / "manifest.json", {})
                if (
                    set(reference)
                    != {
                        "kind",
                        "normalized_texts",
                        "keys",
                        "text_sha256",
                        "input_type",
                        "dimensions",
                        "provider_input_sha256",
                        "provider_generation_parameters_sha256",
                    }
                    or not isinstance(keys, list)
                    or not keys
                    or not all(isinstance(key, str) for key in keys)
                    or len(set(keys)) != len(keys)
                    or type(dimensions) is not int
                    or dimensions < 1
                    or input_type not in {"query", "document"}
                    or not isinstance(text_sha256, list)
                    or len(text_sha256) != len(keys)
                    or not all(_is_sha256(value) for value in text_sha256)
                    or not isinstance(normalized_texts, list)
                    or len(normalized_texts) != len(keys)
                    or not all(
                        isinstance(value, str) and value == " ".join(value.split())
                        for value in normalized_texts
                    )
                    or call.get("model") != self.authorization.document["voyage"]["model"]
                    or not isinstance(manifest, dict)
                    or response_event.get("input_sha256") != reference.get("provider_input_sha256")
                    or response_event.get("generation_parameters_sha256")
                    != reference.get("provider_generation_parameters_sha256")
                ):
                    return False
                expected_hashes = [_sha256(value.encode()) for value in normalized_texts]
                replay = ReplayEmbeddingClient(
                    self.pack / "vectors",
                    model=cast(str, call["model"]),
                    dimensions=dimensions,
                )
                expected_keys = [
                    replay._key(value, input_type) for value in normalized_texts  # noqa: SLF001
                ]
                expected_input_sha256 = _sha256(
                    json.dumps(expected_hashes, separators=(",", ":")).encode()
                )
                expected_generation_sha256 = _sha256(
                    _canonical({"input_type": input_type, "row_count": len(keys)})
                )
                if (
                    text_sha256 != expected_hashes
                    or keys != expected_keys
                    or reference.get("provider_input_sha256") != expected_input_sha256
                    or reference.get("provider_generation_parameters_sha256")
                    != expected_generation_sha256
                    or call.get("kind")
                    != (
                        "query_embedding" if input_type == "query" else "proposal_embedding"
                    )
                    or call.get("rows") != len(keys)
                ):
                    return False
                row_hashes: list[str] = []
                for key, normalized_hash in zip(keys, text_sha256, strict=True):
                    record = manifest.get(key)
                    required = {
                        "path",
                        "sha256",
                        "input_type",
                        "text_sha256",
                        "size_bytes",
                        "dtype",
                        "dimensions",
                    }
                    if not isinstance(record, dict) or set(record) != required:
                        return False
                    relative = record.get("path")
                    if (
                        not isinstance(relative, str)
                        or relative != f"{input_type}/{key}.npy"
                        or not _is_sha256(record.get("sha256"))
                        or not _is_sha256(record.get("text_sha256"))
                        or record.get("input_type") != input_type
                        or record.get("text_sha256") != normalized_hash
                        or record.get("dimensions") != dimensions
                        or type(record.get("size_bytes")) is not int
                        or record["size_bytes"] < 0
                        or not isinstance(record.get("dtype"), str)
                        or not record["dtype"]
                    ):
                        return False
                    path = self.pack / "vectors" / relative
                    self._reject_indirect(path)
                    try:
                        content = path.read_bytes()
                        vector = np.load(path, allow_pickle=False)
                    except (OSError, ValueError):
                        return False
                    if (
                        not isinstance(vector, np.ndarray)
                        or len(content) != record["size_bytes"]
                        or _sha256(content) != record["sha256"]
                        or vector.dtype.name != record["dtype"]
                        or vector.shape != (dimensions,)
                        or not np.isfinite(vector).all()
                    ):
                        return False
                    row_hashes.append(_sha256(np.asarray(vector, dtype=np.float32).tobytes()))
                response = json.dumps(row_hashes, separators=(",", ":"))
                return _provider_response_sha256(response) == response_sha256
            return False

    def record_structured(self, key: str, generated: GeneratedText) -> None:
        with self._lock:
            self._record_structured(key, generated)

    def _record_structured(self, key: str, generated: GeneratedText) -> None:
        path = self.pack / "structured.json"
        records = self._read_json(path, {})
        record = {
            "text": generated.text,
            "text_sha256": _sha256(generated.text.encode()),
            "provider": generated.provider.value,
            "model": generated.model,
            "request_id": generated.request_id,
            "input_tokens": generated.input_tokens,
            "output_tokens": generated.output_tokens,
            "cost_microusd": generated.cost_microusd,
            "cache_creation_input_tokens": generated.cache_creation_input_tokens,
            "cache_read_input_tokens": generated.cache_read_input_tokens,
        }
        existing = records.get(key)
        if existing is not None and existing != record:
            raise CaptureDenied("structured replay identity received a different response")
        records[key] = record
        self._write_json(path, records)

    def record_vectors(
        self,
        texts: list[str],
        vectors: FloatMatrix,
        *,
        model: str,
        dimensions: int,
        input_type: InputType,
    ) -> list[str]:
        with self._lock:
            return self._record_vectors(
                texts, vectors, model=model, dimensions=dimensions, input_type=input_type
            )

    def _record_vectors(
        self,
        texts: list[str],
        vectors: FloatMatrix,
        *,
        model: str,
        dimensions: int,
        input_type: InputType,
    ) -> list[str]:
        matrix = np.asarray(vectors, dtype=np.float32)
        if matrix.shape != (len(texts), dimensions) or not np.isfinite(matrix).all():
            raise CaptureDenied("capture vectors violate the replay contract")
        replay = ReplayEmbeddingClient(self.pack / "vectors", model=model, dimensions=dimensions)
        manifest = self._read_json(self.pack / "vectors" / "manifest.json", {})
        keys: list[str] = []
        for index, text in enumerate(texts):
            key = replay._key(text, input_type)  # noqa: SLF001 - published replay identity
            relative = f"{input_type}/{key}.npy"
            destination = replay.root / relative
            destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            buffer = io.BytesIO()
            np.save(buffer, matrix[index], allow_pickle=False)
            raw = buffer.getvalue()
            if destination.exists() and destination.read_bytes() != raw:
                raise CaptureDenied("replay vector identity was reused with different bytes")
            if not destination.exists():
                self._write_bytes(destination, raw)
            record = {
                "path": relative,
                "sha256": _sha256(raw),
                "input_type": input_type,
                "text_sha256": _sha256(" ".join(text.split()).encode()),
                "size_bytes": len(raw),
                "dtype": matrix[index].dtype.name,
                "dimensions": dimensions,
            }
            if key in manifest and manifest[key] != record:
                raise CaptureDenied("replay vector manifest identity changed")
            manifest[key] = record
            keys.append(key)
        self._write_json(replay.root / "manifest.json", manifest)
        return keys

    def finalize_pack(self) -> dict[str, str]:
        with self._lock:
            return self._finalize_pack()

    def _finalize_pack(self) -> dict[str, str]:
        manifest, result = self.build_pack_manifest()
        self.publish_pack_manifest(manifest)
        return result

    def build_pack_manifest(self) -> tuple[dict[str, object], dict[str, str]]:
        ledger = self._read_ledger()
        if not ledger["calls"] or any(
            call["observed_microusd"] is None or not call["stored"] for call in ledger["calls"]
        ):
            raise CaptureDenied("capture has an incomplete or unstored provider lifecycle")
        structured = self.pack / "structured.json"
        vectors = self.pack / "vectors" / "manifest.json"
        if not structured.is_file() or not vectors.is_file():
            raise CaptureDenied("capture replay pack is incomplete")
        files = []
        for path in sorted(self.pack.rglob("*")):
            self._reject_indirect(path)
            if path.is_file() and path.name != "replay-supplement.json":
                raw = path.read_bytes()
                files.append(
                    {
                        "path": path.relative_to(self.pack).as_posix(),
                        "bytes": len(raw),
                        "sha256": _sha256(raw),
                    }
                )
        manifest: dict[str, object] = {
            "schema_version": 1,
            "manifest_rule": "self-excluding",
            "files": files,
        }
        return manifest, {
            "pack_manifest_sha256": _sha256(_canonical(manifest) + b"\n"),
            "ledger_sha256": _sha256(self._ledger.read_bytes()),
        }

    def publish_pack_manifest(self, manifest: dict[str, object]) -> None:
        with self._lock:
            if (self.pack / "replay-supplement.json").exists():
                raise CaptureDenied("capture replay supplement is already published")
            self._write_json(self.pack / "replay-supplement.json", manifest)

    def calls(self) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(call) for call in self._read_ledger()["calls"]]

    def write_completion(self, value: dict[str, object]) -> Path:
        with self._lock:
            return self._write_completion(value)

    def write_lineage(self, value: dict[str, object]) -> None:
        with self._lock:
            destination = self.pack / "capture-lineage.json"
            if destination.exists():
                raise CaptureDenied("capture replay lineage is already published")
            self._write_json(destination, value)

    def _write_completion(self, value: dict[str, object]) -> Path:
        destination = self.root / "capture-completion.json"
        if destination.exists():
            raise CaptureDenied("capture completion manifest already exists")
        self._write_json(destination, value)
        return destination

    def _read_ledger(self) -> dict[str, Any]:
        ledger = self._read_json(self._ledger, None)
        if (
            not isinstance(ledger, dict)
            or ledger.get("authorization_sha256") != self.authorization.sha256
        ):
            raise CaptureDenied("capture reservation ledger is invalid")
        return ledger

    def _read_server_audit(self) -> dict[str, Any]:
        if self._server_audit_poison.exists():
            raise CaptureDenied("capture server audit is poisoned")
        audit = self._read_json(self._server_audit, None)
        if (
            not isinstance(audit, dict)
            or set(audit)
            != {"schema_version", "authorization_sha256", "capability_sha256", "entries"}
            or audit.get("schema_version") != 1
            or audit.get("authorization_sha256") != self.authorization.sha256
            or not _is_sha256(audit.get("capability_sha256"))
            or not isinstance(audit.get("entries"), list)
        ):
            raise CaptureDenied("capture server audit is invalid")
        for ordinal, entry in enumerate(audit["entries"], 1):
            if (
                not isinstance(entry, dict)
                or set(entry)
                != {
                    "ordinal",
                    "method",
                    "raw_path",
                    "canonical_path",
                    "authenticated",
                    "allowed",
                    "status",
                    "job_id",
                    "query_state",
                }
                or entry.get("ordinal") != ordinal
                or not isinstance(entry.get("method"), str)
                or not _is_audit_path(entry.get("raw_path"))
                or not _is_audit_path(entry.get("canonical_path"))
                or type(entry.get("authenticated")) is not bool
                or type(entry.get("allowed")) is not bool
                or type(entry.get("status")) is not int
                or entry.get("query_state") not in _AUDIT_QUERY_STATES
                or (
                    entry.get("job_id") is not None
                    and not _is_uuid(entry.get("job_id"))
                )
            ):
                raise CaptureDenied("capture server audit is invalid")
        return cast(dict[str, Any], audit)

    def _read_json(self, path: Path, default: Any) -> Any:
        if not path.exists():
            return default
        self._reject_indirect(path)
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise CaptureDenied("capture private store is malformed") from exc

    def _write_json(self, path: Path, value: object) -> None:
        self._reject_indirect(path.parent)
        payload = _canonical(value) + b"\n"
        descriptor, temporary_name = tempfile.mkstemp(prefix=".capture-", dir=path.parent)
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            _replace_durably(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)

    def _write_bytes(self, path: Path, payload: bytes) -> None:
        self._reject_indirect(path.parent)
        descriptor, temporary_name = tempfile.mkstemp(prefix=".capture-", dir=path.parent)
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            _replace_durably(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)

    def _reject_indirect(self, path: Path) -> None:
        current = path.absolute()
        chain = [current, *current.parents]
        if any(_is_indirect(item) for item in chain if item.exists()):
            raise CaptureDenied("capture private store contains a symlink or reparse point")

    def _lock_windows_acl(self) -> None:
        owner = _windows_current_principal()
        try:
            completed = subprocess.run(
                ["icacls", str(self.root), "/inheritance:r"],
                check=False,
                capture_output=True,
                text=True,
                timeout=10,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise CaptureDenied("capture store Windows owner-only ACL cannot be created") from exc
        if completed.returncode != 0:
            raise CaptureDenied("capture store Windows owner-only ACL cannot be created")
        current = subprocess.run(
            ["icacls", str(self.root)], check=False, capture_output=True, text=True, timeout=10
        )
        principals = (
            _windows_acl_principals(current.stdout, self.root)
            if current.returncode == 0
            else None
        )
        if principals is None:
            raise CaptureDenied("capture store Windows owner-only ACL cannot be proven")
        for principal in sorted(
            {value for value in principals if value.casefold() != owner.casefold()},
            key=str.casefold,
        ):
            removed = subprocess.run(
                ["icacls", str(self.root), "/remove", principal],
                check=False,
                capture_output=True,
                text=True,
                timeout=10,
            )
            if removed.returncode != 0:
                raise CaptureDenied("capture store Windows owner-only ACL cannot be created")
        granted = subprocess.run(
            ["icacls", str(self.root), "/grant:r", f"{owner}:(OI)(CI)F"],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
        if granted.returncode != 0:
            raise CaptureDenied("capture store Windows owner-only ACL cannot be created")
        verified = subprocess.run(
            ["icacls", str(self.root)], check=False, capture_output=True, text=True, timeout=10
        )
        if verified.returncode != 0 or not _windows_acl_is_owner_only(
            verified.stdout, owner, self.root
        ):
            raise CaptureDenied("capture store Windows owner-only ACL cannot be proven")


def _call_at(ledger: dict[str, Any], ordinal: int) -> dict[str, Any]:
    calls = ledger.get("calls")
    if (
        not isinstance(calls, list)
        or not 1 <= ordinal <= len(calls)
        or not isinstance(calls[ordinal - 1], dict)
    ):
        raise CaptureDenied("capture reservation ledger is invalid")
    return cast(dict[str, Any], calls[ordinal - 1])


def _fsync_dir(path: Path) -> None:
    if os.name == "nt":
        return
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _is_indirect(path: Path) -> bool:
    if path.is_symlink():
        return True
    if os.name != "nt":
        return False
    attributes = getattr(os.lstat(path), "st_file_attributes", 0)
    return bool(attributes & 0x0400)  # FILE_ATTRIBUTE_REPARSE_POINT


def _replace_durably(source: Path, destination: Path) -> None:
    if os.name != "nt":
        os.replace(source, destination)
        _fsync_dir(destination.parent)
        return
    _move_file_ex_write_through(source, destination)


def _move_file_ex_write_through(source: Path, destination: Path) -> None:
    import ctypes

    flags = 0x1 | 0x8  # MOVEFILE_REPLACE_EXISTING | MOVEFILE_WRITE_THROUGH
    result = ctypes.windll.kernel32.MoveFileExW(str(source), str(destination), flags)
    if not result:
        raise CaptureDenied(
            f"capture atomic write-through replacement failed: {ctypes.get_last_error()}"
        )


def _windows_current_principal() -> str:
    try:
        completed = subprocess.run(
            ["whoami"], check=False, capture_output=True, text=True, timeout=10
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise CaptureDenied("capture store Windows owner cannot be determined") from exc
    principal = completed.stdout.strip()
    if completed.returncode != 0 or not principal:
        raise CaptureDenied("capture store Windows owner cannot be determined")
    return principal


def _windows_acl_is_owner_only(output: str, owner: str, root: Path | None = None) -> bool:
    owner_key = owner.casefold()
    entries = _windows_acl_entries(output, root)
    if not entries:
        return False
    for principal, permissions in entries:
        if principal.casefold() != owner_key:
            return False
        normalized = permissions.casefold().replace(" ", "")
        if "(i)" in normalized or "(f)" not in normalized:
            return False
    return True


def _windows_acl_principals(output: str, root: Path | None = None) -> tuple[str, ...] | None:
    entries = _windows_acl_entries(output, root)
    if entries is None:
        return None
    return tuple(principal for principal, _permissions in entries)


def _windows_acl_entries(
    output: str, root: Path | None = None
) -> tuple[tuple[str, str], ...] | None:
    root_text = str(root) if root is not None else ""
    entries: list[tuple[str, str]] = []
    for raw in output.splitlines():
        line = raw.strip()
        if not line or line.casefold().startswith("successfully processed"):
            continue
        if root_text and line.casefold().startswith(root_text.casefold()):
            line = line[len(root_text) :].strip()
        if not line or ":" not in line:
            return None
        principal, permissions = line.split(":", 1)
        principal = principal.strip()
        if not principal or not permissions.strip():
            return None
        entries.append((principal, permissions))
    return tuple(entries) if entries else None


class CaptureStructuredTextGenerator(StructuredTextGenerator):
    def __init__(
        self,
        inner: StructuredTextGenerator,
        store: CaptureStore,
        endpoints: dict[ProviderName, str],
    ) -> None:
        self.inner = inner
        self.store = store
        self.endpoints = endpoints

    def generate_text(
        self,
        instruction: str,
        input_text: str,
        *,
        output_schema: dict[str, object],
        provider: ProviderName,
        model: str,
        options: GenerationOptions = DEFAULT_GENERATION_OPTIONS,
    ) -> GeneratedText:
        endpoint = self.endpoints.get(provider)
        if endpoint is None:
            raise CaptureDenied("structured provider endpoint is unavailable")
        if provider is ProviderName.GEMINI:
            endpoint = f"{endpoint}/{quote(model, safe='')}:generateContent"
        self.store.authorization.allows_structured(provider, model, endpoint)
        identity = current_provider_attempt_identity()
        if identity is None:
            raise CaptureDenied("structured capture requires a durable provider attempt identity")
        if identity.replay_namespace != self.store.authorization.document["replay_namespace"]:
            raise CaptureDenied("structured capture replay namespace is not authorized")
        if identity.kind not in _CAPTURE_STRUCTURED_KINDS.get(identity.stage.value, frozenset()):
            raise CaptureDenied("structured capture stage or call kind is not authorized")
        route = self.store.authorization.structured_route(provider, model, endpoint)
        if options.max_tokens is None or options.max_tokens > route["max_output_tokens"]:
            raise CaptureDenied("structured output-token authorization would be exceeded")
        input_bytes = sum(
            len(value.encode())
            for value in (
                instruction,
                input_text,
                _canonical(output_schema).decode(),
                options.cacheable_source_prefix or "",
            )
        )
        if input_bytes > route["max_input_bytes"]:
            raise CaptureDenied("structured input-byte authorization would be exceeded")
        reserved_tokens = options.max_tokens
        reserved_cost = math.ceil(
            input_bytes * route["input_microusd_per_million"] / 1_000_000
        ) + math.ceil(reserved_tokens * route["output_microusd_per_million"] / 1_000_000)
        if reserved_cost > route["max_reserved_microusd"]:
            raise CaptureDenied("structured cost authorization would be exceeded")
        replay_options = _capture_replay_generation_options.get() or options
        replay_parameters: dict[str, object] = {
            "thinking": replay_options.thinking.value,
            "thinking_budget_tokens": replay_options.thinking_budget_tokens,
            "temperature": replay_options.temperature,
            "max_tokens": replay_options.max_tokens,
        }
        replay_identity = provider_replay_identity_document(identity)
        replay_request: dict[str, object] = {
            "kind": "structured",
            "provider": provider.value,
            "model": model,
            "instruction_sha256": _sha256(instruction.encode()),
            "input_sha256": _sha256(input_text.encode()),
            "output_schema_sha256": _sha256(_canonical(output_schema)),
            "cache_prefix_sha256": (
                _sha256(replay_options.cacheable_source_prefix.encode())
                if replay_options.cacheable_source_prefix is not None
                else None
            ),
            "generation_parameters": replay_parameters,
            "replay_identity": replay_identity,
        }
        replay_key = structured_request_key_from_hashes(
            provider=provider.value,
            model=model,
            instruction_sha256=cast(str, replay_request["instruction_sha256"]),
            input_sha256=cast(str, replay_request["input_sha256"]),
            output_schema_sha256=cast(str, replay_request["output_schema_sha256"]),
            cache_prefix_sha256=cast(str | None, replay_request["cache_prefix_sha256"]),
            generation_parameters=replay_parameters,
            attempt_identity=replay_identity,
        )
        replay_request["key"] = replay_key
        ordinal = self.store.reserve(
            kind="structured",
            output_tokens=reserved_tokens,
            cost_microusd=reserved_cost,
            provider=provider.value,
            model=model,
            request_sha256=_structured_request_sha256(
                provider.value, model, instruction, input_text, output_schema, options, identity
            ),
            replay_identity=provider_replay_identity_document(identity),
            replay_request=replay_request,
        )
        generated = self.inner.generate_text(
            instruction,
            input_text,
            output_schema=output_schema,
            provider=provider,
            model=model,
            options=options,
        )
        try:
            if (
                generated.provider is not provider
                or generated.model != model
                or generated.output_tokens > reserved_tokens
            ):
                raise CaptureDenied("captured structured response violates authorization")
            self.store.record_structured(replay_key, generated)
            self.store.bind_private_response(
                ordinal,
                _provider_response_sha256(sanitize_model_text(generated.text)),
                replay_request,
            )
            self.store.complete(ordinal, observed_microusd=generated.cost_microusd, stored=True)
        except Exception as exc:
            self.store.complete(ordinal, observed_microusd=0, stored=False)
            raise CaptureIndeterminate("structured response was not durably captured") from exc
        return generated


class CaptureStructuredTextService(StructuredTextService):
    """Inject the manifest output cap before the ordinary service opens an attempt."""

    def __init__(
        self,
        generator: StructuredTextGenerator,
        authorization: CaptureAuthorization,
        endpoints: dict[ProviderName, str],
    ) -> None:
        super().__init__(generator)
        self._authorization = authorization
        self._endpoints = endpoints

    def generate_json(
        self,
        instruction: str,
        input_text: str,
        *,
        output_model: Any,
        provider: ProviderName,
        model: str,
        options: GenerationOptions = DEFAULT_GENERATION_OPTIONS,
    ) -> Any:
        caller_options = options
        endpoint = self._endpoints.get(provider)
        if endpoint is None:
            raise CaptureDenied("structured provider endpoint is unavailable")
        if provider is ProviderName.GEMINI:
            endpoint = f"{endpoint}/{quote(model, safe='')}:generateContent"
        route = self._authorization.structured_route(provider, model, endpoint)
        if options.max_tokens is None:
            options = replace(options, max_tokens=route["max_output_tokens"])
        elif options.max_tokens > route["max_output_tokens"]:
            raise CaptureDenied("structured output-token authorization would be exceeded")
        token = _capture_replay_generation_options.set(caller_options)
        try:
            return super().generate_json(
                instruction,
                input_text,
                output_model=output_model,
                provider=provider,
                model=model,
                options=options,
            )
        finally:
            _capture_replay_generation_options.reset(token)


class CaptureEmbeddingClient:
    """One bounded, no-retry live Voyage batch per dynamic vector request."""

    def __init__(self, live: VoyageEmbeddingClient, store: CaptureStore) -> None:
        self.live = live
        self.store = store
        self.model = live.model
        self.dimensions = live.dimensions

    async def embed(
        self, texts: Sequence[str], *, input_type: InputType
    ) -> FloatMatrix:
        normalized_texts = [" ".join(item.split()) for item in texts]
        rows = normalized_texts
        normalized_hashes = [_sha256(item.encode()) for item in normalized_texts]
        expected_kind = "query_embedding" if input_type == "query" else "embedding"
        scope = "query_embedding" if input_type == "query" else "proposal_embedding"
        self.store.authorization.allows_voyage(self.model, self.dimensions, self.live.url, scope)
        input_bytes = sum(len(item.encode()) for item in rows)
        if len(rows) > 1_000 or sum(len(item) for item in rows) > 280_000:
            raise CaptureDenied("capture embedding request would require more than one dispatch")
        handle = begin_provider_call(
            provider="voyage",
            model=self.model,
            instruction="capture dynamic Voyage embedding",
            input_text=json.dumps(normalized_hashes, separators=(",", ":")),
            output_schema={"type": "matrix", "dimensions": self.dimensions},
            generation_parameters={"input_type": input_type, "row_count": len(rows)},
            cacheable_source_prefix=None,
        )
        if handle is None:
            raise CaptureDenied("capture embedding requires a durable provider attempt identity")
        identity = handle.identity
        if (
            identity.kind != expected_kind
            or identity.replay_namespace != self.store.authorization.document["replay_namespace"]
        ):
            emit_provider_event(
                handle, "contract_failed", error="capture embedding identity is unauthorized"
            )
            raise CaptureDenied("capture embedding identity is not authorized")
        if (
            expected_kind == "query_embedding" and identity.stage.value not in _CAPTURE_QUERY_STAGES
        ) or (
            expected_kind == "embedding" and identity.stage.value not in _CAPTURE_PROPOSAL_STAGES
        ):
            emit_provider_event(
                handle, "contract_failed", error="capture embedding stage is unauthorized"
            )
            raise CaptureDenied("capture embedding stage is not authorized")
        replay = ReplayEmbeddingClient(
            self.store.pack / "vectors", model=self.model, dimensions=self.dimensions
        )
        replay_request = {
            "kind": "vectors",
            "normalized_texts": normalized_texts,
            "keys": [replay._key(item, input_type) for item in normalized_texts],  # noqa: SLF001
            "text_sha256": normalized_hashes,
            "input_type": input_type,
            "dimensions": self.dimensions,
            "provider_input_sha256": handle.input_sha256,
            "provider_generation_parameters_sha256": handle.generation_parameters_sha256,
        }
        ordinal = self.store.reserve(
            kind=scope,
            rows=len(rows),
            input_bytes=input_bytes,
            cost_microusd=self.store.authorization.voyage_reservation(),
            provider="voyage",
            model=self.model,
            request_sha256=handle.request_sha256,
            replay_identity=provider_replay_identity_document(identity),
            replay_request=replay_request,
        )
        emit_provider_event(handle, "dispatched")
        try:
            vectors = await self.live.embed(rows, input_type=input_type)
            matrix = np.asarray(vectors, dtype=np.float32)
            if matrix.shape != (len(rows), self.dimensions) or not np.isfinite(matrix).all():
                raise CaptureDenied("Voyage response violates the capture vector contract")
            vector_keys = self.store.record_vectors(
                rows, matrix, model=self.model, dimensions=self.dimensions, input_type=input_type
            )
            if vector_keys != replay_request["keys"]:
                raise CaptureDenied("capture vector replay identity changed before storage")
            response = json.dumps(
                [_sha256(np.asarray(row, dtype=np.float32).tobytes()) for row in matrix],
                separators=(",", ":"),
            )
            self.store.bind_private_response(
                ordinal,
                _provider_response_sha256(response),
                replay_request,
            )
            self.store.complete(ordinal, observed_microusd=0, stored=True)
            emit_provider_event(
                handle,
                "response_received",
                request_id=f"voyage:{_sha256(response.encode())[:24]}",
                response_text=response,
            )
            emit_provider_event(
                handle, "accepted", request_id=f"voyage:{_sha256(response.encode())[:24]}"
            )
            return matrix
        except CaptureIndeterminate:
            raise
        except Exception as exc:
            self.store.complete(ordinal, observed_microusd=0, stored=False)
            if isinstance(exc, CaptureDenied):
                emit_provider_event(
                    handle,
                    "validation_failed",
                    error="capture Voyage response was not durably retained",
                )
            else:
                emit_provider_event(
                    handle,
                    "transport_failed",
                    error="capture Voyage response was not durably retained",
                )
            raise CaptureIndeterminate("Voyage response was not durably captured") from exc

    async def aclose(self) -> None:
        await self.live.aclose()


def _structured_request_sha256(
    provider: str,
    model: str,
    instruction: str,
    input_text: str,
    output_schema: dict[str, object],
    options: GenerationOptions,
    identity: object,
) -> str:
    document = {
        "provider": provider,
        "model": model,
        "instruction_sha256": _sha256(instruction.encode()),
        "input_sha256": _sha256(input_text.encode()),
        "output_schema_sha256": _sha256(_canonical(output_schema)),
        "generation_parameters_sha256": _sha256(
            _canonical(
                {
                    "thinking": options.thinking.value,
                    "thinking_budget_tokens": options.thinking_budget_tokens,
                    "temperature": options.temperature,
                    "max_tokens": options.max_tokens,
                }
            )
        ),
        "cache_prefix_sha256": (
            _sha256(options.cacheable_source_prefix.encode())
            if options.cacheable_source_prefix is not None
            else None
        ),
        "identity": provider_attempt_identity_document(cast(Any, identity)),
    }
    return _sha256(_canonical(document))
