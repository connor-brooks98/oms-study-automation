from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Literal
from uuid import UUID

from oms_hub.anki.domain import CurationStage

ProviderMode = Literal["canonical", "shadow"]
ProviderAttemptKind = Literal["primary", "repair", "embedding", "query_embedding"]
ProviderEventKind = Literal[
    "begun",
    "dispatched",
    "response_received",
    "accepted",
    "validation_failed",
    "transport_failed",
    "contract_failed",
]


class ProviderAttemptIndeterminate(RuntimeError):
    """A provider call may have been dispatched without durable outcome evidence."""


@dataclass(frozen=True, slots=True)
class ProviderAttemptIdentity:
    job_id: UUID
    stage: CurationStage
    stage_attempt: int
    mode: ProviderMode
    call_index: int
    batch_index: int | None
    batch_note_ids: tuple[int, ...]
    kind: ProviderAttemptKind
    # These fields are deliberately not part of the database/audit key.  They
    # are the stable, content-addressed key used by a captured shadow response
    # and by a new canonical rehearsal job.
    replay_namespace: str = "legacy"
    replay_attempt: int = 1
    subcall_ordinal: int = 0

    def __post_init__(self) -> None:
        if self.stage_attempt < 1 or self.call_index < 1 or self.replay_attempt < 1:
            raise ValueError("provider attempt ordinals must be positive")
        if self.batch_index is not None and self.batch_index < 0:
            raise ValueError("provider batch index cannot be negative")
        if len(set(self.batch_note_ids)) != len(self.batch_note_ids):
            raise ValueError("provider batch note IDs must be unique")
        if not self.replay_namespace:
            raise ValueError("provider replay namespace is required")
        if self.subcall_ordinal < 0:
            raise ValueError("provider subcall ordinal cannot be negative")

    @property
    def batch_note_ids_sha256(self) -> str:
        payload = json.dumps(self.batch_note_ids, separators=(",", ":"))
        return hashlib.sha256(payload.encode()).hexdigest()


@dataclass(frozen=True, slots=True)
class ProviderAttemptEvent:
    identity: ProviderAttemptIdentity
    event: ProviderEventKind
    request_sha256: str
    request_id: str | None = None
    response_sha256: str | None = None
    error: str | None = None
    missing_note_ids: tuple[int, ...] = ()
    extra_note_ids: tuple[int, ...] = ()
    duplicate_note_ids: tuple[int, ...] = ()

    @classmethod
    def begin(
        cls, identity: ProviderAttemptIdentity, *, request_sha256: str
    ) -> ProviderAttemptEvent:
        return cls(identity, "begun", request_sha256)

    @classmethod
    def dispatched(
        cls, identity: ProviderAttemptIdentity, *, request_sha256: str
    ) -> ProviderAttemptEvent:
        return cls(identity, "dispatched", request_sha256)

    @classmethod
    def response_received(
        cls,
        identity: ProviderAttemptIdentity,
        *,
        request_sha256: str,
        request_id: str,
        response_sha256: str,
    ) -> ProviderAttemptEvent:
        return cls(identity, "response_received", request_sha256, request_id, response_sha256)

    @classmethod
    def validation_failed(
        cls,
        identity: ProviderAttemptIdentity,
        *,
        request_sha256: str,
        error: str,
        missing_note_ids: tuple[int, ...] = (),
        extra_note_ids: tuple[int, ...] = (),
        duplicate_note_ids: tuple[int, ...] = (),
    ) -> ProviderAttemptEvent:
        return cls(
            identity,
            "validation_failed",
            request_sha256,
            error=error,
            missing_note_ids=missing_note_ids,
            extra_note_ids=extra_note_ids,
            duplicate_note_ids=duplicate_note_ids,
        )


@dataclass(frozen=True, slots=True)
class ProviderEventEvidence:
    event: ProviderAttemptEvent
    provider: str
    model: str
    instruction_sha256: str
    input_sha256: str
    output_schema_sha256: str
    generation_parameters: dict[str, object]
    generation_parameters_sha256: str
    cache_prefix_sha256: str | None
    request_id: str | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    cost_microusd: int = 0
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0
    response_text: str | None = None
    diagnostic_source: str | None = None
    http_status: int | None = None


