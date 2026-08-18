import hashlib
import json
import os
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from threading import Barrier
from uuid import uuid4

import pytest

from oms_hub.anki.domain import CurationStage
from oms_hub.anki.provider_attempts import (
    ProviderAttemptBinding,
    ProviderAttemptEvent,
    ProviderAttemptIdentity,
    ProviderAttemptIndeterminate,
    ProviderAttemptLifecycle,
    _safe_error,
    begin_provider_call,
    bind_provider_attempts,
    current_provider_attempt_identity,
    emit_provider_event,
    provider_attempt_identity_document,
    provider_call_scope,
    provider_cost_reservation,
)
from oms_hub.anki.rehearsal.structured import structured_request_key
from oms_hub.llm.domain import GenerationOptions, ProviderName


def _identity() -> ProviderAttemptIdentity:
    return ProviderAttemptIdentity(
        job_id=uuid4(),
        stage=CurationStage.CARD_RESIDUAL,
        stage_attempt=1,
        mode="canonical",
        call_index=1,
        batch_index=0,
        batch_note_ids=(11, 12),
        kind="primary",
    )


def test_provider_attempt_event_lifecycle_is_append_only_and_terminal() -> None:
    identity = _identity()
    lifecycle = ProviderAttemptLifecycle()
    lifecycle.append(ProviderAttemptEvent.begin(identity, request_sha256="a" * 64))
    lifecycle.append(ProviderAttemptEvent.dispatched(identity, request_sha256="a" * 64))
    lifecycle.append(
        ProviderAttemptEvent.response_received(
            identity,
            request_sha256="a" * 64,
            request_id="req-1",
            response_sha256="b" * 64,
        )
    )
    lifecycle.append(
        ProviderAttemptEvent.validation_failed(
            identity,
            request_sha256="a" * 64,
            error="partition mismatch",
            missing_note_ids=(12,),
            extra_note_ids=(13,),
            duplicate_note_ids=(11,),
        )
    )
    assert lifecycle.terminal
    assert lifecycle.events[-1].missing_note_ids == (12,)
    with pytest.raises(ValueError, match="already exists"):
        lifecycle.append(lifecycle.events[-1])


def test_dispatched_without_response_is_indeterminate() -> None:
    identity = _identity()
    lifecycle = ProviderAttemptLifecycle()
    lifecycle.append(ProviderAttemptEvent.begin(identity, request_sha256="a" * 64))
    lifecycle.append(ProviderAttemptEvent.dispatched(identity, request_sha256="a" * 64))
    with pytest.raises(ProviderAttemptIndeterminate):
        lifecycle.require_safe_to_retry()


@pytest.mark.parametrize(
    "message",
    (
        "provider failed: Authorization: Bearer secret-value",
        "provider failed: x-api-key=secret-value",
        "provider failed: bearer secret-value",
        "Gemini URL: https://x.test/generate?key=secret-value&alt=json",
        "Gemini URL: https://x.test/generate;KEY : secret-value",
        "request header X_Goog_API_Key: secret-value",
        "request header x goog api key = secret-value",
        'provider failed: {"x-goog-api-key":"secret-value"}',
        r"provider failed: {\"API_KEY\":\"secret-value\"}",
        "request header 'x-goog-api-key' = 'secret-value'",
        '{"access_token":"secret,value\\"still-secret","ok":true}',
        r"{\"refresh_token\":\"secret,still-secret\"}",
        'client_secret = "secret,value,still-secret", next=ok',
        "authorization: 'Bearer secret,value'",
        '{"cookie":"a=b, session=still-secret"}',
    ),
)
def test_provider_exception_persistence_uses_response_secret_redaction(message: str) -> None:
    persisted = _safe_error(message)
    assert persisted is not None
    assert "secret-value" not in persisted
    assert "[REDACTED]" in persisted


def test_deferred_acceptance_never_precedes_downstream_contract_failure() -> None:
    events = []
    identity = _identity()
    binding = ProviderAttemptBinding(
        job_id=identity.job_id,
        stage=identity.stage,
        stage_attempt=identity.stage_attempt,
        mode=identity.mode,
        recorder=lambda evidence: events.append(evidence.event.event),
    )
    with bind_provider_attempts(binding), provider_call_scope(batch_index=0, defer_acceptance=True):
        handle = begin_provider_call(
            provider="openai",
            model="model",
            instruction="instruction",
            input_text="input",
            output_schema={"type": "object"},
            generation_parameters={},
            cacheable_source_prefix=None,
        )
        emit_provider_event(handle, "dispatched")
        emit_provider_event(handle, "response_received", request_id="request", response_text="{}")
        emit_provider_event(handle, "accepted", request_id="request")
        emit_provider_event(handle, "contract_failed", error="inadmissible passage")
    assert events == ["begun", "dispatched", "response_received", "contract_failed"]


