from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from oms_hub.controlled_restart import (
    CONTROLLED_RESTART_EXIT_CODE,
    ControlledRestartController,
    ControlledRestartError,
    ControlledRestartRequest,
    validate_gate_directory,
)

REVISION = "1" * 40
TREE = "2" * 40
SCHEMA = 22


class _Supervisor:
    def __init__(self, *, quiesce_result: bool = True) -> None:
        self.quiesce_result = quiesce_result
        self.quiesce_calls: list[float] = []
        self.resume_calls = 0

    def quiesce(self, timeout_seconds: float) -> bool:
        self.quiesce_calls.append(timeout_seconds)
        return self.quiesce_result

    def resume(self) -> None:
        self.resume_calls += 1


def _request(now: datetime, **overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "schema_version": 1,
        "nonce": "5a9243c3-8992-47bb-bbab-45264e8f262a",
        "expected_revision": REVISION,
        "expected_tree": TREE,
        "expected_schema": SCHEMA,
        "exit_code": CONTROLLED_RESTART_EXIT_CODE,
        "expires_at": (now + timedelta(minutes=2)).isoformat(),
    }
    payload.update(overrides)
    return payload


def _health() -> dict[str, object]:
    return {
        "status": "ok",
        "build_revision": REVISION,
        "build_tree": TREE,
        "schema_version": SCHEMA,
        "database_reachable": True,
        "workers": {
            name: {
                "alive": True,
                "start_count": 1,
                "active_work_age_seconds": None,
            }
            for name in ("generation_worker", "ingestion_worker", "studio_worker")
        },
    }


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"extra": "value"}, "unknown"),
        ({"schema_version": 2}, "schema"),
        ({"schema_version": True}, "schema"),
        ({"nonce": "not-a-uuid"}, "nonce"),
        ({"expected_revision": "abc"}, "revision"),
        ({"expected_tree": "abc"}, "tree"),
        ({"expected_schema": True}, "schema"),
        ({"exit_code": 1}, "exit code"),
    ],
)
def test_request_contract_rejects_unbounded_input(
    overrides: dict[str, object],
    message: str,
) -> None:
    now = datetime(2026, 8, 10, 20, 0, tzinfo=UTC)

    with pytest.raises(ControlledRestartError, match=message):
        ControlledRestartRequest.from_mapping(_request(now, **overrides), now=now)


def test_request_contract_rejects_expired_or_long_lived_request() -> None:
    now = datetime(2026, 8, 10, 20, 0, tzinfo=UTC)

    with pytest.raises(ControlledRestartError, match="expired"):
        ControlledRestartRequest.from_mapping(
            _request(now, expires_at=(now - timedelta(seconds=1)).isoformat()),
            now=now,
        )
    with pytest.raises(ControlledRestartError, match="five minutes"):
        ControlledRestartRequest.from_mapping(
            _request(now, expires_at=(now + timedelta(minutes=6)).isoformat()),
            now=now,
        )


def test_gate_directory_must_be_exact_and_cannot_traverse_a_symlink(tmp_path: Path) -> None:
    data_root = tmp_path / "data"
    expected = data_root / "acceptance" / "f28"
    expected.mkdir(parents=True)

    assert validate_gate_directory(data_root, expected) == expected.resolve()
    with pytest.raises(ControlledRestartError, match="exactly"):
        validate_gate_directory(data_root, tmp_path / "elsewhere")

    linked = tmp_path / "linked"
    linked.symlink_to(data_root, target_is_directory=True)
    with pytest.raises(ControlledRestartError, match="link or reparse"):
        validate_gate_directory(linked, linked / "acceptance" / "f28")


def test_controller_arms_fires_and_records_one_exact_native_exit(tmp_path: Path) -> None:
    now = datetime(2026, 8, 10, 20, 0, tzinfo=UTC)
    data_root = tmp_path / "data"
    gate = data_root / "acceptance" / "f28"
    gate.mkdir(parents=True)
    (gate / "request.json").write_text(
        json.dumps(_request(now), sort_keys=True),
        encoding="utf-8",
    )
    supervisor = _Supervisor()
    server = SimpleNamespace(should_exit=False)
    controller = ControlledRestartController(
        gate_directory=gate,
        data_directory=data_root,
        expected_revision=REVISION,
        expected_tree=TREE,
        expected_schema=SCHEMA,
        supervisor=supervisor,
        server=server,
        readiness_probe=_health,
        now=lambda: now,
    )

    controller.poll_once()
    assert not list(gate.glob("armed-*.json"))
    controller.poll_once()
    armed = next(gate.glob("armed-*.json"))
    armed_sha = hashlib.sha256(armed.read_bytes()).hexdigest()
    (gate / f"fire-{controller.active_nonce}.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "nonce": controller.active_nonce,
                "armed_sha256": armed_sha,
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )

    controller.poll_once()

    assert server.should_exit is True
    assert supervisor.quiesce_calls == [10.0]
    assert supervisor.resume_calls == 0
    assert controller.finalize_server_exit() == CONTROLLED_RESTART_EXIT_CODE
    server_exit = json.loads(
        next(gate.glob("server-exit-*.json")).read_text(encoding="utf-8")
    )
    latest = json.loads((gate / "latest-server-exit.json").read_text(encoding="utf-8"))
    assert server_exit["nonce"] == latest["nonce"] == controller.active_nonce
    assert server_exit["exit_code"] == latest["exit_code"] == CONTROLLED_RESTART_EXIT_CODE
    assert server_exit["expected_revision"] == REVISION
    assert server_exit["expected_tree"] == TREE
    assert not (gate / "request.json").exists()
    assert next(gate.glob("consumed-*.json")).exists()
    finalized = json.loads(
        next(gate.glob("finalized-*.json")).read_text(encoding="utf-8")
    )
    assert finalized["nonce"] == controller.active_nonce
    assert finalized["consumed"] == f"consumed-{controller.active_nonce}.json"
    assert finalized["consumed_sha256"] == hashlib.sha256(
        next(gate.glob("consumed-*.json")).read_bytes()
    ).hexdigest()
    assert finalized["server_exit_sha256"] == hashlib.sha256(
        next(gate.glob("server-exit-*.json")).read_bytes()
    ).hexdigest()
    assert finalized["latest_server_exit_sha256"] == hashlib.sha256(
        (gate / "latest-server-exit.json").read_bytes()
    ).hexdigest()