ProviderEventRecorder = Callable[[ProviderEventEvidence], None]


@dataclass(slots=True)
class ProviderAttemptBinding:
    job_id: UUID
    stage: CurationStage
    stage_attempt: int
    mode: ProviderMode
    recorder: ProviderEventRecorder
    replay_namespace: str = "legacy"
    replay_attempt: int = 1
    _allocated_calls: set[int] = field(init=False, default_factory=set, repr=False)
    _allocation_lock: threading.Lock = field(
        init=False, default_factory=threading.Lock, repr=False
    )

    def __post_init__(self) -> None:
        if self.stage_attempt < 1 or self.replay_attempt < 1:
            raise ValueError("provider attempt stage ordinal must be positive")
        if not self.replay_namespace:
            raise ValueError("provider replay namespace is required")

    def allocate_call_index(self, detail: ProviderCallDetail) -> int:
        """Allocate a deterministic *audit* integer from caller-supplied slots.

        A counter makes concurrent S4 batches depend on scheduler timing.  The
        stable logical tuple is instead hashed to fit the legacy INTEGER audit
        column.  A collision is a hard failure; it must never silently merge
        two provider calls into one append-only lifecycle.
        """
        if detail.batch_index is None:
            raise ValueError("provider call requires an explicit batch ordinal")
        canonical = _canonical_sha256(
            {
                "stage": self.stage.value,
                "batch_ordinal": detail.batch_index,
                "batch_note_ids_sha256": _batch_note_ids_sha256(detail.batch_note_ids),
                "call_kind": detail.kind,
                "subcall_ordinal": detail.subcall_ordinal,
            }
        )
        # Fifteen hexadecimal digits are always representable by SQLite and
        # PostgreSQL signed BIGINT columns and are non-zero for this tuple.
        call_index = int(canonical[:15], 16) or 1
        # S4 batches run concurrently.  The ContextVar keeps their call
        # details task-local; this lock makes the shared binding allocation
        # atomic when two tasks arrive in the reverse order.
        with self._allocation_lock:
            if call_index in self._allocated_calls:
                raise ValueError("provider call identity collision")
            self._allocated_calls.add(call_index)
        return call_index


@dataclass(slots=True)
class ProviderCallDetail:
    batch_index: int | None = None
    batch_note_ids: tuple[int, ...] = ()
    kind: ProviderAttemptKind = "primary"
    subcall_ordinal: int = 0
    defer_acceptance: bool = False


@dataclass(slots=True)
class ProviderCallHandle:
    binding: ProviderAttemptBinding
    identity: ProviderAttemptIdentity
    request_sha256: str
    provider: str
    model: str
    instruction_sha256: str
    input_sha256: str
    output_schema_sha256: str
    generation_parameters: dict[str, object]
    generation_parameters_sha256: str
    cache_prefix_sha256: str | None
    defer_acceptance: bool = False
    deferred_accepted: dict[str, object] | None = field(default=None, repr=False)


_BINDING: ContextVar[ProviderAttemptBinding | None] = ContextVar(
    "anki_provider_attempt_binding", default=None
)
_DETAIL: ContextVar[ProviderCallDetail | None] = ContextVar(
    "anki_provider_call_detail", default=None
)
_ACTIVE_HANDLE: ContextVar[ProviderCallHandle | None] = ContextVar(
    "anki_active_provider_call_handle", default=None
)


@contextmanager
def bind_provider_attempts(binding: ProviderAttemptBinding) -> Iterator[None]:
    token = _BINDING.set(binding)
    # A durable handle is meaningful only while its binding owns the current
    # execution.  Always restore the caller's handle as well: a collision or
    # a caller exception after ``begun`` must not seed a later replay request.
    active_token = _ACTIVE_HANDLE.set(None)
    try:
        yield
    finally:
        _ACTIVE_HANDLE.reset(active_token)
        _BINDING.reset(token)


