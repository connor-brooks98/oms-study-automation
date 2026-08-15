from __future__ import annotations

import hashlib
import json
import os
import runpy
import shutil
import subprocess
import sys
import sysconfig
import time
import zipfile
from ast import Import, ImportFrom, parse
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from uuid import UUID

import pytest

from oms_hub.anki.contracts import CreateCurationJobRequest
from oms_hub.anki.domain import CurationStage, PipelineContractVersion, ResolvedModelConfiguration
from oms_hub.anki.provider_attempts import (
    ProviderAttemptBinding,
    begin_provider_call,
    bind_provider_attempts,
    provider_call_scope,
)
from oms_hub.anki.rehearsal import process as process_module
from oms_hub.anki.rehearsal.process import (
    LoopbackHttp,
    ProcessRehearsal,
    RehearsalRequest,
    RehearsalResult,
    _environment_evidence,
    _expected_empty_replay_miss,
    _failure_injection_stage_order,
    _load_runtime_ledger,
    _replay_namespace_sha256,
    _require_no_denied_egress_authorizations,
    _stable_logical_call_ids,
    _validate_adapter_ledger,
    _validate_egress_ledger,
    _validate_empty_overlay_replay,
    _validate_empty_replay_supplement,
    _verify_replay_supplement,
    _write_deterministic_zip,
    fresh_job_payload,
    run_failure_injection_matrix,
    unchanged_review_payload,
)


def _request(tmp_path: Path, **changes: object) -> RehearsalRequest:
    implementation = tmp_path / "implementation"
    implementation.mkdir(exist_ok=True)
    values: dict[str, object] = {
        "capsule": tmp_path / "capsule",
        "overlay": tmp_path / "overlay",
        "mode": "deterministic",
        "port": 8788,
        "evidence_zip": tmp_path / "evidence.zip",
        "failed_job_id": UUID("12345678-1234-5678-1234-567812345678"),
        "expected_manifest_sha256": "0" * 64,
        "implementation_repository": implementation,
        "expected_implementation_commit": "a" * 40,
        "expected_implementation_tree": "b" * 40,
        "trusted_python": Path(sys.executable),
    }
    values.update(changes)
    return RehearsalRequest(**values)  # type: ignore[arg-type]


def _begun_recovery_rows(
    restarted_events: tuple[str, ...],
) -> tuple[SimpleNamespace, dict[str, object], list[dict[str, object]]]:
    job = SimpleNamespace(
        configuration_sha256="a" * 64,
        pipeline_contract_version=SimpleNamespace(value="card_centric_v2"),
        model_config_sha256="b" * 64,
        source_revision_hashes={1: "c" * 64},
        index_snapshot_id="snapshot",
        companion_generation="companion",
        semantic_generation="semantic",
        source_index_generation="source-index",
    )
    material: dict[str, object] = {
        "stage": "card_residual",
        "kind": "primary",
        "batch_index": 0,
        "batch_note_ids_sha256": "d" * 64,
        "subcall_ordinal": 0,
        "provider": "openai",
        "model": "fixture",
        "instruction_sha256": "e" * 64,
        "input_sha256": "f" * 64,
        "output_schema_sha256": "0" * 64,
        "generation_parameters_sha256": "1" * 64,
        "cache_prefix_sha256": None,
    }
    precrash = material | {
        "id": 41,
        "stage_attempt": 1,
        "mode": "shadow",
        "call_index": 17,
        "request_sha256": "2" * 64,
        "event": "begun",
    }
    restarted = [
        material
        | {
            "id": 42 + ordinal,
            "stage_attempt": 2,
            "mode": "canonical",
            "call_index": 23,
            "request_sha256": "3" * 64,
            "event": event,
        }
        for ordinal, event in enumerate(restarted_events)
    ]
    return job, precrash, [precrash, *restarted]


def _recovery_repository(job: SimpleNamespace, rows: list[dict[str, object]]) -> SimpleNamespace:
    return SimpleNamespace(
        require_no_indeterminate_provider_attempt=lambda *_args: None,
        require_job=lambda _job_id: job,
        list_provider_attempt_events=lambda _job_id: rows,
    )


def test_failure_injection_matrix_runs_every_real_provider_stage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Bind selected stages to an observed durable ``begun`` event, not source text."""
    observed: list[CurationStage] = []

    class RecordedRunner:
        def __init__(self, request: RehearsalRequest) -> None:
            self.request = request

        def run(self) -> RehearsalResult:
            assert self.request.failure_injection is not None
            stage, _checkpoint = self.request.failure_injection
            events: list[object] = []
            binding = ProviderAttemptBinding(
                job_id=UUID(int=1),
                stage=stage,
                stage_attempt=1,
                mode="canonical",
                recorder=events.append,
            )
            # This is the same structured-provider boundary the stage runners
            # use: a stable batch slot followed by a durable begun recorder.
            with (
                bind_provider_attempts(binding),
                provider_call_scope(batch_index=0, batch_note_ids=(101,)),
            ):
                begin_provider_call(
                    provider="openai",
                    model="fixture",
                    instruction="classify fixture batch",
                    input_text="fixture note",
                    output_schema={"type": "object"},
                    generation_parameters={"temperature": 0},
                    cacheable_source_prefix=None,
                )
            assert [item.event.event for item in events] == ["begun"]
            assert events[0].event.identity.stage is stage
            observed.append(stage)
            _write_deterministic_zip(
                self.request.evidence_zip, {"fixture.json": {"stage": stage.value}}
            )
            return RehearsalResult(UUID(int=1), self.request.overlay, self.request.evidence_zip)

    monkeypatch.setattr(process_module, "ProcessRehearsal", RecordedRunner)
    request = _request(tmp_path, restart_after_durable_boundary=False)
    results = run_failure_injection_matrix(request)
    expected = _failure_injection_stage_order()
    assert expected == (
        CurationStage.CARD_LEDGER,
        CurationStage.CARD_PREFILTER,
        CurationStage.CARD_FAST_CLASSIFY,
        CurationStage.CARD_CLASSIFY,
        CurationStage.CARD_RESIDUAL,
        CurationStage.CARD_GAP_FILL,
        CurationStage.DEDUPE,
    )
    assert tuple(dict.fromkeys(observed)) == expected
    assert {result.stage for result in results} == set(expected)
    assert len(results) == len(expected) * 4


def test_minimal_subprocess_interlock_harness_restarts_and_packages_truthful_evidence(
    tmp_path: Path,
) -> None:
    """Exercise real child interlock/egress mechanics without claiming a Hub run."""
    evidence = tmp_path / "runtime-evidence"
    event_ledger = tmp_path / "provider-events.jsonl"
    restarted = tmp_path / "restarted.txt"
    source = Path(__file__).parents[2] / "src"
    script = """
import os
from pathlib import Path
from uuid import UUID
from oms_hub.anki.domain import CurationStage
from oms_hub.anki.provider_attempts import (
    ProviderAttemptBinding, begin_provider_call, bind_provider_attempts,
    emit_provider_event, provider_call_scope,
)
from oms_hub.anki.rehearsal.network import EgressEvidenceLedger, EgressPolicy, SocketEgressGuard
ledger = Path(os.environ['EVENT_LEDGER'])
guard = SocketEgressGuard(EgressPolicy.deterministic(EgressEvidenceLedger(
    Path(os.environ['OMS_HUB_ANKI_REHEARSAL_FAILURE_EVIDENCE_DIR']),
    mode='deterministic', run_nonce=os.environ['OMS_HUB_ANKI_REHEARSAL_RUN_NONCE'],
)))
guard.install()
try:
    binding = ProviderAttemptBinding(
        job_id=UUID('12345678-1234-5678-1234-567812345678'),
        stage=CurationStage.CARD_RESIDUAL, stage_attempt=1, mode='canonical',
        recorder=lambda evidence: ledger.open('a', encoding='utf-8').write(
            evidence.event.event + '\\n'
        ),
    )
    with bind_provider_attempts(binding), provider_call_scope(batch_index=0, batch_note_ids=(1,)):
        handle = begin_provider_call(
            provider='openai', model='m', instruction='i', input_text='x',
            output_schema={}, generation_parameters={}, cacheable_source_prefix=None,
        )
        emit_provider_event(handle, 'dispatched')
    Path(os.environ['RESTARTED']).write_text(str(os.getpid()), encoding='utf-8')
finally:
    guard.uninstall()