def test_replay_identity_survives_new_job_and_capture_mode() -> None:
    """A shadow response must service a new canonical rehearsal job."""
    events: list[object] = []

    def call_key(job_id, mode: str) -> str:
        binding = ProviderAttemptBinding(
            job_id=job_id,
            stage=CurationStage.CARD_GAP_FILL,
            stage_attempt=9,
            mode=mode,  # type: ignore[arg-type]
            replay_namespace="frozen-source-namespace",
            recorder=events.append,
        )
        with (
            bind_provider_attempts(binding),
            provider_call_scope(batch_index=3, batch_note_ids=(11, 12), kind="primary"),
        ):
            handle = begin_provider_call(
                provider="openai",
                model="model",
                instruction="instruction",
                input_text="input",
                output_schema={"type": "object"},
                generation_parameters={
                    "thinking": "disabled",
                    "thinking_budget_tokens": 1024,
                    "temperature": None,
                    "max_tokens": None,
                },
                cacheable_source_prefix=None,
            )
            assert handle is not None
            key = structured_request_key(
                "instruction",
                "input",
                output_schema={"type": "object"},
                provider=ProviderName.OPENAI,
                model="model",
                options=GenerationOptions(),
                attempt_identity=handle.identity,
            )
            emit_provider_event(handle, "transport_failed", error="test cleanup")
            return key

    assert call_key(uuid4(), "shadow") == call_key(uuid4(), "canonical")


@pytest.mark.parametrize(
    "field,value",
    (
        ("policy_revision", 2),
        ("style_fidelity", "transcript_outline"),
        ("scope_sha256", "b" * 64),
        ("retrieval_calibration", "calibration-v2"),
        ("evidence_bundle_sha256", "c" * 64),
        ("tier_escalation", "thorough"),
        ("rate_table_sha256", "d" * 64),
    ),
)
def test_v3_replay_key_invalidates_each_frozen_stage_input_identity(
    field: str, value: object
) -> None:
    """The real replay key hashes the canonical v3 request document, not a job ID."""
    base = {
        "policy_revision": 1,
        "style_fidelity": "none",
        "scope_sha256": "a" * 64,
        "retrieval_calibration": "calibration-v1",
        "evidence_bundle_sha256": "a" * 64,
        "tier_escalation": "cheap",
        "rate_table_sha256": "a" * 64,
    }
    changed = {**base, field: value}
    identity = _identity()

    def key(stage_input: dict[str, object]) -> str:
        return structured_request_key(
            "v3-stage",
            "same-provider-request",
            output_schema={"type": "object"},
            provider=ProviderName.OPENAI,
            model="model",
            options=GenerationOptions(),
            attempt_identity=replace(
                identity,
                stage_input_sha256=hashlib.sha256(
                    json.dumps(stage_input, sort_keys=True, separators=(",", ":")).encode()
                ).hexdigest(),
            ),
        )

    assert key(base) != key(changed)


def test_v3_stage_input_digest_binds_request_sha_without_changing_legacy_identity() -> None:
    legacy = _identity()
    assert provider_attempt_identity_document(legacy) == {
        "job_id": str(legacy.job_id),
        "stage": legacy.stage.value,
        "durable_attempt": 1,
        "mode": "canonical",
        "call_ordinal": 1,
        "call_kind": "primary",
        "batch_ordinal": 0,
        "batch_note_ids_sha256": legacy.batch_note_ids_sha256,
        "subcall_ordinal": 0,
    }

    def request_sha(stage_input_sha256: str) -> str:
        identity = replace(
            legacy,
            stage=CurationStage.V3_R7_CLASSIFICATION,
            stage_input_sha256=stage_input_sha256,
        )
        binding = ProviderAttemptBinding(
            job_id=identity.job_id,
            stage=identity.stage,
            stage_attempt=identity.stage_attempt,
            mode=identity.mode,
            stage_input_sha256=identity.stage_input_sha256,
            recorder=lambda _event: None,
        )
        with (
            bind_provider_attempts(binding),
            provider_call_scope(batch_index=0),
            provider_cost_reservation({"call_id": stage_input_sha256}),
        ):
            handle = begin_provider_call(
                provider="openai",
                model="model",
                instruction="instruction",
                input_text="input",
                output_schema={"type": "object"},
                generation_parameters={},
                cacheable_source_prefix=None,
            )
            emit_provider_event(handle, "transport_failed", error="test cleanup")
            return handle.request_sha256

    first = "a" * 64
    second = "b" * 64
    assert (
        provider_attempt_identity_document(replace(legacy, stage_input_sha256=first))[
            "stage_input_sha256"
        ]
        == first
    )
    assert request_sha(first) != request_sha(second)