@contextmanager
def provider_call_scope(
    *,
    batch_index: int | None = None,
    batch_note_ids: tuple[int, ...] = (),
    kind: ProviderAttemptKind = "primary",
    subcall_ordinal: int = 0,
    defer_acceptance: bool = False,
) -> Iterator[None]:
    token = _DETAIL.set(
        ProviderCallDetail(batch_index, batch_note_ids, kind, subcall_ordinal, defer_acceptance)
    )
    try:
        yield
    finally:
        _DETAIL.reset(token)


def begin_provider_call(
    *,
    provider: str,
    model: str,
    instruction: str,
    input_text: str,
    output_schema: dict[str, object],
    generation_parameters: dict[str, object],
    cacheable_source_prefix: str | None,
) -> ProviderCallHandle | None:
    binding = _BINDING.get()
    if binding is None:
        return None
    detail = _DETAIL.get()
    if detail is None:
        raise ValueError("provider call requires provider_call_scope with a stable slot")
    identity = ProviderAttemptIdentity(
        job_id=binding.job_id,
        stage=binding.stage,
        stage_attempt=binding.stage_attempt,
        mode=binding.mode,
        call_index=binding.allocate_call_index(detail),
        batch_index=detail.batch_index,
        batch_note_ids=detail.batch_note_ids,
        kind=detail.kind,
        replay_namespace=binding.replay_namespace,
        replay_attempt=binding.replay_attempt,
        subcall_ordinal=detail.subcall_ordinal,
    )
    instruction_sha256 = hashlib.sha256(instruction.encode()).hexdigest()
    input_sha256 = hashlib.sha256(input_text.encode()).hexdigest()
    schema_sha256 = _canonical_sha256(output_schema)
    parameters_sha256 = _canonical_sha256(generation_parameters)
    cache_prefix_sha256 = (
        hashlib.sha256(cacheable_source_prefix.encode()).hexdigest()
        if cacheable_source_prefix is not None
        else None
    )
    request_sha256 = _canonical_sha256(
        {
            "provider": provider,
            "model": model,
            "instruction_sha256": instruction_sha256,
            "input_sha256": input_sha256,
            "output_schema_sha256": schema_sha256,
            "generation_parameters_sha256": parameters_sha256,
            "cache_prefix_sha256": cache_prefix_sha256,
            "identity": provider_attempt_identity_document(identity),
        }
    )
    handle = ProviderCallHandle(
        binding,
        identity,
        request_sha256,
        provider,
        model,
        instruction_sha256,
        input_sha256,
        schema_sha256,
        generation_parameters,
        parameters_sha256,
        cache_prefix_sha256,
        detail.defer_acceptance,
    )
    _ACTIVE_HANDLE.set(handle)
    emit_provider_event(handle, "begun")
    return handle


def provider_attempt_identity_document(identity: ProviderAttemptIdentity) -> dict[str, object]:
    """Audit identity; intentionally includes the actual job and capture mode."""
    return {
        "job_id": str(identity.job_id),
        "stage": identity.stage.value,
        "durable_attempt": identity.stage_attempt,
        "mode": identity.mode,
        "call_ordinal": identity.call_index,
        "call_kind": identity.kind,
        "batch_ordinal": identity.batch_index,
        "batch_note_ids_sha256": identity.batch_note_ids_sha256,
        "subcall_ordinal": identity.subcall_ordinal,
    }


def provider_replay_identity_document(identity: ProviderAttemptIdentity) -> dict[str, object]:
    """Stable replay identity, independent of ephemeral job UUID and mode."""
    return {
        "replay_namespace": identity.replay_namespace,
        "stage": identity.stage.value,
        "replay_attempt": identity.replay_attempt,
        "call_kind": identity.kind,
        "batch_ordinal": identity.batch_index,
        "batch_note_ids_sha256": identity.batch_note_ids_sha256,
        "subcall_ordinal": identity.subcall_ordinal,
    }


