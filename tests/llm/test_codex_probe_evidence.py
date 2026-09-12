import json
import runpy
from pathlib import Path
from types import SimpleNamespace

import pytest

from oms_hub.llm.codex_session import SessionLifecycle, SessionResult


@pytest.mark.parametrize("raw,exit_code", [("not JSON", 1), ('{"ok": 1}', 1), ('{"ok": true}', 0)])
def test_smoke_retains_raw_and_lifecycle_before_validation(tmp_path, monkeypatch, raw, exit_code):
    probe = runpy.run_path(str(Path(__file__).parents[2] / "scripts/probe-codex-session.py"))
    scope = probe["live_probe"].__globals__
    calls = []

    def generate(request, *, cancelled, on_lifecycle):
        evidence = next(tmp_path.glob("smoke-*"))
        assert (
            json.loads((evidence / "request.json").read_text())["request_id"] == request.request_id
        )
        on_lifecycle(SessionLifecycle(request.request_id, "dispatching"))
        assert json.loads((evidence / "event-000.json").read_text())["phase"] == "dispatching"
        calls.append(request.request_id)
        on_lifecycle(SessionLifecycle(request.request_id, "completed", "thread-test", "turn-test"))
        return SessionResult("thread-test", "turn-test", raw)

    client = SimpleNamespace(
        work_root=tmp_path, generate=generate, close=lambda: calls.append("closed")
    )
    monkeypatch.setitem(scope, "CodexSessionClient", lambda *a, **kw: client)
    args = SimpleNamespace(
        executable=Path("unused"),
        session_home=tmp_path.parent / "session",
        work_root=tmp_path,
        binary_sha256="fixture-pin",
        model="fixture",
        login=False,
    )
    assert probe["live_probe"](args) == exit_code
    evidence = next(tmp_path.glob("smoke-*"))
    assert (evidence / "raw.txt").read_text() == raw
    report = json.loads((evidence / "result.json").read_text())
    assert report["thread_id"] == "thread-test" and report["turn_id"] == "turn-test"
    assert report["synthetic_smoke"] == ("passed" if exit_code == 0 else "failed")
    assert report["live_ready"] is False
    assert len(calls) == 2 and calls[-1] == "closed"


def test_smoke_evidence_write_failure_prevents_dispatch(tmp_path, monkeypatch):
    probe = runpy.run_path(str(Path(__file__).parents[2] / "scripts/probe-codex-session.py"))
    scope = probe["live_probe"].__globals__
    client = SimpleNamespace(
        work_root=tmp_path, generate=lambda *a, **kw: pytest.fail("dispatched"), close=lambda: None
    )
    monkeypatch.setitem(scope, "CodexSessionClient", lambda *a, **kw: client)

    def fail_write(*args):
        raise OSError("fixture disk failure")

    monkeypatch.setitem(scope, "verified_atomic_write", fail_write)
    args = SimpleNamespace(
        executable=Path("unused"),
        session_home=tmp_path.parent / "session",
        work_root=tmp_path,
        binary_sha256="fixture-pin",
        model="fixture",
        login=False,
    )
    assert probe["live_probe"](args) == 1