def test_finalize_does_not_publish_launcher_evidence_when_consume_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime(2026, 8, 10, 20, 0, tzinfo=UTC)
    data_root = tmp_path / "data"
    gate = data_root / "acceptance" / "f28"
    gate.mkdir(parents=True)
    (gate / "request.json").write_text(
        json.dumps(_request(now), sort_keys=True),
        encoding="utf-8",
    )
    server = SimpleNamespace(should_exit=False)
    controller = ControlledRestartController(
        gate_directory=gate,
        data_directory=data_root,
        expected_revision=REVISION,
        expected_tree=TREE,
        expected_schema=SCHEMA,
        supervisor=_Supervisor(),
        server=server,
        readiness_probe=_health,
        now=lambda: now,
    )
    controller.poll_once()
    controller.poll_once()
    armed = next(gate.glob("armed-*.json"))
    (gate / f"fire-{controller.active_nonce}.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "nonce": controller.active_nonce,
                "armed_sha256": hashlib.sha256(armed.read_bytes()).hexdigest(),
            }
        ),
        encoding="utf-8",
    )
    controller.poll_once()
    original_replace = Path.replace

    def fail_active_consume(source: Path, destination: Path) -> Path:
        if source.name == "active.json":
            raise OSError("injected active consume failure")
        return original_replace(source, destination)

    monkeypatch.setattr(Path, "replace", fail_active_consume)

    with pytest.raises(OSError, match="injected active consume failure"):
        controller.finalize_server_exit()

    assert (gate / "active.json").exists()
    assert next(gate.glob("server-exit-*.json")).exists()
    assert not (gate / "latest-server-exit.json").exists()
    assert not list(gate.glob("finalized-*.json"))


def test_controller_rejects_identity_drift_without_exiting(tmp_path: Path) -> None:
    now = datetime(2026, 8, 10, 20, 0, tzinfo=UTC)
    data_root = tmp_path / "data"
    gate = data_root / "acceptance" / "f28"
    gate.mkdir(parents=True)
    (gate / "request.json").write_text(
        json.dumps(_request(now, expected_tree="3" * 40)),
        encoding="utf-8",
    )
    supervisor = _Supervisor()
    server = SimpleNamespace(should_exit=False)
    controller = ControlledRestartController(
        gate_directory=gate,
        data_directory=data_root,
        expected_revision=REVISION,
        expected_tree=TREE,
        expected_schema=SCHEMA,
        supervisor=supervisor,
        server=server,
        readiness_probe=_health,
        now=lambda: now,
    )

    controller.poll_once()

    assert server.should_exit is False
    assert supervisor.quiesce_calls == []
    assert supervisor.resume_calls == 0
    assert next(gate.glob("rejected-*.json")).exists()


def test_controller_resumes_when_armed_request_expires(tmp_path: Path) -> None:
    current = [datetime(2026, 8, 10, 20, 0, tzinfo=UTC)]
    data_root = tmp_path / "data"
    gate = data_root / "acceptance" / "f28"
    gate.mkdir(parents=True)
    (gate / "request.json").write_text(
        json.dumps(
            _request(
                current[0],
                expires_at=(current[0] + timedelta(seconds=1)).isoformat(),
            )
        ),
        encoding="utf-8",
    )
    supervisor = _Supervisor()
    server = SimpleNamespace(should_exit=False)
    controller = ControlledRestartController(
        gate_directory=gate,
        data_directory=data_root,
        expected_revision=REVISION,
        expected_tree=TREE,
        expected_schema=SCHEMA,
        supervisor=supervisor,
        server=server,
        readiness_probe=_health,
        now=lambda: current[0],
    )

    controller.poll_once()
    controller.poll_once()
    current[0] += timedelta(seconds=2)
    controller.poll_once()

    assert server.should_exit is False
    assert supervisor.resume_calls == 1
    assert next(gate.glob("expired-*.json")).exists()


def test_controller_resumes_workers_after_quiesce_timeout(tmp_path: Path) -> None:
    now = datetime(2026, 8, 10, 20, 0, tzinfo=UTC)
    data_root = tmp_path / "data"
    gate = data_root / "acceptance" / "f28"
    gate.mkdir(parents=True)
    (gate / "request.json").write_text(
        json.dumps(_request(now)),
        encoding="utf-8",
    )
    supervisor = _Supervisor(quiesce_result=False)
    server = SimpleNamespace(should_exit=False)
    controller = ControlledRestartController(
        gate_directory=gate,
        data_directory=data_root,
        expected_revision=REVISION,
        expected_tree=TREE,
        expected_schema=SCHEMA,
        supervisor=supervisor,
        server=server,
        readiness_probe=_health,
        now=lambda: now,
    )

    controller.poll_once()

    assert server.should_exit is False
    assert supervisor.resume_calls == 1
    reason = json.loads(
        next(gate.glob("rejected-reason-*.json")).read_text(encoding="utf-8")
    )
    assert reason["reason"] == "worker_quiesce_timeout"