def replay_namespace_from_job_source(
    *,
    configuration_sha256: str,
    pipeline_contract_version: str,
    model_config_sha256: str,
    source_revision_hashes: dict[int, str],
    index_snapshot_id: str | None,
    companion_generation: str | None,
    semantic_generation: str | None,
    source_index_generation: str | None,
) -> str:
    """Name the immutable job source, never the execution that consumes it.

    This is purposely derived only from frozen candidate/job-source data.  A
    shadow job and a newly-created canonical job therefore share it, while a
    changed source, model configuration, or index generation cannot replay a
    captured response by accident.
    """
    return _canonical_sha256(
        {
            "replay_schema": 1,
            "configuration_sha256": configuration_sha256,
            "pipeline_contract_version": pipeline_contract_version,
            "model_config_sha256": model_config_sha256,
            "source_revision_hashes": {
                str(key): value for key, value in sorted(source_revision_hashes.items())
            },
            "index_snapshot_id": index_snapshot_id,
            "companion_generation": companion_generation,
            "semantic_generation": semantic_generation,
            "source_index_generation": source_index_generation,
        }
    )


def current_provider_attempt_identity() -> ProviderAttemptIdentity | None:
    """Return the active structured-call identity, if one is being recorded."""
    handle = _ACTIVE_HANDLE.get()
    return handle.identity if handle is not None else None


def emit_provider_event(
    handle: ProviderCallHandle | None,
    event: ProviderEventKind,
    *,
    request_id: str | None = None,
    input_tokens: int = 0,
    output_tokens: int = 0,
    cost_microusd: int = 0,
    cache_creation_input_tokens: int = 0,
    cache_read_input_tokens: int = 0,
    response_text: str | None = None,
    error: str | None = None,
    missing_note_ids: tuple[int, ...] = (),
    extra_note_ids: tuple[int, ...] = (),
    duplicate_note_ids: tuple[int, ...] = (),
    diagnostic_source: str | None = None,
    http_status: int | None = None,
) -> None:
    if handle is None:
        return
    if event == "accepted" and handle.defer_acceptance:
        handle.deferred_accepted = {
            "request_id": request_id,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "cost_microusd": cost_microusd,
            "cache_creation_input_tokens": cache_creation_input_tokens,
            "cache_read_input_tokens": cache_read_input_tokens,
        }
        return
    bounded_response = _bounded_redacted(response_text)
    response_sha256 = (
        hashlib.sha256(bounded_response.encode()).hexdigest()
        if bounded_response is not None
        else None
    )
    attempt_event = ProviderAttemptEvent(
        identity=handle.identity,
        event=event,
        request_sha256=handle.request_sha256,
        request_id=request_id,
        response_sha256=response_sha256,
        error=_safe_error(error),
        missing_note_ids=tuple(sorted(missing_note_ids)),
        extra_note_ids=tuple(sorted(extra_note_ids)),
        duplicate_note_ids=tuple(sorted(duplicate_note_ids)),
    )
    handle.binding.recorder(
        ProviderEventEvidence(
            event=attempt_event,
            provider=handle.provider,
            model=handle.model,
            instruction_sha256=handle.instruction_sha256,
            input_sha256=handle.input_sha256,
            output_schema_sha256=handle.output_schema_sha256,
            generation_parameters=handle.generation_parameters,
            generation_parameters_sha256=handle.generation_parameters_sha256,
            cache_prefix_sha256=handle.cache_prefix_sha256,
            request_id=request_id,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost_microusd=cost_microusd,
            cache_creation_input_tokens=cache_creation_input_tokens,
            cache_read_input_tokens=cache_read_input_tokens,
            response_text=bounded_response,
            diagnostic_source=diagnostic_source,
            http_status=http_status,
        )
    )
    _interlock_after_durable_provider_event(attempt_event)
    if event in {"accepted", "validation_failed", "transport_failed", "contract_failed"}:
        if _ACTIVE_HANDLE.get() is handle:
            _ACTIVE_HANDLE.set(None)