def test_batch_call_slots_are_order_independent_and_collision_fails() -> None:
    binding = ProviderAttemptBinding(
        job_id=uuid4(),
        stage=CurationStage.CARD_FAST_CLASSIFY,
        stage_attempt=1,
        mode="canonical",
        recorder=lambda _event: None,
    )
    first = provider_call_scope(batch_index=1, batch_note_ids=(2,))
    second = provider_call_scope(batch_index=0, batch_note_ids=(1,))
    with bind_provider_attempts(binding), second:
        left = begin_provider_call(
            provider="openai",
            model="m",
            instruction="i",
            input_text="0",
            output_schema={},
            generation_parameters={},
            cacheable_source_prefix=None,
        )
    with bind_provider_attempts(binding), first:
        right = begin_provider_call(
            provider="openai",
            model="m",
            instruction="i",
            input_text="1",
            output_schema={},
            generation_parameters={},
            cacheable_source_prefix=None,
        )
    assert left is not None and right is not None
    assert left.identity.call_index != right.identity.call_index
    emit_provider_event(left, "transport_failed", error="test cleanup")
    emit_provider_event(right, "transport_failed", error="test cleanup")
    with bind_provider_attempts(binding), provider_call_scope(batch_index=0, batch_note_ids=(1,)):
        with pytest.raises(ValueError, match="collision"):
            begin_provider_call(
                provider="openai",
                model="m",
                instruction="i",
                input_text="0",
                output_schema={},
                generation_parameters={},
                cacheable_source_prefix=None,
            )


def test_batch_call_allocation_is_thread_safe_with_real_thread_contention() -> None:
    binding = ProviderAttemptBinding(
        job_id=uuid4(),
        stage=CurationStage.CARD_FAST_CLASSIFY,
        stage_attempt=1,
        mode="canonical",
        recorder=lambda _event: None,
    )

    barrier = Barrier(2)

    def allocate(batch: int):
        barrier.wait(timeout=5)
        with (
            bind_provider_attempts(binding),
            provider_call_scope(batch_index=batch, batch_note_ids=(batch + 1,)),
        ):
            return begin_provider_call(
                provider="openai",
                model="m",
                instruction="i",
                input_text=str(batch),
                output_schema={},
                generation_parameters={},
                cacheable_source_prefix=None,
            )

    with ThreadPoolExecutor(max_workers=2) as executor:
        first, second = tuple(executor.map(allocate, (1, 0)))
    assert first is not None and second is not None
    assert first.identity.batch_index == 1
    assert second.identity.batch_index == 0
    assert first.identity.call_index != second.identity.call_index
    emit_provider_event(first, "transport_failed", error="test cleanup")
    emit_provider_event(second, "transport_failed", error="test cleanup")


def test_provider_attempt_binding_cleans_active_identity_after_reversed_collisions() -> None:
    """A failed nested allocation cannot leak its outer handle across workers."""
    binding = ProviderAttemptBinding(
        job_id=uuid4(),
        stage=CurationStage.CARD_FAST_CLASSIFY,
        stage_attempt=1,
        mode="canonical",
        recorder=lambda _event: None,
    )
    barrier = Barrier(2)

    def allocate_then_collide(batch: int) -> ProviderAttemptIdentity | None:
        handle = None
        try:
            with (
                bind_provider_attempts(binding),
                provider_call_scope(batch_index=batch, batch_note_ids=(batch + 1,)),
            ):
                barrier.wait(timeout=5)
                handle = begin_provider_call(
                    provider="openai",
                    model="m",
                    instruction="i",
                    input_text=str(batch),
                    output_schema={},
                    generation_parameters={},
                    cacheable_source_prefix=None,
                )
                with provider_call_scope(batch_index=batch, batch_note_ids=(batch + 1,)):
                    with pytest.raises(ValueError, match="collision"):
                        begin_provider_call(
                            provider="openai",
                            model="m",
                            instruction="i",
                            input_text=str(batch),
                            output_schema={},
                            generation_parameters={},
                            cacheable_source_prefix=None,
                        )
                assert current_provider_attempt_identity() is handle.identity
                return handle.identity
        finally:
            assert current_provider_attempt_identity() is None

    assert current_provider_attempt_identity() is None
    with ThreadPoolExecutor(max_workers=2) as executor:
        identities = tuple(executor.map(allocate_then_collide, (1, 0)))
    assert all(identity is not None for identity in identities)
    assert current_provider_attempt_identity() is None