"""
    base = os.environ | {
        "PYTHONPATH": str(source),
        "EVENT_LEDGER": str(event_ledger),
        "RESTARTED": str(restarted),
        "OMS_HUB_ANKI_REHEARSAL_FAILURE_STAGE": "card_residual",
        "OMS_HUB_ANKI_REHEARSAL_FAILURE_EVENT": "dispatched",
        "OMS_HUB_ANKI_REHEARSAL_FAILURE_OCCURRENCE": "1",
        "OMS_HUB_ANKI_REHEARSAL_FAILURE_EVIDENCE_DIR": str(evidence),
        "OMS_HUB_ANKI_REHEARSAL_RUN_NONCE": "harness-nonce",
        "OMS_HUB_ANKI_REHEARSAL_FAILURE_ACTION": "pause",
    }
    direct_python = sys._base_executable if os.name == "nt" else sys.executable
    first = subprocess.Popen([direct_python, "-c", script], env=base)
    try:
        interlock_path = evidence / "provider-fault-interlock.json"
        deadline = time.monotonic() + 5
        while not interlock_path.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert interlock_path.is_file()
        interlock = json.loads(interlock_path.read_text(encoding="utf-8"))
        runtime_pid = interlock["pid"]
        assert isinstance(runtime_pid, int) and runtime_pid > 0
        assert runtime_pid == first.pid
        assert interlock["event"] == "dispatched"
        assert interlock["action"] == "pause"
        # This deliberately has no launcher wrapper and does not claim to test
        # Windows runtime-tree ownership. Keep cleanup in finally so an
        # assertion failure cannot leave its paused direct child behind.
        first.kill()
        first.wait(timeout=10)
        assert first.returncode is not None
        crash_egress = _load_runtime_ledger(evidence / "egress-decisions.json")
        _validate_egress_ledger(
            crash_egress, "harness-nonce", "deterministic", require_clean_lifecycle=False
        )
        assert [row["kind"] for row in crash_egress["records"]] == ["startup"]

        second_environment = base.copy()
        for key in (
            "OMS_HUB_ANKI_REHEARSAL_FAILURE_STAGE",
            "OMS_HUB_ANKI_REHEARSAL_FAILURE_EVENT",
            "OMS_HUB_ANKI_REHEARSAL_FAILURE_OCCURRENCE",
            "OMS_HUB_ANKI_REHEARSAL_FAILURE_ACTION",
        ):
            second_environment.pop(key)
        second = subprocess.run([direct_python, "-c", script], env=second_environment, check=False)
        assert second.returncode == 0
        assert restarted.read_text(encoding="utf-8") != str(runtime_pid)
        final_egress = _load_runtime_ledger(evidence / "egress-decisions.json")
        _validate_egress_ledger(final_egress, "harness-nonce", "deterministic")
        destination = tmp_path / "harness-checkpoint.zip"
        _write_deterministic_zip(
            destination,
            {
                "harness.json": {
                    "execution_kind": "harness_interlock_process_test",
                    "actual_hub_run": False,
                    "process_pid": first.pid,
                    "crashed_runtime_pid": runtime_pid,
                    "restarted_pid": int(restarted.read_text(encoding="utf-8")),
                    "interlock": interlock,
                },
                "egress.json": final_egress,
            },
        )
        with zipfile.ZipFile(destination) as archive:
            payload = json.loads(archive.read("harness.json"))
        assert payload["execution_kind"] == "harness_interlock_process_test"
        assert payload["actual_hub_run"] is False
    finally:
        if first.poll() is None:
            first.kill()
            first.wait(timeout=10)


def _replay_supplement(tmp_path: Path) -> tuple[Path, str]:
    root = tmp_path / "replay-supplement"
    vectors = root / "vectors"
    vectors.mkdir(parents=True)
    (root / "structured.json").write_bytes(b"{}\n")
    (vectors / "manifest.json").write_bytes(b"{}\n")
    files = []
    for path in sorted(root.rglob("*")):
        if path.is_file():
            encoded = path.read_bytes()
            files.append(
                {
                    "path": path.relative_to(root).as_posix(),
                    "bytes": len(encoded),
                    "sha256": hashlib.sha256(encoded).hexdigest(),
                }
            )
    manifest = {
        "schema_version": 1,
        "manifest_rule": "self-excluding",
        "files": files,
    }
    manifest_path = root / "replay-supplement.json"
    manifest_path.write_text(json.dumps(manifest, sort_keys=True), encoding="utf-8")
    return root, hashlib.sha256(manifest_path.read_bytes()).hexdigest()


def _refresh_replay_supplement_manifest(root: Path) -> str:
    files = [
        {
            "path": path.relative_to(root).as_posix(),
            "bytes": path.stat().st_size,
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        }
        for path in sorted(root.rglob("*"))
        if path.is_file() and path.name != "replay-supplement.json"
    ]
    manifest = root / "replay-supplement.json"
    manifest.write_text(
        json.dumps({"schema_version": 1, "manifest_rule": "self-excluding", "files": files}),
        encoding="utf-8",
    )
    return hashlib.sha256(manifest.read_bytes()).hexdigest()


def _empty_replay_miss_rows(
    terminal: str = "transport_failed",
    *,
    kind: str = "primary",
    provider: str = "openai",
    **topology: object,
) -> list[dict[str, object]]:
    common: dict[str, object] = {
        "stage": "card_ledger",
        "stage_attempt": 1,
        "mode": "canonical",
        "call_index": 7,
        "subcall_ordinal": 0,
        "batch_index": 0,
        "batch_note_ids": [],
        "batch_note_ids_sha256": hashlib.sha256(b"[]").hexdigest(),
        "kind": kind,
        "provider": provider,
        "model": "fixture",
        "instruction_sha256": "b" * 64,
        "input_sha256": "c" * 64,
        "output_schema_sha256": "d" * 64,
        "generation_parameters": {},
        "generation_parameters_sha256": "e" * 64,
        "cache_prefix_sha256": None,
        "request_sha256": "f" * 64,
        "request_id": None,
        "input_tokens": 0,
        "output_tokens": 0,
        "cost_microusd": 0,
        "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": 0,
        "response_sha256": None,
        "response_text": None,
        "missing_note_ids": [],
        "extra_note_ids": [],
        "duplicate_note_ids": [],
        "diagnostic_source": None,
        "http_status": None,
        "created_at": "2026-08-13T00:00:00+00:00",
    }
    common.update(topology)
    error = (
        f"missing structured replay response {'1' * 64}"
        if terminal == "transport_failed"
        else "replay vector validation failed"
    )
    return [
        common
        | {
            "id": ordinal,
            "event": event,
            "validation_error": error if event == terminal else None,
        }
        for ordinal, event in enumerate(("begun", "dispatched", terminal), 1)
    ]


def test_first_replay_miss_defaults_to_golden_and_rejects_invalid_combinations(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    supplement, supplement_sha256 = _replay_supplement(tmp_path)
    capsule = tmp_path / "capsule"
    capsule.mkdir()
    (capsule / "capsule.json").write_text("{}", encoding="utf-8")
    capsule_sha256 = hashlib.sha256((capsule / "capsule.json").read_bytes()).hexdigest()
    monkeypatch.setattr(ProcessRehearsal, "_verify_implementation_identity", lambda self: None)
    monkeypatch.setattr(process_module, "verify_capsule", lambda _capsule: SimpleNamespace())
    golden = _request(
        tmp_path,
        capsule=capsule,
        expected_manifest_sha256=capsule_sha256,
        replay_supplement=supplement,
        expected_replay_supplement_manifest_sha256=supplement_sha256,
    )
    assert golden.run_goal == "golden"
    for changes, message in (
        ({"mode": "shadow", "shadow_egress_pins_json": "{}"}, "deterministic mode"),
        ({"restart_after_durable_boundary": True}, "disable restart"),
        ({"failure_injection": (CurationStage.CARD_RESIDUAL, "begun")}, "failure injection"),
    ):
        values = {
            "capsule": capsule,
            "expected_manifest_sha256": capsule_sha256,
            "replay_supplement": supplement,
            "expected_replay_supplement_manifest_sha256": supplement_sha256,
            "run_goal": "first_replay_miss",
            "restart_after_durable_boundary": False,
        } | changes
        request = _request(tmp_path, **values)
        with pytest.raises(ValueError, match=message):
            ProcessRehearsal(request)._validate_destinations()


def test_first_replay_miss_rejects_nonempty_or_malformed_replay_before_child_launch(
    tmp_path: Path,
) -> None:
    supplement, supplement_sha256 = _replay_supplement(tmp_path)
    (supplement / "structured.json").write_text('{"not":"empty"}', encoding="utf-8")
    supplement_sha256 = _refresh_replay_supplement_manifest(supplement)
    with pytest.raises(ValueError, match="empty structured replay"):
        _validate_empty_replay_supplement(supplement, supplement_sha256)
    malformed, _malformed_sha256 = _replay_supplement(tmp_path / "malformed")
    (malformed / "vectors" / "manifest.json").write_text("[]", encoding="utf-8")
    # Rebuild just the test supplement manifest so this exercises semantic
    # validation rather than its earlier byte-integrity gate.
    malformed_sha256 = _refresh_replay_supplement_manifest(malformed)
    with pytest.raises(ValueError, match="empty vector manifest"):
        _validate_empty_replay_supplement(malformed, malformed_sha256)


def test_first_replay_miss_rejects_capsule_carried_overlay_payload_before_connect(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    request = _request(tmp_path, run_goal="first_replay_miss", restart_after_durable_boundary=False)
    overlay_root = tmp_path / "overlay"
    (overlay_root / "replay" / "vectors").mkdir(parents=True)
    (overlay_root / "replay" / "structured.json").write_text("{}", encoding="utf-8")
    (overlay_root / "replay" / "vectors" / "payload.npy").write_bytes(b"not a vector")
    overlay = SimpleNamespace(root=overlay_root, database_path=overlay_root / "db.sqlite")
    connected = False
    monkeypatch.setattr(ProcessRehearsal, "_validate_destinations", lambda self: SimpleNamespace())
    monkeypatch.setattr(process_module, "materialize_capsule", lambda *_args: overlay)
    monkeypatch.setattr(ProcessRehearsal, "_install_replay_supplement", lambda *_args: None)

    def connect(*_args: object) -> LoopbackHttp:
        nonlocal connected
        connected = True
        raise AssertionError("overlay replay validation must precede connection")

    monkeypatch.setattr(ProcessRehearsal, "_start_and_connect", connect)
    with pytest.raises(ValueError, match="payload or unknown"):
        ProcessRehearsal(request).run()
    assert connected is False
    with pytest.raises(ValueError, match="payload or unknown"):
        _validate_empty_overlay_replay(overlay_root)


def test_expected_replay_miss_requires_exact_safe_error_and_single_chain() -> None:
    namespace = "0" * 64
    structured_rows = _empty_replay_miss_rows()
    miss = _expected_empty_replay_miss(
        structured_rows, namespace, f"missing structured replay response {'1' * 64}"
    )
    assert miss["kind"] == "structured_empty_pack"
    assert miss["provider_chain"] == ["begun", "dispatched", "transport_failed"]
    structured_error = f"missing structured replay response {'1' * 64}"
    for rows, error in (
        (structured_rows + _empty_replay_miss_rows(), structured_error),
        (
            structured_rows[:-1] + [structured_rows[-1] | {"event": "validation_failed"}],
            structured_error,
        ),
        (
            _empty_replay_miss_rows("validation_failed", kind="query_embedding", provider="replay"),
            "replay vector validation failed",
        ),
        (structured_rows, "arbitrary worker failure"),
        (
            structured_rows[:-1] + [structured_rows[-1] | {"response_text": "forbidden"}],
            structured_error,
        ),
    ):
        with pytest.raises(RuntimeError):
            _expected_empty_replay_miss(rows, namespace, error)


@pytest.mark.parametrize(
    "topology",
    (
        {"stage": "card_residual"},
        {"stage_attempt": 2},
        {"mode": "shadow"},
        {"kind": "repair"},
        {"batch_index": 1},
        {"batch_note_ids": [1]},
        {"subcall_ordinal": 1},
    ),
)
def test_expected_structured_replay_miss_rejects_wrong_s2_first_topology(
    topology: dict[str, object],
) -> None:
    with pytest.raises(RuntimeError, match="topology"):
        _expected_empty_replay_miss(
            _empty_replay_miss_rows(**topology),
            "0" * 64,
            f"missing structured replay response {'1' * 64}",
        )


def test_smoke_http_transcript_requires_health_create_and_status(tmp_path: Path) -> None:
    harness = ProcessRehearsal(_request(tmp_path, restart_after_durable_boundary=False))
    job_id = UUID(int=2)
    client = SimpleNamespace(
        transcript=[
            {"method": "GET", "path": "/health", "status": 200},
            {"method": "POST", "path": "/api/anki/jobs", "status": 201},
            {"method": "GET", "path": f"/api/anki/jobs/{job_id}", "status": 200},
        ]
    )
    harness._assert_smoke_http_transcript(client, job_id)
    for transcript in (
        client.transcript[1:],
        [client.transcript[0], client.transcript[2]],
        [client.transcript[0], client.transcript[1]],
        [{"method": "GET", "path": "/health", "status": "200"}, *client.transcript[1:]],
    ):
        with pytest.raises(RuntimeError):
            harness._assert_smoke_http_transcript(SimpleNamespace(transcript=transcript), job_id)
    for forbidden in ("review", "envelope", "apply", "retry", "restart"):
        with pytest.raises(RuntimeError):
            harness._assert_smoke_http_transcript(
                SimpleNamespace(
                    transcript=[
                        *client.transcript,
                        {
                            "method": "POST",
                            "path": f"/api/anki/jobs/{job_id}/{forbidden}",
                            "status": 200,
                        },
                    ]
                ),
                job_id,
            )


def test_smoke_runtime_ledger_requires_empty_mutations_and_loopback_only(tmp_path: Path) -> None:
    harness = ProcessRehearsal(_request(tmp_path, restart_after_durable_boundary=False))
    adapter = {"records": []}
    loopback = {"records": [{"kind": "authorization", "host": "127.0.0.1", "allowed": True}]}
    harness._validate_expected_replay_miss_runtime(adapter, loopback)
    with pytest.raises(RuntimeError, match="empty read-only"):
        harness._validate_expected_replay_miss_runtime(
            {"records": [{"action": "addNotes"}]}, loopback
        )
    with pytest.raises(RuntimeError, match="non-loopback"):
        harness._validate_expected_replay_miss_runtime(
            adapter,
            {"records": [{"kind": "authorization", "host": "provider.example", "allowed": True}]},
        )


def test_process_run_executes_expected_replay_miss_branch_in_safe_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    request = _request(tmp_path, run_goal="first_replay_miss", restart_after_durable_boundary=False)
    harness = ProcessRehearsal(request)
    job_id = UUID(int=2)
    order: list[str] = []
    child = SimpleNamespace(
        observation=process_module.ProcessObservation(7, 7, "start", None, None, ("python",))
    )
    harness._children = [child]
    manifest = SimpleNamespace()
    overlay = SimpleNamespace(
        root=tmp_path / "overlay", database_path=tmp_path / "overlay/db.sqlite"
    )
    (overlay.root / "replay").mkdir(parents=True)
    (overlay.root / "replay" / "structured.json").write_text("{}", encoding="utf-8")
    repository = SimpleNamespace(require_job=lambda _job_id: SimpleNamespace(id=UUID(int=1)))
    client = SimpleNamespace(request=lambda *_args: (201, {"id": str(job_id)}))
    monkeypatch.setattr(ProcessRehearsal, "_validate_destinations", lambda self: manifest)
    monkeypatch.setattr(
        process_module, "materialize_capsule", lambda *_args: order.append("materialize") or overlay
    )
    monkeypatch.setattr(
        ProcessRehearsal, "_install_replay_supplement", lambda *_args: order.append("install")
    )
    monkeypatch.setattr(
        process_module, "Database", lambda _url: SimpleNamespace(close=lambda: None)
    )
    monkeypatch.setattr(process_module, "AnkiCurationRepository", lambda _database: repository)
    monkeypatch.setattr(
        ProcessRehearsal, "_start_and_connect", lambda *_args: order.append("connect") or client
    )
    monkeypatch.setattr(process_module, "fresh_job_payload", lambda _failed: {})
    monkeypatch.setattr(
        ProcessRehearsal,
        "_poll",
        lambda *_args, **_kwargs: (
            order.append("poll")
            or {"state": "failed", "error": "missing structured replay response " + "1" * 64}
        ),
    )
    monkeypatch.setattr(
        ProcessRehearsal,
        "_validate_expected_replay_miss",
        lambda *_args: order.append("miss") or {"kind": "structured_empty_pack"},
    )
    monkeypatch.setattr(
        ProcessRehearsal, "_assert_smoke_http_transcript", lambda *_args: order.append("http")
    )

    def stop_all(_overlay: object = None) -> None:
        order.append("stop")
        child.observation = process_module.ProcessObservation(7, 7, "start", "end", 0, ("python",))

    def runtime(_overlay: object) -> tuple[dict[str, object], dict[str, object]]:
        assert child.observation.ended_at == "end"
        order.append("runtime")
        return {"records": []}, {"records": []}

    def policy(*_args: object) -> None:
        assert child.observation.ended_at == "end"
        order.append("policy")

    def write(*_args: object) -> None:
        assert child.observation.ended_at == "end"
        order.append("write")
        _write_deterministic_zip(
            request.evidence_zip, {"outcome.json": {"result": "EXPECTED_REPLAY_MISS"}}
        )

    original_verify = process_module._verify_evidence_zip
    monkeypatch.setattr(ProcessRehearsal, "_stop_all", lambda self, overlay=None: stop_all(overlay))
    monkeypatch.setattr(
        ProcessRehearsal, "_validate_runtime_evidence", lambda self, value: runtime(value)
    )
    monkeypatch.setattr(
        ProcessRehearsal,
        "_validate_expected_replay_miss_runtime",
        lambda self, *args: policy(*args),
    )
    monkeypatch.setattr(
        ProcessRehearsal, "_write_expected_replay_miss_evidence", lambda self, *args: write(*args)
    )
    monkeypatch.setattr(
        process_module,
        "_verify_evidence_zip",
        lambda path: order.append("verify") or original_verify(path),
    )
    monkeypatch.setattr(
        ProcessRehearsal, "_restart_after_durable_boundary", lambda *_args: pytest.fail("restart")
    )
    monkeypatch.setattr(
        ProcessRehearsal,
        "_assert_provider_ledger_is_restart_safe",
        lambda *_args: pytest.fail("golden provider check"),
    )
    monkeypatch.setattr(
        process_module, "unchanged_review_payload", lambda *_args: pytest.fail("golden review")
    )

    result = harness.run()

    assert result.outcome == "expected_replay_miss"
    assert result.run_goal == "first_replay_miss"
    assert order.index("materialize") < order.index("install") < order.index("connect")
    assert order.index("connect") < order.index("poll") < order.index("miss") < order.index("http")
    assert order.index("http") < order.index("stop") < order.index("runtime")
    assert (
        order.index("runtime")
        < order.index("policy")
        < order.index("write")
        < order.index("verify")
    )


def test_refuses_existing_overlay_and_evidence_before_capsule_access(tmp_path: Path) -> None:
    overlay = tmp_path / "overlay"
    overlay.mkdir()
    with pytest.raises(ValueError, match="overlay destination"):
        ProcessRehearsal(_request(tmp_path, overlay=overlay))._validate_destinations()
    evidence = tmp_path / "evidence.zip"
    evidence.write_bytes(b"prior evidence")
    with pytest.raises(ValueError, match="evidence destination"):
        ProcessRehearsal(
            _request(tmp_path, overlay=tmp_path / "fresh-overlay", evidence_zip=evidence)
        )._validate_destinations()


def test_manifest_digest_is_required_before_self_consistency(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    capsule = tmp_path / "capsule"
    capsule.mkdir()
    (capsule / "capsule.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(ProcessRehearsal, "_verify_implementation_identity", lambda self: None)
    with pytest.raises(ValueError, match="operator-supplied"):
        ProcessRehearsal(_request(tmp_path))._validate_destinations()


def test_replay_supplement_is_manifest_bound_and_copies_only_replay_files(
    tmp_path: Path,
) -> None:
    supplement, manifest_sha256 = _replay_supplement(tmp_path)
    assert _verify_replay_supplement(supplement, manifest_sha256) == (
        "structured.json",
        "vectors/manifest.json",
    )
    overlay_root = tmp_path / "overlay"
    (overlay_root / "replay").mkdir(parents=True)
    # Materialized Windows overlays can contain the same JSON placeholder with
    # CRLF; only the empty structured value, not its native newline bytes,
    # determines whether replacement is safe.
    (overlay_root / "replay/structured.json").write_bytes(b"{}\r\n")
    harness = ProcessRehearsal(
        _request(
            tmp_path,
            replay_supplement=supplement,
            expected_replay_supplement_manifest_sha256=manifest_sha256,
        )
    )
    harness._install_replay_supplement(SimpleNamespace(root=overlay_root))  # type: ignore[arg-type]
    assert (overlay_root / "replay/structured.json").read_bytes() == b"{}\n"
    assert (overlay_root / "replay/vectors/manifest.json").read_bytes() == b"{}\n"


def test_replay_supplement_rejects_unknown_files_and_missing_operator_hash(tmp_path: Path) -> None:
    supplement, manifest_sha256 = _replay_supplement(tmp_path)
    (supplement / "unexpected.txt").write_text("no", encoding="utf-8")
    with pytest.raises(ValueError, match="unknown or sensitive"):
        _verify_replay_supplement(supplement, manifest_sha256)
    clean, _ = _replay_supplement(tmp_path / "other")
    with pytest.raises(ValueError, match="operator-supplied"):
        _verify_replay_supplement(clean, None)


def test_replay_supplement_copy_race_fails_before_child_launch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    supplement, manifest_sha256 = _replay_supplement(tmp_path)
    overlay_root = tmp_path / "overlay"
    (overlay_root / "replay").mkdir(parents=True)
    (overlay_root / "replay/structured.json").write_bytes(b"{}\n")
    harness = ProcessRehearsal(
        _request(
            tmp_path,
            replay_supplement=supplement,
            expected_replay_supplement_manifest_sha256=manifest_sha256,
        )
    )

    def raced_copy(_source: Path, destination: Path) -> str:
        destination.write_text('{"raced":true}\n', encoding="utf-8")
        return str(destination)

    monkeypatch.setattr("oms_hub.anki.rehearsal.process.shutil.copyfile", raced_copy)
    with pytest.raises(RuntimeError, match="do not match operator manifest"):
        harness._install_replay_supplement(SimpleNamespace(root=overlay_root))  # type: ignore[arg-type]


def test_windows_job_binds_exact_popen_handle_and_uses_ctrl_break_then_close(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeApi:
        def __init__(self) -> None:
            self.calls: list[tuple[str, int]] = []

        def create_kill_on_close_job(self) -> int:
            self.calls.append(("create", 7))
            return 7

        def assign_process_handle(self, handle: int, process_handle: int) -> None:
            self.calls.append(("assign", handle))
            self.calls.append(("process_handle", process_handle))

        def active_processes(self, handle: int) -> int:
            self.calls.append(("active", handle))
            return 0

        def send_ctrl_break(self, process_group_id: int) -> None:
            self.calls.append(("break", process_group_id))

        def terminate_job(self, handle: int) -> None:
            self.calls.append(("terminate", handle))

        def close_handle(self, handle: int) -> None:
            self.calls.append(("close", handle))

    api = FakeApi()
    monkeypatch.setattr(process_module, "_is_windows", lambda: True)
    monkeypatch.setattr(process_module, "_windows_job_api", lambda: api)
    job = process_module._WindowsJob.create()
    job.assign_process_handle(34)
    job.send_ctrl_break(12)
    assert job.active_processes() == 0
    job.close()
    assert api.calls == [
        ("create", 7),
        ("assign", 7),
        ("process_handle", 34),
        ("break", 12),
        ("active", 7),
        ("close", 7),
    ]


def test_windows_job_assignment_failure_preserves_close_ownership(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeApi:
        def __init__(self) -> None:
            self.closed = False

        def create_kill_on_close_job(self) -> int:
            return 7

        def assign_process_handle(self, _handle: int, _process_handle: int) -> None:
            raise OSError("access denied")

        def close_handle(self, _handle: int) -> None:
            self.closed = True

    api = FakeApi()
    monkeypatch.setattr(process_module, "_is_windows", lambda: True)
    monkeypatch.setattr(process_module, "_windows_job_api", lambda: api)
    job = process_module._WindowsJob.create()
    with pytest.raises(OSError, match="access denied"):
        job.assign_process_handle(34)
    job.close()
    assert api.closed is True


def test_windows_job_close_is_retryable_after_closehandle_failure() -> None:
    class FakeApi:
        def __init__(self) -> None:
            self.close_calls = 0

        def close_handle(self, _handle: int) -> None:
            self.close_calls += 1
            if self.close_calls == 1:
                raise OSError("transient close failure")

    api = FakeApi()
    job = process_module._WindowsJob(api, 7)
    with pytest.raises(OSError, match="transient close failure"):
        job.close()
    assert job.closed is False
    job.close()
    assert job.closed is True
    assert api.close_calls == 2


def test_windows_job_setup_preserves_setinformation_failure_when_close_also_fails() -> None:
    api = object.__new__(process_module._CtypesWindowsJobApi)
    api._kernel32 = SimpleNamespace(  # type: ignore[attr-defined]
        CreateJobObjectW=lambda *_args: 7,
        SetInformationJobObject=lambda *_args: 0,
    )
    api._extended_limit = lambda: SimpleNamespace(  # type: ignore[attr-defined]
        BasicLimitInformation=SimpleNamespace(LimitFlags=0)
    )
    api._JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000  # type: ignore[attr-defined]
    api._JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9  # type: ignore[attr-defined]
    api._ctypes = SimpleNamespace(byref=lambda value: value, sizeof=lambda _value: 1)  # type: ignore[attr-defined]

    def fail_checked(result: object, action: str) -> None:
        if not result:
            raise OSError(action)

    api._checked = fail_checked  # type: ignore[method-assign]
    api.close_handle = lambda _handle: (_ for _ in ()).throw(OSError("close"))  # type: ignore[method-assign]
    with pytest.raises(OSError, match="SetInformationJobObject") as raised:
        api.create_kill_on_close_job()
    assert any("close" in note for note in getattr(raised.value, "__notes__", ()))


def test_windows_startup_environment_failure_precedes_job_and_log_creation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class FakeJob:
        handle = 7

        def __init__(self) -> None:
            self.close_calls = 0

        def close(self) -> None:
            self.close_calls += 1

    job = FakeJob()
    harness = ProcessRehearsal(_request(tmp_path))
    runtime = tmp_path / "python.exe"
    runtime.write_bytes(b"runtime")
    dependencies = tmp_path / "site-packages"
    dependencies.mkdir()
    harness._windows_runtime_identity = {
        "base_executable": str(runtime),
        "base_executable_sha256": hashlib.sha256(b"runtime").hexdigest(),
        "python_version": "test",
        "dependency_paths": [str(dependencies)],
    }
    overlay = SimpleNamespace(root=tmp_path / "overlay")
    monkeypatch.setattr(harness, "_assert_loopback_port_is_free", lambda: None)
    monkeypatch.setattr(process_module, "_is_windows", lambda: True)
    monkeypatch.setattr(process_module._WindowsJob, "create", classmethod(lambda _cls: job))

    def fail_environment(*_args: object) -> dict[str, str]:
        raise ValueError("bad environment")

    monkeypatch.setattr(harness, "_environment", fail_environment)
    with pytest.raises(ValueError, match="bad environment"):
        harness._start_and_connect(overlay, SimpleNamespace())  # type: ignore[arg-type]
    assert job.close_calls == 0
    logs = overlay.root / "rehearsal" / "process-logs"
    assert not logs.exists()


def test_windows_handshake_releases_only_self_bound_runtime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class FakeJob:
        def __init__(self) -> None:
            self.assigned: list[int] = []

        def assign_process_handle(self, handle: int) -> None:
            self.assigned.append(handle)

        def active_processes(self) -> int:
            return 1

        def close(self) -> None:
            raise AssertionError("bound job must remain open after release")

    class FakeProcess:
        pid = 34
        _handle = 71

    harness = ProcessRehearsal(_request(tmp_path, runtime_evidence_nonce="handshake-nonce"))
    ready = tmp_path / "startup.ready.json"
    release = tmp_path / "startup.release.json"
    ready.write_text(
        json.dumps({"schema_version": 1, "pid": 34, "run_nonce": "handshake-nonce"}),
        encoding="utf-8",
    )
    stdout_path = tmp_path / "stdout.log"
    stderr_path = tmp_path / "stderr.log"
    stdout = stdout_path.open("wb")
    stderr = stderr_path.open("wb")
    child = process_module._Child(
        FakeProcess(),  # type: ignore[arg-type]
        process_module.ProcessObservation(12, None, "start", None, None, ("serve",)),
        stdout_path,
        stderr_path,
        stdout,
        stderr,
        job=FakeJob(),  # type: ignore[arg-type]
        startup_ready_path=ready,
        startup_release_path=release,
    )
    monkeypatch.setattr(process_module, "_is_windows", lambda: True)
    try:
        harness._release_windows_runtime_after_handshake(child, time.monotonic() + 1)
    finally:
        stdout.close()
        stderr.close()
    assert child.runtime_pid == 34
    assert child.observation.runtime_pid == 34
    assert child.job.assigned == [71]  # type: ignore[union-attr]
    assert json.loads(release.read_text(encoding="utf-8")) == {
        "schema_version": 1,
        "pid": 34,
        "run_nonce": "handshake-nonce",
    }
    assert harness._timeline[-1]["event"] == "runtime_job_bound_and_released"


def test_windows_handshake_pid_mismatch_fails_before_release_and_closes_job(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class FakeJob:
        def __init__(self) -> None:
            self.closed = False

        def assign_process_handle(self, _handle: int) -> None:
            raise AssertionError("PID mismatch must not assign the Job")

        def active_processes(self) -> int:
            return 0

        def close(self) -> None:
            self.closed = True

    class FakeProcess:
        pid = 12
        _handle = 71

        def poll(self) -> int:
            return 0

    harness = ProcessRehearsal(_request(tmp_path, runtime_evidence_nonce="nonce"))
    ready = tmp_path / "startup.ready.json"
    release = tmp_path / "startup.release.json"
    ready.write_text(
        json.dumps({"schema_version": 1, "pid": 34, "run_nonce": "nonce"}), encoding="utf-8"
    )
    stdout = (tmp_path / "stdout.log").open("wb")
    stderr = (tmp_path / "stderr.log").open("wb")
    job = FakeJob()
    child = process_module._Child(
        FakeProcess(),  # type: ignore[arg-type]
        process_module.ProcessObservation(12, None, "start", None, None, ("serve",)),
        tmp_path / "stdout.log",
        tmp_path / "stderr.log",
        stdout,
        stderr,
        job=job,  # type: ignore[arg-type]
        startup_ready_path=ready,
        startup_release_path=release,
    )
    monkeypatch.setattr(process_module, "_is_windows", lambda: True)
    try:
        with pytest.raises(RuntimeError, match="PID does not match"):
            harness._release_windows_runtime_after_handshake(child, time.monotonic() + 1)
    finally:
        stdout.close()
        stderr.close()
    assert job.closed is True
    assert not release.exists()


def test_windows_popen_handle_requires_existing_handle() -> None:
    assert process_module._windows_popen_handle(SimpleNamespace(_handle=71)) == 71  # type: ignore[arg-type]
    with pytest.raises(RuntimeError, match="handle is unavailable"):
        process_module._windows_popen_handle(SimpleNamespace(_handle=None))  # type: ignore[arg-type]


def test_windows_runtime_probe_rejects_relative_executable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = ProcessRehearsal(_request(tmp_path))
    monkeypatch.setattr(
        process_module.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(
            returncode=0,
            stdout=json.dumps(
                {"base_executable": "python.exe", "version": "test"}
            ),
        ),
    )
    with pytest.raises(ValueError, match="base executable"):
        harness._probe_windows_runtime()


def test_windows_runtime_probe_canonicalizes_duplicate_dependency_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    dependency = tmp_path / "site-packages"
    dependency.mkdir()
    harness = ProcessRehearsal(
        _request(
            tmp_path,
            trusted_dependency_paths=(dependency.resolve(), dependency.resolve()),
        )
    )
    monkeypatch.setattr(
        process_module.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(
            returncode=0,
            stdout=json.dumps(
                {
                    "base_executable": sys.executable,
                    "version": "test",
                }
            ),
        ),
    )
    identity = harness._probe_windows_runtime()
    assert identity["dependency_paths"] == [str(dependency)]
    harness._windows_runtime_identity = identity
    command = harness._command(runtime_executable=harness._windows_runtime_executable())
    assert json.loads(command[-1]) == [str(dependency)]


def test_windows_runtime_probe_uses_launcher_attested_real_venv_dependency_path(
    tmp_path: Path,
) -> None:
    launcher_venv = tmp_path / "launcher-venv"
    subprocess.run(
        [sys.executable, "-m", "venv", "--copies", "--without-pip", str(launcher_venv)],
        check=True,
        capture_output=True,
        text=True,
    )
    launcher_python = launcher_venv / (
        "Scripts/python.exe" if os.name == "nt" else "bin/python"
    )
    if os.name != "nt":
        library_name = sysconfig.get_config_var("LDLIBRARY")
        assert isinstance(library_name, str)
        source_library = Path(sys.base_prefix) / "lib" / library_name
        assert source_library.is_file()
        target_library = launcher_venv / "lib" / library_name
        target_library.parent.mkdir(exist_ok=True)
        shutil.copy2(source_library, target_library)
    purelib = Path(
        subprocess.run(
            [
                str(launcher_python),
                "-c",
                "import sysconfig;print(sysconfig.get_paths()['purelib'])",
            ],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
    )
    purelib.mkdir(parents=True, exist_ok=True)
    harness = ProcessRehearsal(
        _request(
            tmp_path,
            trusted_python=launcher_python,
            trusted_dependency_paths=(purelib.resolve(),),
        )
    )

    identity = harness._probe_windows_runtime()

    assert identity["dependency_paths"] == [str(purelib.resolve())]


def test_windows_runtime_rejects_missing_or_indirect_attested_dependency_paths(
    tmp_path: Path,
) -> None:
    with pytest.raises(ValueError, match="dependency paths are unavailable"):
        ProcessRehearsal(_request(tmp_path))._trusted_dependency_paths()
    with pytest.raises(ValueError, match="unavailable or indirect"):
        ProcessRehearsal(
            _request(tmp_path, trusted_dependency_paths=(Path("relative-site-packages"),))
        )._trusted_dependency_paths()


@pytest.mark.parametrize(
    "evidence",
    (
        None,
        {"schema_version": 1, "dependencies": {}},
        {
            "schema_version": 1,
            "dependencies": {
                name: {"origin": "/missing", "version": "test"}
                for name in ("fastapi", "sqlalchemy", "starlette")
            },
        },
        {
            "schema_version": 1,
            "dependencies": {
                name: {"origin": "/missing"}
                for name in ("fastapi", "sqlalchemy", "starlette", "uvicorn")
            },
        },
    ),
)
def test_windows_dependency_closure_rejects_malformed_or_incomplete_evidence(
    evidence: object,
) -> None:
    with pytest.raises(RuntimeError, match="dependency closure"):
        process_module._validate_runtime_dependency_closure(evidence, [str(Path.cwd())])


def test_windows_child_capability_requires_runtime_identity(
    tmp_path: Path,
) -> None:
    with pytest.raises(RuntimeError, match="identity was not preflighted"):
        ProcessRehearsal(_request(tmp_path))._probe_windows_child_capability({})


def test_windows_child_capability_rejects_malformed_success_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    dependency = tmp_path / "site-packages"
    dependency.mkdir()
    runtime = Path(sys.executable).resolve()
    harness = ProcessRehearsal(
        _request(tmp_path, trusted_dependency_paths=(dependency.resolve(),))
    )
    harness._windows_runtime_identity = {
        "base_executable": str(runtime),
        "base_executable_sha256": hashlib.sha256(runtime.read_bytes()).hexdigest(),
        "python_version": sys.version,
        "dependency_paths": [str(dependency.resolve())],
    }
    monkeypatch.setattr(
        process_module.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(returncode=0, stdout="not-json", stderr=""),
    )
    with pytest.raises(RuntimeError, match="evidence is malformed"):
        harness._probe_windows_child_capability({})


def test_windows_child_capability_rejects_dependency_origin_outside_attested_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    dependency = tmp_path / "site-packages"
    dependency.mkdir()
    outside = tmp_path / "outside"
    outside_origins: dict[str, Path] = {}
    for name in ("fastapi", "sqlalchemy", "starlette", "uvicorn"):
        origin = outside / name / "__init__.py"
        origin.parent.mkdir(parents=True)
        origin.write_text("", encoding="utf-8")
        outside_origins[name] = origin
    runtime = Path(sys.executable).resolve()
    harness = ProcessRehearsal(
        _request(tmp_path, trusted_dependency_paths=(dependency.resolve(),))
    )
    harness._windows_runtime_identity = {
        "base_executable": str(runtime),
        "base_executable_sha256": hashlib.sha256(runtime.read_bytes()).hexdigest(),
        "python_version": sys.version,
        "dependency_paths": [str(dependency.resolve())],
    }
    monkeypatch.setattr(
        process_module.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(
            returncode=0,
            stdout=json.dumps(
                {
                    "schema_version": 1,
                    "dependencies": {
                        name: {
                            "origin": str(outside_origins[name]),
                            "version": "test",
                        }
                        for name in ("fastapi", "sqlalchemy", "starlette", "uvicorn")
                    },
                }
            ),
            stderr="",
        ),
    )

    with pytest.raises(RuntimeError, match="dependency closure"):
        harness._probe_windows_child_capability({"SYSTEMROOT": str(tmp_path)})


def test_windows_attestation_requires_canonical_preflight_dependency_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    implementation = tmp_path / "implementation"
    source = implementation / "src"
    source.mkdir(parents=True)
    dependency = tmp_path / "site-packages"
    dependency.mkdir()
    dependency_modules = {
        name: {
            "origin": str(dependency / name / "__init__.py"),
            "version": "test",
        }
        for name in ("fastapi", "sqlalchemy", "starlette", "uvicorn")
    }
    for evidence in dependency_modules.values():
        Path(cast(str, evidence["origin"])).parent.mkdir(parents=True)
        Path(cast(str, evidence["origin"])).write_text("", encoding="utf-8")
    harness = ProcessRehearsal(_request(tmp_path, implementation_repository=implementation))
    harness._source_tree_sha256 = "c" * 64
    harness._windows_runtime_identity = {
        "base_executable": str(Path(sys.executable).resolve()),
        "base_executable_sha256": "a" * 64,
        "python_version": "test",
        "dependency_paths": [str(dependency)],
        "dependency_modules": dependency_modules,
    }
    attestation = tmp_path / "attestation.json"
    payload = {
        "source": str(source.resolve()),
        "modules": {
            "oms_hub": str(source / "oms_hub/__init__.py"),
            "oms_hub.cli": str(source / "oms_hub/cli.py"),
        },
        "python_executable": str(Path(sys.executable).resolve()),
        "python_version": "test",
        "isolated": True,
        "no_site": True,
        "ignore_environment": True,
        "bootstrap_dependency_paths": [str(dependency)],
        "runtime_dependencies": dependency_modules,
        "pid": 34,
        "run_nonce": harness._runtime_evidence_nonce,
        "commit": harness.request.expected_implementation_commit,
        "tree": harness.request.expected_implementation_tree,
        "source_tree_sha256": "c" * 64,
        "source_files": {},
    }
    attestation.write_text(json.dumps(payload), encoding="utf-8")
    monkeypatch.setattr(process_module, "_is_windows", lambda: True)
    harness._validate_source_attestation(attestation)
    payload["bootstrap_dependency_paths"] = [str(dependency), str(dependency)]
    attestation.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(RuntimeError, match="wrong dependency paths"):
        harness._validate_source_attestation(attestation)
    payload["bootstrap_dependency_paths"] = [str(dependency)]
    payload["python_executable"] = str(tmp_path / "wrong-python.exe")
    attestation.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(RuntimeError, match="wrong runtime executable"):
        harness._validate_source_attestation(attestation)
    payload["python_executable"] = str(Path(sys.executable).resolve())
    payload["python_version"] = "wrong"
    attestation.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(RuntimeError, match="wrong runtime version"):
        harness._validate_source_attestation(attestation)
    payload["python_version"] = "test"
    payload["runtime_dependencies"] = {
        **dependency_modules,
        "uvicorn": {
            "origin": str(tmp_path / "outside" / "uvicorn" / "__init__.py"),
            "version": "test",
        },
    }
    attestation.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(RuntimeError, match="dependency closure"):
        harness._validate_source_attestation(attestation)


def test_windows_runtime_hash_is_rechecked_before_direct_launch(tmp_path: Path) -> None:
    runtime = tmp_path / "python.exe"
    runtime.write_bytes(b"before")
    harness = ProcessRehearsal(_request(tmp_path))
    harness._windows_runtime_identity = {
        "base_executable": str(runtime),
        "base_executable_sha256": hashlib.sha256(b"before").hexdigest(),
        "python_version": "test",
        "dependency_paths": [],
    }
    runtime.write_bytes(b"after")
    with pytest.raises(RuntimeError, match="changed after preflight"):
        harness._windows_runtime_executable()


@pytest.mark.parametrize("hard", (False, True))
def test_windows_stop_closes_parent_job_once_after_clean_shutdown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, hard: bool
) -> None:
    class FakeJob:
        closed = False

        def __init__(self) -> None:
            self.calls: list[str] = []

        def send_ctrl_break(self, _group_id: int) -> None:
            self.calls.append("break")

        def terminate(self) -> None:
            self.calls.append("terminate")

        def close(self) -> None:
            self.calls.append("close")
            self.closed = True

    class FakeProcess:
        pid = 12

        def wait(self, timeout: float) -> int:
            del timeout
            return 0

    job = FakeJob()
    harness = ProcessRehearsal(_request(tmp_path))
    monkeypatch.setattr(process_module, "_is_windows", lambda: True)
    monkeypatch.setattr(harness, "_wait_for_windows_job_empty", lambda *_args, **_kwargs: True)
    child = SimpleNamespace(process=FakeProcess(), runtime_pid=34, job=job)
    harness._stop_windows_job_child(child, hard=hard)  # type: ignore[arg-type]
    assert job.calls == (["terminate", "close"] if hard else ["break", "close"])


def test_windows_stop_hard_failure_closes_job_then_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class FakeJob:
        closed = False

        def __init__(self) -> None:
            self.calls: list[str] = []

        def terminate(self) -> None:
            self.calls.append("terminate")

        def close(self) -> None:
            self.calls.append("close")
            self.closed = True

    job = FakeJob()
    harness = ProcessRehearsal(_request(tmp_path))
    monkeypatch.setattr(process_module, "_is_windows", lambda: True)
    monkeypatch.setattr(harness, "_wait_for_windows_job_empty", lambda *_args, **_kwargs: False)
    child = SimpleNamespace(process=SimpleNamespace(pid=12), runtime_pid=34, job=job)
    with pytest.raises(RuntimeError, match="did not empty after hard termination"):
        harness._stop_windows_job_child(child, hard=True)  # type: ignore[arg-type]
    assert job.calls == ["terminate", "close"]


def test_windows_job_empty_wait_requires_popen_to_exit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class FakeJob:
        def active_processes(self) -> int:
            return 0

    class FakeProcess:
        def wait(self, timeout: float) -> int:
            del timeout
            raise subprocess.TimeoutExpired("serve", 0.1)

    harness = ProcessRehearsal(_request(tmp_path))
    clock_values = iter((0.0, 0.0))
    monkeypatch.setattr(harness, "_clock", lambda: next(clock_values))
    assert harness._wait_for_windows_job_empty(FakeJob(), FakeProcess(), timeout=1) is False  # type: ignore[arg-type]


def test_loopback_http_requires_csrf_before_unsafe_request() -> None:
    client = LoopbackHttp(8788)
    with pytest.raises(RuntimeError, match="CSRF bootstrap"):
        client.request("POST", "/api/anki/jobs", {})


def test_loopback_http_bootstraps_csrf_before_post() -> None:
    from http.cookiejar import Cookie

    class Response:
        status = 200

        def __enter__(self) -> Response:
            return self

        def __exit__(self, *args: object) -> None:
            del args

        def read(self) -> bytes:
            return b"{}"

    class Opener:
        calls: list[object] = []

        def open(self, request: object, timeout: int) -> Response:
            del timeout
            self.calls.append(request)
            if len(self.calls) == 1:
                client.cookies.set_cookie(
                    Cookie(
                        version=0,
                        name="study_hub_csrf",
                        value="csrf-value",
                        port=None,
                        port_specified=False,
                        domain="127.0.0.1",
                        domain_specified=False,
                        domain_initial_dot=False,
                        path="/",
                        path_specified=True,
                        secure=False,
                        expires=None,
                        discard=True,
                        comment=None,
                        comment_url=None,
                        rest={},
                        rfc2109=False,
                    )
                )
            return Response()

    client = LoopbackHttp(8788)
    opener = Opener()
    client._opener = opener  # type: ignore[assignment]
    client.bootstrap_csrf()
    client.request("POST", "/api/anki/jobs", {})
    assert opener.calls[1].headers["X-csrf-token"] == "csrf-value"


def test_fresh_job_payload_matches_http_contract() -> None:
    config = ResolvedModelConfiguration.card_centric_v2_default("openai", "gpt-5")
    job = SimpleNamespace(
        pipeline_contract_version=PipelineContractVersion.CARD_CENTRIC_V2,
        lecture_id=1,
        block_id="block",
        source_revision_ids=(2, 3),
        deck_allowlist=("Deck",),
        tag_allowlist=("Tag",),
        instruction_text="",
        target_deck="Deck",
        target_tag="Tag",
        index_snapshot_id="snapshot",
        lcl_prompt_version="lcl-v1",
        judgment_rubric_version="judgment-v1",
        gap_prompt_version="gap-v1",
        provider="openai",
        model="gpt-5",
        resolved_model_config=config,
        source_revision_hashes={2: "a" * 64, 3: "b" * 64},
        summary_outline_id=4,
        summary_outline_sha256="c" * 64,
        semantic_generation="semantic",
        companion_generation="companion",
    )
    payload = fresh_job_payload(job)  # type: ignore[arg-type]
    assert (
        CreateCurationJobRequest.model_validate(payload).pipeline_contract_version
        == "card_centric_v2"
    )


def test_command_and_environment_are_explicit_and_overlay_bound(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = ProcessRehearsal(_request(tmp_path))
    overlay = type(
        "Overlay",
        (),
        {"root": tmp_path / "overlay", "database_path": tmp_path / "overlay/hub/hub.db"},
    )()
    harness._source_tree_sha256 = "c" * 64
    command = harness._command()
    prompt_directory = overlay.root / "sources/repository/src/oms_hub/anki/prompt_assets"
    prompt_directory.mkdir(parents=True)
    manifest = SimpleNamespace(logical_roots={"repository": "sources/repository"})
    environment = harness._environment(overlay, manifest)  # type: ignore[arg-type]
    assert command[:4] == [str(harness.request.trusted_python.resolve()), "-I", "-S", "-c"]
    assert command[5] == str((harness.request.implementation_repository / "src").resolve())
    assert environment["OMS_HUB_DATABASE_URL"] == f"sqlite:///{overlay.database_path}"
    assert environment["OMS_HUB_ANKI_REHEARSAL_OVERLAY_DIR"] == str(overlay.root)
    assert environment["OMS_HUB_ANKI_REHEARSAL_RUN_NONCE"] == harness._runtime_evidence_nonce
    assert environment["OMS_HUB_ANKI_REHEARSAL_SOURCE_TREE_SHA256"] == "c" * 64
    assert environment["OMS_HUB_STUDY_ROOT"] == str(overlay.root / "study")
    assert environment["OMS_HUB_ICLOUD_STAGING_ROOT"] == str(overlay.root / "icloud-staging")
    assert Path(environment["OMS_HUB_STUDY_ROOT"]).is_dir()
    assert Path(environment["OMS_HUB_ICLOUD_STAGING_ROOT"]).is_dir()
    assert environment["OMS_HUB_ANKI_PROMPT_DIRECTORY"] == str(prompt_directory)
    assert environment["OMS_HUB_DASHBOARD_HOST"] == "127.0.0.1"
    assert "PYTHONPATH" not in environment
    assert "sys.flags.no_site" in command[4]
    assert "bootstrap_dependency_paths" in command[4]
    assert command[0] == str(harness.request.trusted_python.resolve())
    assert "AssignProcessToJobObject" not in command[4]
    assert "handle_list" not in command[4]
    assert command[4].index("temporary.write_text") < command[4].index("raise SystemExit")


def test_windows_environment_adds_only_canonical_systemroot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = ProcessRehearsal(_request(tmp_path))
    harness._source_tree_sha256 = "c" * 64
    overlay = type(
        "Overlay",
        (),
        {"root": tmp_path / "overlay", "database_path": tmp_path / "overlay/hub/hub.db"},
    )()
    prompt_directory = overlay.root / "sources/repository/src/oms_hub/anki/prompt_assets"
    prompt_directory.mkdir(parents=True)
    system_root = tmp_path / "Windows"
    system_root.mkdir()
    monkeypatch.setattr(process_module, "_is_windows", lambda: True)
    monkeypatch.setattr(
        process_module.os,
        "environ",
        {"PATH": "allowlisted-path", "WINDIR": "forbidden", "sYsTeMrOoT": str(system_root)},
    )

    environment = harness._environment(
        overlay, SimpleNamespace(logical_roots={"repository": "sources/repository"})
    )

    assert environment["SYSTEMROOT"] == str(system_root.resolve())
    assert "sYsTeMrOoT" not in environment
    assert "WINDIR" not in environment
    assert {key for key in environment if not key.startswith("OMS_HUB_")} == {
        "PATH",
        "SYSTEMROOT",
    }


def test_windows_child_capability_preflight_uses_exact_child_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class FakeJob:
        def __init__(self) -> None:
            self.closed = False

        def close(self) -> None:
            self.closed = True

    dependency = tmp_path / "site-packages"
    dependency.mkdir()
    dependency_evidence = {
        name: {
            "origin": str(dependency / name / "__init__.py"),
            "version": "test",
        }
        for name in ("fastapi", "sqlalchemy", "starlette", "uvicorn")
    }
    for item in dependency_evidence.values():
        origin = Path(cast(str, item["origin"]))
        origin.parent.mkdir(parents=True)
        origin.write_text("", encoding="utf-8")
    harness = ProcessRehearsal(
        _request(tmp_path, trusted_dependency_paths=(dependency.resolve(),))
    )
    executable = Path(sys.executable).resolve()
    harness._windows_runtime_identity = {
        "base_executable": str(executable),
        "base_executable_sha256": hashlib.sha256(executable.read_bytes()).hexdigest(),
        "python_version": "test",
        "dependency_paths": [str(dependency.resolve())],
    }
    overlay = SimpleNamespace(root=tmp_path / "overlay")
    environment = {"PATH": "allowlisted-path", "SYSTEMROOT": str(tmp_path)}
    job = FakeJob()
    subprocess_calls: list[tuple[list[str], dict[str, object]]] = []
    popen_environments: list[dict[str, str]] = []

    def capability_run(argv: list[str], **kwargs: object) -> SimpleNamespace:
        subprocess_calls.append((argv, kwargs))
        return SimpleNamespace(
            returncode=0,
            stdout=json.dumps(
                {"schema_version": 1, "dependencies": dependency_evidence}
            ),
            stderr="",
        )

    def halt_after_preflight(_argv: list[str], **kwargs: object) -> subprocess.Popen[bytes]:
        popen_environments.append(cast(dict[str, str], kwargs["env"]))
        raise RuntimeError("halt after capability preflight")

    monkeypatch.setattr(harness, "_assert_loopback_port_is_free", lambda: None)
    monkeypatch.setattr(harness, "_environment", lambda *_args: environment)
    monkeypatch.setattr(process_module, "_is_windows", lambda: True)
    monkeypatch.setattr(process_module.subprocess, "run", capability_run)
    monkeypatch.setattr(process_module.subprocess, "CREATE_NEW_PROCESS_GROUP", 0, raising=False)
    monkeypatch.setattr(process_module._WindowsJob, "create", classmethod(lambda _cls: job))
    harness._popen = halt_after_preflight

    with pytest.raises(RuntimeError, match="halt after capability preflight"):
        harness._start_and_connect(overlay, SimpleNamespace())  # type: ignore[arg-type]

    assert subprocess_calls == [
        (
            [
                str(executable),
                "-I",
                "-S",
                "-B",
                "-c",
                process_module._WINDOWS_CHILD_CAPABILITY_PROBE,
                json.dumps([str(dependency.resolve())], separators=(",", ":")),
            ],
            {
                "check": False,
                "capture_output": True,
                "text": True,
                "timeout": 10,
                "env": environment,
            },
        )
    ]
    assert popen_environments == [environment]
    assert popen_environments[0] is environment
    assert job.closed is True


def test_windows_child_capability_failure_precedes_job_log_and_hub_child(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = ProcessRehearsal(_request(tmp_path))
    executable = Path(sys.executable).resolve()
    harness._windows_runtime_identity = {
        "base_executable": str(executable),
        "base_executable_sha256": hashlib.sha256(executable.read_bytes()).hexdigest(),
        "python_version": "test",
        "dependency_paths": [],
    }
    overlay = SimpleNamespace(root=tmp_path / "overlay")
    monkeypatch.setattr(harness, "_assert_loopback_port_is_free", lambda: None)
    monkeypatch.setattr(harness, "_environment", lambda *_args: {"PATH": "allowlisted"})
    monkeypatch.setattr(process_module, "_is_windows", lambda: True)
    monkeypatch.setattr(
        process_module.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(returncode=1, stdout="", stderr="failure"),
    )
    monkeypatch.setattr(
        process_module._WindowsJob,
        "create",
        classmethod(lambda _cls: pytest.fail("Job creation must follow capability preflight")),
    )
    harness._popen = lambda *_args, **_kwargs: pytest.fail("Hub child must not start")

    with pytest.raises(RuntimeError, match="capability preflight failed"):
        harness._start_and_connect(overlay, SimpleNamespace())  # type: ignore[arg-type]
    assert not (overlay.root / "rehearsal" / "process-logs").exists()


def _exited_windows_preinitialization_child(
    tmp_path: Path, harness: ProcessRehearsal, *, exit_code: int = 1
) -> tuple[SimpleNamespace, process_module._Child]:
    class ExitedProcess:
        pid = 73

        def __init__(self) -> None:
            self.returncode = exit_code

        def poll(self) -> int:
            return self.returncode

    evidence = tmp_path / "overlay/rehearsal/runtime-evidence"
    evidence.mkdir(parents=True)
    identity = {"schema_version": 1, "pid": 73, "run_nonce": harness._runtime_evidence_nonce}
    ready = evidence / "startup-1.ready.json"
    release = evidence / "startup-1.release.json"
    ready.write_text(json.dumps(identity), encoding="utf-8")
    release.write_text(json.dumps(identity), encoding="utf-8")
    stdout_path = tmp_path / "stdout.log"
    stderr_path = tmp_path / "stderr.log"
    child = process_module._Child(
        ExitedProcess(),  # type: ignore[arg-type]
        process_module.ProcessObservation(73, None, "start", None, None, ("python",)),
        stdout_path,
        stderr_path,
        stdout_path.open("wb"),
        stderr_path.open("wb"),
        attestation_path=evidence / "implementation-source-1.json",
        startup_ready_path=ready,
        startup_release_path=release,
    )
    return SimpleNamespace(root=tmp_path / "overlay"), child


def test_windows_preinitialization_exit_skips_only_absent_runtime_ledgers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = ProcessRehearsal(_request(tmp_path, runtime_evidence_nonce="nonce"))
    overlay, child = _exited_windows_preinitialization_child(tmp_path, harness)
    monkeypatch.setattr(process_module, "_is_windows", lambda: True)

    harness._stop_child(child, overlay)  # type: ignore[arg-type]

    assert harness._timeline[-1] == {
        "at": harness._timeline[-1]["at"],
        "event": "windows_preinitialization_exit_without_runtime_ledgers",
        "process_pid": 73,
        "exit_code": 1,
    }


def test_windows_preinitialization_exit_closes_job_without_signaling(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class FakeJob:
        def __init__(self) -> None:
            self.closed = False
            self.calls: list[str] = []

        def send_ctrl_break(self, _process_group_id: int) -> None:
            self.calls.append("ctrl_break")

        def terminate(self) -> None:
            self.calls.append("terminate")

        def close(self) -> None:
            self.calls.append("close")
            self.closed = True

    harness = ProcessRehearsal(_request(tmp_path, runtime_evidence_nonce="nonce"))
    overlay, child = _exited_windows_preinitialization_child(tmp_path, harness)
    job = FakeJob()
    child.job = job  # type: ignore[assignment]
    monkeypatch.setattr(process_module, "_is_windows", lambda: True)

    harness._stop_child(child, overlay)  # type: ignore[arg-type]

    assert job.calls == ["close"]
    assert job.closed is True
    assert child.stdout.closed is True
    assert child.stderr.closed is True
    assert [entry["event"] for entry in harness._timeline[-2:]] == [
        "process_stopped",
        "windows_preinitialization_exit_without_runtime_ledgers",
    ]


@pytest.mark.parametrize(
    "case",
    ("source_attestation", "partial_ledger", "malformed_handshake", "successful_exit"),
)
def test_windows_preinitialization_ledger_exception_remains_fail_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, case: str
) -> None:
    harness = ProcessRehearsal(_request(tmp_path, runtime_evidence_nonce="nonce"))
    overlay, child = _exited_windows_preinitialization_child(
        tmp_path, harness, exit_code=0 if case == "successful_exit" else 1
    )
    evidence = overlay.root / "rehearsal" / "runtime-evidence"
    if case == "source_attestation":
        assert child.attestation_path is not None
        child.attestation_path.write_text("{}", encoding="utf-8")
    elif case == "partial_ledger":
        (evidence / "read-only-anki-mutation-ledger.json").write_text("{}", encoding="utf-8")
    elif case == "malformed_handshake":
        assert child.startup_ready_path is not None
        child.startup_ready_path.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(process_module, "_is_windows", lambda: True)

    with pytest.raises(RuntimeError, match="runtime evidence is missing"):
        harness._stop_child(child, overlay)  # type: ignore[arg-type]


def test_run_preserves_startup_error_when_cleanup_runtime_validation_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = ProcessRehearsal(_request(tmp_path))
    overlay = SimpleNamespace(root=tmp_path / "overlay", database_path=tmp_path / "overlay/hub.db")
    monkeypatch.setattr(harness, "_validate_destinations", lambda: SimpleNamespace())
    monkeypatch.setattr(process_module, "materialize_capsule", lambda *_args: overlay)
    monkeypatch.setattr(harness, "_install_replay_supplement", lambda _overlay: None)
    monkeypatch.setattr(
        process_module, "Database", lambda _url: SimpleNamespace(close=lambda: None)
    )
    monkeypatch.setattr(
        harness,
        "_start_and_connect",
        lambda *_args: (_ for _ in ()).throw(RuntimeError("startup primary failure")),
    )
    monkeypatch.setattr(
        harness,
        "_stop_all",
        lambda *_args: (_ for _ in ()).throw(RuntimeError("cleanup runtime evidence failure")),
    )

    with pytest.raises(RuntimeError, match="startup primary failure") as raised:
        harness.run()
    assert any("cleanup runtime evidence failure" in note for note in raised.value.__notes__)


def test_restarted_child_environment_is_disarmed_after_archiving_initial_interlock(
    tmp_path: Path,
) -> None:
    harness = ProcessRehearsal(
        _request(tmp_path, failure_injection=(CurationStage.CARD_RESIDUAL, "begun"))
    )
    harness._source_tree_sha256 = "c" * 64
    overlay = type(
        "Overlay",
        (),
        {"root": tmp_path / "overlay", "database_path": tmp_path / "overlay/hub/hub.db"},
    )()
    prompt_directory = overlay.root / "sources/repository/src/oms_hub/anki/prompt_assets"
    prompt_directory.mkdir(parents=True)
    manifest = SimpleNamespace(logical_roots={"repository": "sources/repository"})
    first = harness._environment(overlay, manifest)  # type: ignore[arg-type]
    assert first["OMS_HUB_ANKI_REHEARSAL_FAILURE_STAGE"] == "card_residual"
    evidence = overlay.root / "rehearsal" / "runtime-evidence"
    evidence.mkdir(parents=True)
    interlock = {
        "schema_version": 1,
        "run_nonce": harness._runtime_evidence_nonce,
        "pid": 41,
        "stage": "card_residual",
        "event": "begun",
        "occurrence": 1,
        "call_index": 17,
        "subcall_ordinal": 0,
    }
    (evidence / "provider-fault-interlock.json").write_text(json.dumps(interlock), encoding="utf-8")
    harness._consume_failure_injection(overlay, interlock)  # type: ignore[arg-type]
    second = harness._environment(overlay, manifest)  # type: ignore[arg-type]
    assert "OMS_HUB_ANKI_REHEARSAL_FAILURE_STAGE" not in second
    assert not (evidence / "provider-fault-interlock.json").exists()
    assert json.loads((evidence / "provider-fault-interlock.initial.json").read_text()) == interlock


def test_begun_recovery_uses_append_only_cutoff_and_stable_provider_material(
    tmp_path: Path,
) -> None:
    job = SimpleNamespace(
        configuration_sha256="a" * 64,
        pipeline_contract_version=SimpleNamespace(value="card_centric_v2"),
        model_config_sha256="b" * 64,
        source_revision_hashes={1: "c" * 64},
        index_snapshot_id="snapshot",
        companion_generation="companion",
        semantic_generation="semantic",
        source_index_generation="source-index",
    )
    stable_namespace = _replay_namespace_sha256(job)
    material = {
        "stage": "card_residual",
        "kind": "primary",
        "batch_index": 0,
        "batch_note_ids_sha256": "d" * 64,
        "subcall_ordinal": 0,
        "provider": "openai",
        "model": "fixture",
        "instruction_sha256": "e" * 64,
        "input_sha256": "f" * 64,
        "output_schema_sha256": "0" * 64,
        "generation_parameters_sha256": "1" * 64,
        "cache_prefix_sha256": None,
    }
    # The audit request hash and execution fields intentionally change after
    # restart; frozen provider material identifies one logical dispatch.
    precrash = material | {
        "id": 41,
        "stage_attempt": 1,
        "mode": "shadow",
        "call_index": 17,
        "request_sha256": "2" * 64,
        "event": "begun",
    }
    restarted = [
        material
        | {
            "id": ordinal,
            "stage_attempt": 2,
            "mode": "canonical",
            "call_index": 23,
            "request_sha256": "3" * 64,
            "event": event,
        }
        for ordinal, event in (
            (42, "begun"),
            (43, "dispatched"),
            (44, "response_received"),
            (45, "accepted"),
        )
    ]
    rows = [precrash, *restarted]
    assert _stable_logical_call_ids([precrash], stable_namespace) == _stable_logical_call_ids(
        restarted, stable_namespace
    )
    repository = SimpleNamespace(
        require_no_indeterminate_provider_attempt=lambda *_args: None,
        require_job=lambda _job_id: job,
        list_provider_attempt_events=lambda _job_id: rows,
    )
    harness = ProcessRehearsal(_request(tmp_path))
    harness._assert_provider_ledger_is_restart_safe(
        repository,
        UUID(int=1),
        expected_precrash_logical_identities=_stable_logical_call_ids([precrash], stable_namespace),
        precrash_event_id_cutoff=41,
        fault_interlock={"stage": "card_residual", "call_index": 17, "subcall_ordinal": 0},
    )
    proof = next(
        item
        for item in harness._timeline
        if item["event"] == "begun_recovery_stable_identity_verified"
    )
    assert proof["post_restart_events"] == 4
    assert proof["target_dispatches"] == 1
    assert proof["target_final_outcome"] == "accepted"


def test_begun_recovery_accept_then_contract_failure_has_one_final_outcome(
    tmp_path: Path,
) -> None:
    job, precrash, rows = _begun_recovery_rows(
        ("begun", "dispatched", "response_received", "accepted", "contract_failed")
    )
    repository = _recovery_repository(job, rows)
    stable_namespace = _replay_namespace_sha256(job)
    harness = ProcessRehearsal(_request(tmp_path))
    harness._assert_provider_ledger_is_restart_safe(
        repository,
        UUID(int=1),
        expected_precrash_logical_identities=_stable_logical_call_ids([precrash], stable_namespace),
        precrash_event_id_cutoff=41,
        fault_interlock={"stage": "card_residual", "call_index": 17, "subcall_ordinal": 0},
    )
    proof = next(
        item
        for item in harness._timeline
        if item["event"] == "begun_recovery_stable_identity_verified"
    )
    assert proof["target_dispatches"] == 1
    assert proof["target_final_outcome"] == "contract_failed"


def test_begun_recovery_rejects_duplicate_dispatches_with_different_outcomes(
    tmp_path: Path,
) -> None:
    job, precrash, rows = _begun_recovery_rows(
        (
            "begun",
            "dispatched",
            "response_received",
            "accepted",
            "begun",
            "dispatched",
            "response_received",
            "contract_failed",
        )
    )
    stable_namespace = _replay_namespace_sha256(job)
    with pytest.raises(RuntimeError, match="duplicated provider dispatch"):
        ProcessRehearsal(_request(tmp_path))._assert_provider_ledger_is_restart_safe(
            _recovery_repository(job, rows),
            UUID(int=1),
            expected_precrash_logical_identities=_stable_logical_call_ids(
                [precrash], stable_namespace
            ),
            precrash_event_id_cutoff=41,
            fault_interlock={"stage": "card_residual", "call_index": 17, "subcall_ordinal": 0},
        )


def test_begun_recovery_rejects_duplicate_non_target_provider_dispatches(
    tmp_path: Path,
) -> None:
    job, precrash, rows = _begun_recovery_rows(
        ("begun", "dispatched", "response_received", "accepted")
    )
    non_target = {
        key: value
        for key, value in rows[1].items()
        if key not in {"id", "stage_attempt", "mode", "call_index", "request_sha256", "event"}
    }
    rows.extend(
        non_target
        | {
            "id": 100 + attempt * 10 + ordinal,
            "stage_attempt": attempt,
            "mode": "canonical",
            "call_index": 99,
            "request_sha256": str(attempt) * 64,
            "event": event,
            "batch_index": 1,
        }
        for attempt, events in (
            (3, ("begun", "dispatched", "response_received", "accepted")),
            (4, ("begun", "dispatched", "response_received", "contract_failed")),
        )
        for ordinal, event in enumerate(events)
    )
    stable_namespace = _replay_namespace_sha256(cast(Any, job))
    with pytest.raises(RuntimeError, match="duplicated provider dispatch"):
        ProcessRehearsal(_request(tmp_path))._assert_provider_ledger_is_restart_safe(
            cast(Any, _recovery_repository(job, rows)),
            UUID(int=1),
            expected_precrash_logical_identities=_stable_logical_call_ids(
                [precrash], stable_namespace
            ),
            precrash_event_id_cutoff=41,
            fault_interlock={"stage": "card_residual", "call_index": 17, "subcall_ordinal": 0},
        )


@pytest.mark.parametrize("invalid_terminal", ("validation_failed", "transport_failed"))
def test_begun_recovery_rejects_invalid_post_accepted_transition(
    tmp_path: Path, invalid_terminal: str
) -> None:
    job, precrash, rows = _begun_recovery_rows(
        ("begun", "dispatched", "response_received", "accepted", invalid_terminal)
    )
    stable_namespace = _replay_namespace_sha256(cast(Any, job))
    with pytest.raises(RuntimeError, match="provider lifecycle is invalid"):
        ProcessRehearsal(_request(tmp_path))._assert_provider_ledger_is_restart_safe(
            cast(Any, _recovery_repository(job, rows)),
            UUID(int=1),
            expected_precrash_logical_identities=_stable_logical_call_ids(
                [precrash], stable_namespace
            ),
            precrash_event_id_cutoff=41,
            fault_interlock={"stage": "card_residual", "call_index": 17, "subcall_ordinal": 0},
        )


def test_begun_recovery_rejects_missing_terminal_outcome(tmp_path: Path) -> None:
    job, precrash, rows = _begun_recovery_rows(("begun", "dispatched", "response_received"))
    stable_namespace = _replay_namespace_sha256(job)
    with pytest.raises(RuntimeError, match="lacks a terminal outcome"):
        ProcessRehearsal(_request(tmp_path))._assert_provider_ledger_is_restart_safe(
            _recovery_repository(job, rows),
            UUID(int=1),
            expected_precrash_logical_identities=_stable_logical_call_ids(
                [precrash], stable_namespace
            ),
            precrash_event_id_cutoff=41,
            fault_interlock={"stage": "card_residual", "call_index": 17, "subcall_ordinal": 0},
        )


def test_child_health_must_attest_exact_launched_identity(tmp_path: Path) -> None:
    harness = ProcessRehearsal(_request(tmp_path))
    harness._source_tree_sha256 = "c" * 64
    health = {
        "rehearsal_nonce": harness._runtime_evidence_nonce,
        "rehearsal_pid": "41",
        "rehearsal_source": str((tmp_path / "implementation/src").resolve()),
        "rehearsal_source_tree_sha256": "c" * 64,
        "rehearsal_commit": "a" * 40,
        "rehearsal_tree": "b" * 40,
    }
    harness._validate_child_health(health, 41)
    health["rehearsal_nonce"] = "wrong"
    with pytest.raises(RuntimeError, match="does not identify"):
        harness._validate_child_health(health, 41)


def test_port_preflight_rejects_bound_loopback_port(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from oms_hub.anki.rehearsal import process as process_module

    harness = ProcessRehearsal(_request(tmp_path))

    class BoundPortProbe:
        def __enter__(self) -> BoundPortProbe:
            return self

        def __exit__(self, *_: object) -> None:
            return None

        def setsockopt(self, *_: object) -> None:
            return None

        def bind(self, _address: tuple[str, int]) -> None:
            raise OSError("occupied")

    monkeypatch.setattr(process_module.socket, "socket", lambda *_: BoundPortProbe())
    with pytest.raises(RuntimeError, match="already in use"):
        harness._assert_loopback_port_is_free()


def test_standalone_launcher_is_stdlib_only_until_verified_and_reexecs_isolated() -> None:
    script = Path(__file__).parents[2] / "scripts" / "run-a0-rehearsal.py"
    tree = parse(script.read_text(encoding="utf-8"))
    top_level_imports = [node for node in tree.body if isinstance(node, (Import, ImportFrom))]
    assert all(
        not (
            isinstance(node, ImportFrom)
            and node.module is not None
            and node.module.startswith("oms_hub")
        )
        for node in top_level_imports
    )
    source = script.read_text(encoding="utf-8")
    assert '"-I"' in source and '"-S"' in source
    assert "os.execve" not in source
    assert "subprocess.run(" in source
    assert "_ISOLATED_BOOTSTRAP" in source
    assert '"attestation_b64"' in source
    assert 'parser.add_argument("--isolated-' not in source
    assert source.index("_verify_implementation_identity(args)") < source.index(
        "from oms_hub.anki.rehearsal.process"
    )


def test_standalone_launcher_carries_attested_dependencies_through_base_isolation(
    tmp_path: Path,
) -> None:
    script = Path(__file__).parents[2] / "scripts" / "run-a0-rehearsal.py"
    launcher = runpy.run_path(str(script), run_name="launcher_test")
    base_runtime, attestation = launcher["_capture_isolated_runtime"](Path(sys.executable))
    dependency_root = tmp_path / "attested-site-packages"
    controlled_dependency = dependency_root / "attested_synthetic_dependency"
    controlled_dependency.mkdir(parents=True)
    (controlled_dependency / "__init__.py").write_text(
        "VALUE = 'attested-test-dependency'\n", encoding="utf-8"
    )
    attestation = dict(attestation, dependency_paths=[str(dependency_root)])
    dependency_paths = attestation["dependency_paths"]
    encoded, dependency_sha256 = launcher["_attestation_transport"](attestation)
    code = "".join(
        (
            "import importlib,json,runpy,sys; from pathlib import Path; ",
            "module=runpy.run_path(sys.argv[1],run_name='isolated_launcher_test'); ",
            "paths=module['_validate_isolated_runtime'](\n"
            "sys.argv[2],sys.argv[3],Path(sys.argv[4])\n"
            "); ",
            "sys.path[:0]=paths; dependency=importlib.import_module("
            "'attested_synthetic_dependency'); "
            "print(json.dumps({'paths':paths,'dependency':str(Path(dependency.__file__).resolve()),"
            "'value':dependency.VALUE}))",
        )
    )
    completed = subprocess.run(
        [
            str(base_runtime),
            "-I",
            "-S",
            "-c",
            code,
            str(script),
            encoded,
            dependency_sha256,
            sys.executable,
        ],
        check=True,
        capture_output=True,
        text=True,
        env={**os.environ, "PYTHONPATH": str(tmp_path / "ambient")},
    )
    payload = json.loads(completed.stdout)
    assert payload["paths"] == dependency_paths
    assert str(dependency_root.resolve()) in payload["paths"]
    assert payload["dependency"] == str((controlled_dependency / "__init__.py").resolve())
    assert payload["value"] == "attested-test-dependency"
    mismatched_runtime = dict(attestation, runtime_version="untrusted runtime build")
    mismatched_encoded, mismatched_digest = launcher["_attestation_transport"](mismatched_runtime)
    mismatched = subprocess.run(
        [
            str(base_runtime),
            "-I",
            "-S",
            "-c",
            code,
            str(script),
            mismatched_encoded,
            mismatched_digest,
            sys.executable,
        ],
        check=False,
        capture_output=True,
        text=True,
        env={**os.environ, "PYTHONPATH": str(tmp_path / "ambient")},
    )
    assert mismatched.returncode != 0
    assert "runtime version" in mismatched.stderr


def test_standalone_launcher_rejects_tampered_or_unknown_dependency_transport(
    tmp_path: Path,
) -> None:
    script = Path(__file__).parents[2] / "scripts" / "run-a0-rehearsal.py"
    launcher = runpy.run_path(str(script), run_name="launcher_test")
    _runtime, attestation = launcher["_capture_isolated_runtime"](Path(sys.executable))
    encoded, digest = launcher["_attestation_transport"](attestation)
    with pytest.raises(ValueError, match="integrity"):
        launcher["_decode_attestation"](encoded, "0" * 64)
    unknown_attestation = dict(attestation, dependency_paths=["/no/such/trusted-dependency"])
    unknown, unknown_digest = launcher["_attestation_transport"](unknown_attestation)
    with pytest.raises(ValueError, match="unavailable"):
        paths = launcher["_decode_attestation"](unknown, unknown_digest)
        launcher["_canonical_dependency_paths"](paths["dependency_paths"])
    dependency = tmp_path / "site-packages"
    dependency.mkdir()
    assert launcher["_canonical_dependency_paths"]([str(dependency), str(dependency)]) == [
        str(dependency.resolve())
    ]
    link = tmp_path / "indirect-site-packages"
    try:
        os.symlink(dependency, link)
    except OSError as exc:
        pytest.skip(f"symbolic links unavailable: {exc}")
    with pytest.raises(ValueError, match="indirect"):
        launcher["_canonical_dependency_paths"]([str(link)])


def test_standalone_launcher_has_no_public_child_only_arguments() -> None:
    script = Path(__file__).parents[2] / "scripts" / "run-a0-rehearsal.py"
    launcher = runpy.run_path(str(script), run_name="launcher_test")
    parser = launcher["_parser"]()
    assert not {
        option
        for action in parser._actions
        for option in action.option_strings
        if option.startswith("--isolated-")
    }


def test_standalone_launcher_forwards_optional_run_goal_without_changing_defaults(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    script = Path(__file__).parents[2] / "scripts" / "run-a0-rehearsal.py"
    launcher = runpy.run_path(str(script), run_name="launcher_test")
    parser = launcher["_parser"]()
    required = [
        "--capsule",
        "capsule",
        "--overlay",
        "overlay",
        "--mode",
        "deterministic",
        "--port",
        "8788",
        "--evidence-zip",
        "evidence.zip",
        "--failed-job-id",
        "12345678-1234-5678-1234-567812345678",
        "--expected-manifest-sha256",
        "0" * 64,
        "--implementation-repository",
        "implementation",
        "--expected-implementation-commit",
        "a" * 40,
        "--expected-implementation-tree",
        "b" * 40,
        "--trusted-python",
        str(Path(sys.executable)),
    ]
    default_request = RehearsalRequest(**vars(parser.parse_args(required)))
    assert default_request.run_goal == "golden"
    assert default_request.restart_after_durable_boundary is True
    smoke_without_no_restart = RehearsalRequest(
        **vars(parser.parse_args([*required, "--run-goal", "first_replay_miss"]))
    )
    assert smoke_without_no_restart.run_goal == "first_replay_miss"
    assert smoke_without_no_restart.restart_after_durable_boundary is True
    supplement, supplement_sha256 = _replay_supplement(tmp_path)
    capsule = tmp_path / "capsule"
    capsule.mkdir()
    (capsule / "capsule.json").write_text("{}", encoding="utf-8")
    capsule_sha256 = hashlib.sha256((capsule / "capsule.json").read_bytes()).hexdigest()
    mapped_smoke_without_restart = RehearsalRequest(
        **vars(
            parser.parse_args(
                [
                    *required,
                    "--capsule",
                    str(capsule),
                    "--overlay",
                    str(tmp_path / "mapped-overlay"),
                    "--evidence-zip",
                    str(tmp_path / "mapped.zip"),
                    "--expected-manifest-sha256",
                    capsule_sha256,
                    "--replay-supplement",
                    str(supplement),
                    "--expected-replay-supplement-manifest-sha256",
                    supplement_sha256,
                    "--run-goal",
                    "first_replay_miss",
                ]
            )
        )
    )
    monkeypatch.setattr(ProcessRehearsal, "_verify_implementation_identity", lambda self: None)
    monkeypatch.setattr(process_module, "verify_capsule", lambda _capsule: SimpleNamespace())
    with pytest.raises(ValueError, match="disable restart"):
        ProcessRehearsal(mapped_smoke_without_restart)._validate_destinations()
    smoke_request = RehearsalRequest(
        **vars(parser.parse_args([*required, "--run-goal", "first_replay_miss", "--no-restart"]))
    )
    assert smoke_request.run_goal == "first_replay_miss"
    assert smoke_request.restart_after_durable_boundary is False


def test_standalone_launcher_full_reexec_uses_venv_dependencies_and_propagates_exit(
    tmp_path: Path,
) -> None:
    script = Path(__file__).parents[2] / "scripts" / "run-a0-rehearsal.py"
    launcher_venv = tmp_path / "launcher-venv"
    subprocess.run(
        [sys.executable, "-m", "venv", "--copies", "--without-pip", str(launcher_venv)],
        check=True,
        capture_output=True,
        text=True,
    )
    launcher_python = launcher_venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    if os.name != "nt":
        library_name = sysconfig.get_config_var("LDLIBRARY")
        assert isinstance(library_name, str)
        source_library = Path(sys.base_prefix) / "lib" / library_name
        assert source_library.is_file()
        target_library = launcher_venv / "lib" / library_name
        target_library.parent.mkdir(exist_ok=True)
        shutil.copy2(source_library, target_library)
    assert launcher_python.is_file()
    assert launcher_python.resolve() != Path(sys._base_executable).resolve()
    purelib = Path(
        subprocess.run(
            [
                str(launcher_python),
                "-c",
                "import sysconfig;print(sysconfig.get_paths()['purelib'])",
            ],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
    )
    pydantic = purelib / "pydantic"
    pydantic.mkdir()
    (pydantic / "__init__.py").write_text(
        "__version__ = 'transported-dependency'\n", encoding="utf-8"
    )
    repository = tmp_path / "implementation"
    process_module = repository / "src" / "oms_hub" / "anki" / "rehearsal" / "process.py"
    process_module.parent.mkdir(parents=True)
    (repository / ".gitignore").write_text("__pycache__/\n", encoding="utf-8")
    for package in (
        process_module.parents[3] / "__init__.py",
        process_module.parents[2] / "__init__.py",
        process_module.parents[1] / "__init__.py",
        process_module.parent / "__init__.py",
    ):
        package.write_text("", encoding="utf-8")
    process_module.write_text(
        "import pydantic\n"
        "class RehearsalRequest:\n"
        "    def __init__(self, **kwargs): self.kwargs = kwargs\n"
        "class ProcessRehearsal:\n"
        "    def __init__(self, request): self.request = request\n"
        "    def run(self):\n"
        "        if not self.request.kwargs['trusted_dependency_paths']:\n"
        "            raise SystemExit(24)\n"
        "        if not any('site-packages' in str(path) for path in "
        "self.request.kwargs['trusted_dependency_paths']):\n"
        "            raise SystemExit(25)\n"
        "        if self.request.kwargs['mode'] == 'shadow': raise SystemExit(23)\n"
        "        return type('Result', (), {\n"
        "            'job_id': pydantic.__version__,\n"
        "            'overlay': 'overlay',\n"
        "            'evidence_zip': 'evidence',\n"
        "            'run_goal': self.request.kwargs['run_goal'],\n"
        "            'outcome': 'golden_success',\n"
        "        })()\n",
        encoding="utf-8",
    )
    for command in (
        ["git", "init", str(repository)],
        ["git", "-C", str(repository), "add", "src", ".gitignore"],
        [
            "git",
            "-C",
            str(repository),
            "-c",
            "user.email=test@example.invalid",
            "-c",
            "user.name=Test",
            "commit",
            "-m",
            "fixture",
        ],
    ):
        subprocess.run(command, check=True, capture_output=True, text=True)
    commit = subprocess.run(
        ["git", "-C", str(repository), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    tree = subprocess.run(
        ["git", "-C", str(repository), "rev-parse", "HEAD^{tree}"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    common = [
        "--capsule",
        str(tmp_path / "capsule"),
        "--overlay",
        str(tmp_path / "overlay"),
        "--port",
        "8765",
        "--evidence-zip",
        str(tmp_path / "evidence.zip"),
        "--failed-job-id",
        str(UUID(int=1)),
        "--expected-manifest-sha256",
        "0" * 64,
        "--implementation-repository",
        str(repository),
        "--expected-implementation-commit",
        commit,
        "--expected-implementation-tree",
        tree,
        "--trusted-python",
        str(launcher_python),
    ]
    success = subprocess.run(
        [str(launcher_python), str(script), "--mode", "deterministic", *common],
        check=False,
        capture_output=True,
        text=True,
        env={**os.environ, "PYTHONPATH": str(tmp_path / "ambient")},
    )
    assert success.returncode == 0
    assert "job_id=transported-dependency" in success.stdout
    assert "run_goal=golden outcome=golden_success" in success.stdout
    assert "PYTHONPATH" not in success.stderr
    forged_environment = {
        **os.environ,
        "OMS_HUB_A0_REHEARSAL_INTERNAL_NONCE": "forged-old-launcher-marker",
    }
    forged = subprocess.run(
        [
            str(launcher_python),
            str(script),
            "--isolated-launch",
            "--isolated-attestation-b64",
            "forged",
            "--isolated-attestation-sha256",
            "forged",
            "--mode",
            "deterministic",
            *common,
        ],
        check=False,
        capture_output=True,
        text=True,
        env=forged_environment,
    )
    assert forged.returncode != 0
    assert "unrecognized arguments" in forged.stderr
    duplicate = subprocess.run(
        [
            str(launcher_python),
            str(script),
            "--isolated-launch",
            "--isolated-launch",
            "--mode",
            "deterministic",
            *common,
        ],
        check=False,
        capture_output=True,
        text=True,
        env=forged_environment,
    )
    assert duplicate.returncode != 0
    assert "unrecognized arguments" in duplicate.stderr
    failed = subprocess.run(
        [str(launcher_python), str(script), "--mode", "shadow", *common],
        check=False,
        capture_output=True,
        text=True,
    )
    assert failed.returncode == 23


def test_implementation_identity_rejects_commit_mismatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = ProcessRehearsal(_request(tmp_path))
    monkeypatch.setattr(
        ProcessRehearsal,
        "_git_output",
        staticmethod(lambda _repository, *args: "c" * 40 if args[0] == "rev-parse" else ""),
    )
    with pytest.raises(ValueError, match="commit"):
        harness._verify_implementation_identity()


def test_implementation_identity_rejects_dirty_repository(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = ProcessRehearsal(_request(tmp_path))

    def git_output(_repository: Path, *args: str) -> str:
        if args == ("rev-parse", "HEAD"):
            return "a" * 40
        if args == ("rev-parse", "HEAD^{tree}"):
            return "b" * 40
        return " M src/owned.py"

    monkeypatch.setattr(ProcessRehearsal, "_git_output", staticmethod(git_output))
    with pytest.raises(ValueError, match="clean"):
        harness._verify_implementation_identity()


def test_restart_stops_child_after_observed_boundary(tmp_path: Path) -> None:
    class FakeProcess:
        pid = 12
        returncode: int | None = None

        def poll(self) -> int | None:
            return self.returncode

        def terminate(self) -> None:
            self.returncode = 0

        def wait(self, timeout: float) -> int:
            del timeout
            return 0

    process = FakeProcess()
    harness = ProcessRehearsal(_request(tmp_path))
    from oms_hub.anki.rehearsal.process import ProcessObservation, _Child

    stdout_path = tmp_path / "stdout.log"
    stderr_path = tmp_path / "stderr.log"
    stdout = stdout_path.open("wb")
    stderr = stderr_path.open("wb")
    harness._children.append(
        _Child(
            process,
            ProcessObservation(12, None, "start", None, None, ("oms-hub", "serve")),
            stdout_path,
            stderr_path,
            stdout,
            stderr,
        )
    )  # type: ignore[arg-type]

    class FakeRepository:
        def list_provider_attempt_events(self, job_id: UUID) -> list[dict[str, object]]:
            del job_id
            return [
                {
                    "stage": "card_ledger",
                    "stage_attempt": 1,
                    "mode": "canonical",
                    "call_index": 0,
                    "event": "accepted",
                }
            ]

    harness._restart_after_durable_boundary(
        FakeRepository(), UUID(int=1), {"state": "card_building_ledger"}
    )  # type: ignore[arg-type]
    assert process.returncode == 0
    assert any(
        item["event"] == "restart_after_observed_durable_boundary" for item in harness._timeline
    )


def test_unchanged_review_and_apply_423_shape() -> None:
    payload = unchanged_review_payload(
        {
            "job": {"review_revision": 2},
            "groups": {
                "pass_1_matches": [{"note_id": 3, "selected": True}],
                "recovered_in_pass_2": [],
                "generated_cards": [],
            },
        }
    )
    assert payload["expected_revision"] == 2
    assert payload["candidate_selections"] == {"3": True}


def test_envelope_response_requires_created_status_and_complete_identity() -> None:
    with pytest.raises(RuntimeError, match="HTTP 409"):
        ProcessRehearsal._validate_envelope_response(  # type: ignore[arg-type]
            None, UUID(int=1), 2, 409, {"detail": "stale"}
        )
    with pytest.raises(RuntimeError, match="malformed or mismatched"):
        ProcessRehearsal._validate_envelope_response(  # type: ignore[arg-type]
            None,
            UUID(int=1),
            2,
            201,
            {
                "job_id": str(UUID(int=2)),
                "envelope_id": str(UUID(int=3)),
                "payload_sha256": "a" * 64,
                "summary": {},
                "reconciliation": {},
            },
        )


def test_envelope_response_rejects_altered_nested_reconciliation_document() -> None:
    job_id = UUID(int=1)
    envelope_id = UUID(int=2)
    reconciliation = {
        "contract_version": "card_centric_s9_v1",
        "can_render_envelope": True,
        "snapshot": {"nested": {"selection": ["one", "two"]}},
        "selection": {"overflow_acknowledgement": {"required": False}},
    }
    persisted = SimpleNamespace(
        review_revision=2,
        job_id=job_id,
        reconciliation_contract_version="card_centric_s9_v1",
        overflow_acknowledgement_provenance={"required": False},
        operations=(),
    )
    repository = SimpleNamespace(
        get_job_envelope=lambda _job_id: SimpleNamespace(
            id=envelope_id,
            job_id=job_id,
            payload_sha256="a" * 64,
        ),
        get_envelope=lambda _envelope_id: persisted,
        reviewed_reconciliation=lambda _job_id, _revision: reconciliation,
    )
    body = {
        "job_id": str(job_id),
        "envelope_id": str(envelope_id),
        "payload_sha256": "a" * 64,
        "summary": {
            "notes_created": 0,
            "existing_notes_retagged": 0,
            "tags_added": 0,
            "tags_removed": 0,
        },
        "reconciliation": reconciliation,
    }
    ProcessRehearsal._validate_envelope_response(repository, job_id, 2, 201, body)
    body["reconciliation"] = {
        **reconciliation,
        "snapshot": {"nested": {"selection": ["one", "altered"]}},
    }
    with pytest.raises(RuntimeError, match="exactly match"):
        ProcessRehearsal._validate_envelope_response(repository, job_id, 2, 201, body)


def test_evidence_redacts_secrets_and_has_hash_manifest(tmp_path: Path) -> None:
    destination = tmp_path / "evidence.zip"
    _write_deterministic_zip(
        destination,
        {
            "record.json": {
                "api_token": "do-not-leak",
                "provider_error": '{"refresh_token":"do-not-leak,still-not"}',
                "ok": True,
            }
        },
    )
    with zipfile.ZipFile(destination) as archive:
        record = archive.read("record.json")
        manifest = json.loads(archive.read("sha256-manifest.json"))
    assert b"do-not-leak" not in record
    assert b"still-not" not in record
    assert manifest["record.json"] == hashlib.sha256(record).hexdigest()
    evidence = _environment_evidence({"API_TOKEN": "do-not-leak", "OMS_HUB_DATA_DIR": "/tmp/x"})
    assert evidence["API_TOKEN"]["value"] == "[REDACTED]"


def test_result_truthfully_reports_missing_native_evidence(tmp_path: Path) -> None:
    result = RehearsalResult(UUID(int=1), tmp_path / "overlay", tmp_path / "evidence.zip")
    assert result.native_gate_complete is False
    assert result.local_execution_evidence == "Local actual-process rehearsal execution performed"
    assert result.missing_evidence == (
        "Native NUC/Windows PowerShell capsule export pending",
        "Native NUC/Windows capsule execution pending",
    )


def test_runtime_evidence_rejects_nonce_mismatch_and_malformed_sequences() -> None:
    adapter = {
        "schema_version": 1,
        "run_nonce": "correct",
        "records": [],
    }
    with pytest.raises(RuntimeError, match="stale or malformed"):
        _validate_adapter_ledger(adapter, "wrong")
    egress = {
        "schema_version": 1,
        "run_nonce": "correct",
        "mode": "deterministic",
        "records": [
            {
                "kind": "startup",
                "mode": "deterministic",
                "host": None,
                "port": None,
                "resolved_address": None,
                "allowed": None,
                "ordinal": 2,
                "timestamp": "now",
            }
        ],
    }
    with pytest.raises(RuntimeError, match="invalid sequence"):
        _validate_egress_ledger(egress, "correct", "deterministic")


def test_runtime_evidence_rejects_non_list_crash_ledger_records() -> None:
    malformed_adapter = {
        "schema_version": 1,
        "run_nonce": "correct",
        "records": {"not": "a-list"},
    }
    with pytest.raises(RuntimeError, match="stale or malformed"):
        _validate_adapter_ledger(malformed_adapter, "correct")


def test_successful_runtime_evidence_rejects_denied_egress_authorization(tmp_path: Path) -> None:
    harness = ProcessRehearsal(_request(tmp_path, runtime_evidence_nonce="nonce"))
    evidence = tmp_path / "overlay/rehearsal/runtime-evidence"
    evidence.mkdir(parents=True)
    (evidence / "read-only-anki-mutation-ledger.json").write_text(
        json.dumps({"schema_version": 1, "run_nonce": "nonce", "records": []}),
        encoding="utf-8",
    )
    (evidence / "egress-decisions.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "run_nonce": "nonce",
                "mode": "deterministic",
                "records": [
                    {
                        "kind": "startup",
                        "mode": "deterministic",
                        "host": None,
                        "port": None,
                        "resolved_address": None,
                        "allowed": None,
                        "ordinal": 1,
                        "timestamp": "now",
                    },
                    {
                        "kind": "authorization",
                        "mode": "deterministic",
                        "host": "example.invalid",
                        "port": 443,
                        "resolved_address": None,
                        "allowed": False,
                        "ordinal": 2,
                        "timestamp": "now",
                    },
                    {
                        "kind": "shutdown",
                        "mode": "deterministic",
                        "host": None,
                        "port": None,
                        "resolved_address": None,
                        "allowed": None,
                        "ordinal": 3,
                        "timestamp": "now",
                    },
                ],
            }
        ),
        encoding="utf-8",
    )
    overlay = SimpleNamespace(root=tmp_path / "overlay")
    with pytest.raises(RuntimeError, match="denied or forbidden egress"):
        harness._validate_runtime_evidence(overlay)  # type: ignore[arg-type]


def test_crash_egress_authorization_gate_rejects_denial_but_allows_lifecycle_markers() -> None:
    records: list[object] = [
        {"kind": "startup", "allowed": None},
        {"kind": "authorization", "allowed": False},
    ]
    with pytest.raises(RuntimeError, match="crash evidence records denied or forbidden"):
        _require_no_denied_egress_authorizations(records, "crash")
    assert (
        _require_no_denied_egress_authorizations(
            [{"kind": "startup", "allowed": None}, {"kind": "shutdown", "allowed": None}],
            "crash",
        )
        == []
    )


def test_runtime_evidence_missing_file_fails_closed(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError, match="runtime evidence is missing"):
        _load_runtime_ledger(tmp_path / "runtime-evidence/egress-decisions.json")