def finalize_provider_call(handle: ProviderCallHandle | None) -> None:
    """Persist a deferred acceptance only after caller-level contracts pass."""
    if handle is None or handle.deferred_accepted is None:
        return
    values = handle.deferred_accepted
    handle.deferred_accepted = None
    handle.defer_acceptance = False
    emit_provider_event(handle, "accepted", **values)  # type: ignore[arg-type]


def _canonical_sha256(value: object) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(payload.encode()).hexdigest()


def _batch_note_ids_sha256(note_ids: tuple[int, ...]) -> str:
    return hashlib.sha256(json.dumps(note_ids, separators=(",", ":")).encode()).hexdigest()


def _bounded_redacted(value: str | None) -> str | None:
    if value is None:
        return None
    # The value branch consumes an entire quoted JSON value (including escaped
    # commas/quotes), rather than stopping at a suffix which could remain a
    # credential in an audit record.  It also accepts escaped JSON delimiters
    # because exceptions are commonly rendered twice before persistence.
    sensitive_key = (
        r"(?:x[_. -]?goog[_. -]?api[_. -]?key|x[_. -]?api[_. -]?key|"
        r"(?:x-)?api[_ -]?key|access[_ -]?token|refresh[_ -]?token|"
        r"client[_ -]?secret|token|secret|password|cookie)"
    )
    quoted_value = r'(?:\\?"(?:\\.|[^"\\])*\\?"|\'(?:\\.|[^\'\\])*\')'
    bare_value = r"[^\s,;}&\]]+"
    redacted = re.sub(
        rf"(?is)(\bauthorization\s*(?:\\?[:=])\s*)(?:{quoted_value}|(?:bearer\s+)?{bare_value})",
        r"\1[REDACTED]",
        value,
    )
    key_value = re.compile(
        rf"(?is)((?:\\?[\"']?){sensitive_key}(?:\\?[\"']?)?\s*(?:\\?[:=])\s*)"
        rf"(?:{quoted_value}|{bare_value})"
    )
    redacted = key_value.sub(r"\1[REDACTED]", redacted)
    # Query-string API keys and bearer authorization lack a conventional JSON
    # key/value shell, so redact their complete token values separately.
    redacted = re.sub(
        r"(?i)([?&;][a-z0-9_.-]*(?:key|token|secret)\s*[:=]\s*)[^&#;\s]+",
        r"\1[REDACTED]",
        redacted,
    )
    redacted = re.sub(r"(?i)\bbearer\s+[^\s,;}&\]]+", "Bearer [REDACTED]", redacted)
    return redacted[:200_000]


_FAULT_LOCK = threading.Lock()
_FAULT_OCCURRENCES: dict[tuple[str, str, str, str], int] = {}
_TERMINAL_PROVIDER_EVENTS = frozenset(
    {"accepted", "validation_failed", "transport_failed", "contract_failed"}
)