def test_child_fault_interlock_is_written_after_event_recorder_and_hard_exits(
    tmp_path: Path,
) -> None:
    evidence = tmp_path / "runtime-evidence"
    recorder = tmp_path / "recorder.json"
    script = """
import json
import os
from uuid import UUID
from oms_hub.anki.domain import CurationStage
from oms_hub.anki.provider_attempts import (
    ProviderAttemptBinding, begin_provider_call, bind_provider_attempts,
    emit_provider_event, provider_call_scope,
)
binding = ProviderAttemptBinding(
    job_id=UUID('12345678-1234-5678-1234-567812345678'),
    stage=CurationStage.CARD_RESIDUAL, stage_attempt=1, mode='canonical',
    recorder=lambda evidence: open(
        os.environ['RECORDER'], 'a', encoding='utf-8'
    ).write(evidence.event.event + '\\n'),
)
with bind_provider_attempts(binding), provider_call_scope(batch_index=0):
    handle = begin_provider_call(
        provider='openai', model='m', instruction='i', input_text='x',
        output_schema={}, generation_parameters={}, cacheable_source_prefix=None,
    )
    emit_provider_event(handle, 'dispatched')
"""
    environment = os.environ | {
        "RECORDER": str(recorder),
        "OMS_HUB_ANKI_REHEARSAL_FAILURE_STAGE": "card_residual",
        "OMS_HUB_ANKI_REHEARSAL_FAILURE_EVENT": "dispatched",
        "OMS_HUB_ANKI_REHEARSAL_FAILURE_OCCURRENCE": "1",
        "OMS_HUB_ANKI_REHEARSAL_FAILURE_EVIDENCE_DIR": str(evidence),
        "OMS_HUB_ANKI_REHEARSAL_RUN_NONCE": "fault-test-nonce",
        "OMS_HUB_ANKI_REHEARSAL_FAILURE_ACTION": "hard_exit",
    }
    completed = subprocess.run(
        [sys.executable, "-c", script], env=environment, check=False, capture_output=True
    )
    assert completed.returncode == 97
    assert recorder.read_text(encoding="utf-8").splitlines() == ["begun", "dispatched"]
    interlock = json.loads((evidence / "provider-fault-interlock.json").read_text())
    assert interlock["run_nonce"] == "fault-test-nonce"
    assert interlock["boundary_selector"] == "dispatched"
    assert interlock["event"] == "dispatched"
    assert interlock["action"] == "hard_exit"


@pytest.mark.parametrize("terminal_event", ("accepted", "contract_failed"))
def test_child_terminal_fault_selector_records_the_actual_matched_event(
    tmp_path: Path, terminal_event: str
) -> None:
    evidence = tmp_path / "runtime-evidence"
    recorder = tmp_path / "recorder.json"
    script = """
import os
from uuid import UUID
from oms_hub.anki.domain import CurationStage
from oms_hub.anki.provider_attempts import (
    ProviderAttemptBinding, begin_provider_call, bind_provider_attempts,
    emit_provider_event, provider_call_scope,
)
binding = ProviderAttemptBinding(
    job_id=UUID('12345678-1234-5678-1234-567812345678'),
    stage=CurationStage.CARD_RESIDUAL, stage_attempt=1, mode='canonical',
    recorder=lambda evidence: open(
        os.environ['RECORDER'], 'a', encoding='utf-8'
    ).write(evidence.event.event + '\\n'),
)
with bind_provider_attempts(binding), provider_call_scope(batch_index=0):
    handle = begin_provider_call(
        provider='openai', model='m', instruction='i', input_text='x',
        output_schema={}, generation_parameters={}, cacheable_source_prefix=None,
    )
    emit_provider_event(handle, 'dispatched')
    emit_provider_event(handle, 'response_received', request_id='req', response_text='{}')
    emit_provider_event(handle, os.environ['TERMINAL_EVENT'], request_id='req', error='failed')
"""
    environment = os.environ | {
        "RECORDER": str(recorder),
        "TERMINAL_EVENT": terminal_event,
        "OMS_HUB_ANKI_REHEARSAL_FAILURE_STAGE": "card_residual",
        "OMS_HUB_ANKI_REHEARSAL_FAILURE_EVENT": "terminal",
        "OMS_HUB_ANKI_REHEARSAL_FAILURE_OCCURRENCE": "1",
        "OMS_HUB_ANKI_REHEARSAL_FAILURE_EVIDENCE_DIR": str(evidence),
        "OMS_HUB_ANKI_REHEARSAL_RUN_NONCE": "terminal-fault-test-nonce",
        "OMS_HUB_ANKI_REHEARSAL_FAILURE_ACTION": "pause",
    }
    child = subprocess.Popen([sys.executable, "-c", script], env=environment)
    interlock_path = evidence / "provider-fault-interlock.json"
    deadline = time.monotonic() + 5
    while not interlock_path.is_file() and time.monotonic() < deadline:
        time.sleep(0.02)
    try:
        assert interlock_path.is_file()
        interlock = json.loads(interlock_path.read_text(encoding="utf-8"))
        assert interlock["boundary_selector"] == "terminal"
        assert interlock["event"] == terminal_event
        assert recorder.read_text(encoding="utf-8").splitlines()[-1] == terminal_event
    finally:
        child.kill()
        child.wait(timeout=5)