def _interlock_after_durable_provider_event(event: ProviderAttemptEvent) -> None:
    """Stop *inside the child* directly after a committed provider event.

    This is deliberately invoked only after the recorder returns.  The
    repository recorder commits its fenced transaction synchronously, so the
    nonce-bound evidence below cannot advertise a boundary which was merely
    observed by a racing parent process.
    """
    stage = os.environ.get("OMS_HUB_ANKI_REHEARSAL_FAILURE_STAGE")
    boundary = os.environ.get("OMS_HUB_ANKI_REHEARSAL_FAILURE_EVENT")
    occurrence_raw = os.environ.get("OMS_HUB_ANKI_REHEARSAL_FAILURE_OCCURRENCE")
    evidence_dir = os.environ.get("OMS_HUB_ANKI_REHEARSAL_FAILURE_EVIDENCE_DIR")
    nonce = os.environ.get("OMS_HUB_ANKI_REHEARSAL_RUN_NONCE")
    if not all((stage, boundary, occurrence_raw, evidence_dir, nonce)):
        return
    assert stage is not None
    assert boundary is not None
    assert occurrence_raw is not None
    assert evidence_dir is not None
    assert nonce is not None
    matches_boundary = boundary == event.event or (
        boundary == "terminal" and event.event in _TERMINAL_PROVIDER_EVENTS
    )
    if stage != event.identity.stage.value or not matches_boundary:
        return
    try:
        occurrence = int(occurrence_raw)
    except ValueError as exc:
        raise RuntimeError("rehearsal failure occurrence is invalid") from exc
    if occurrence < 1:
        raise RuntimeError("rehearsal failure occurrence is invalid")
    key = (stage, boundary, occurrence_raw, nonce)
    with _FAULT_LOCK:
        seen = _FAULT_OCCURRENCES.get(key, 0) + 1
        _FAULT_OCCURRENCES[key] = seen
    if seen != occurrence:
        return
    directory = os.path.abspath(evidence_dir)
    os.makedirs(directory, exist_ok=True)
    destination = os.path.join(directory, "provider-fault-interlock.json")
    if os.path.exists(destination):
        raise RuntimeError("rehearsal failure interlock evidence already exists")
    payload = {
        "schema_version": 1,
        "run_nonce": nonce,
        "pid": os.getpid(),
        "stage": stage,
        # ``terminal`` is a selector, never a ProviderEventKind.  Evidence
        # must retain the concrete, already-committed event that matched it.
        "boundary_selector": boundary,
        "event": event.event,
        "occurrence": occurrence,
        "call_index": event.identity.call_index,
        "subcall_ordinal": event.identity.subcall_ordinal,
        "request_sha256": event.request_sha256,
        "response_sha256": event.response_sha256,
        "action": os.environ.get("OMS_HUB_ANKI_REHEARSAL_FAILURE_ACTION", "pause"),
    }
    descriptor, temporary = tempfile.mkstemp(prefix=".provider-fault-", dir=directory)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            json.dump(payload, output, sort_keys=True, separators=(",", ":"))
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, destination)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    if payload["action"] == "hard_exit":
        os._exit(97)
    if payload["action"] != "pause":
        raise RuntimeError("rehearsal failure interlock action is invalid")
    while True:
        time.sleep(3600)


def _safe_error(value: str | None) -> str | None:
    if value is None:
        return None
    redacted = _bounded_redacted(value)
    assert redacted is not None
    return " ".join(redacted.split())[:2_000]


class ProviderAttemptLifecycle:
    _ORDER = {
        "begun": 0,
        "dispatched": 1,
        "response_received": 2,
        "accepted": 3,
        "validation_failed": 3,
        "transport_failed": 2,
        "contract_failed": 3,
    }
    _TERMINAL = {"accepted", "validation_failed", "transport_failed", "contract_failed"}

    def __init__(self) -> None:
        self.events: list[ProviderAttemptEvent] = []

    @property
    def terminal(self) -> bool:
        return bool(self.events and self.events[-1].event in self._TERMINAL)

    def append(self, event: ProviderAttemptEvent) -> None:
        if any(existing.event == event.event for existing in self.events):
            raise ValueError(f"provider attempt event {event.event} already exists")
        if self.events:
            first = self.events[0]
            if event.identity != first.identity or event.request_sha256 != first.request_sha256:
                raise ValueError("provider attempt event identity changed")
            if self.terminal and not (
                self.events[-1].event == "accepted" and event.event == "contract_failed"
            ):
                raise ValueError("provider attempt lifecycle is already terminal")
            if self._ORDER[event.event] < self._ORDER[self.events[-1].event]:
                raise ValueError("provider attempt event order is invalid")
        elif event.event != "begun":
            raise ValueError("provider attempt lifecycle must begin with begun")
        self.events.append(event)

    def require_safe_to_retry(self) -> None:
        if self.events and self.events[-1].event == "dispatched":
            raise ProviderAttemptIndeterminate(
                "provider attempt was dispatched without durable response evidence"
            )
