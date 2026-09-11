import hashlib
import json
import os
import runpy
import subprocess
import sys
import threading
import time
from dataclasses import replace
from io import BytesIO
from pathlib import Path

import pytest

from oms_hub.llm import codex_session
from oms_hub.llm.codex_session import (
    CodexSessionClient,
    SessionError,
    SessionRequest,
    collect_completed_text,
    model_ready,
)


def test_missing_image_capability_cannot_enable_image_generation():
    assert model_ready({"model": "chosen", "inputModalities": ["text"]}, images=True) is False
    assert model_ready({"model": "chosen"}, images=True) is False
    assert model_ready({"model": "chosen", "inputModalities": ["text", "image"]}, images=True)


@pytest.mark.parametrize("modalities", [None, "text,image", {}, [], ["image"], ["text", 1]])
def test_malformed_or_missing_text_metadata_is_not_ready(modalities):
    assert not model_ready({"model": "chosen", "inputModalities": modalities}, images=False)


def test_text_only_model_is_available_only_for_text():
    assert model_ready({"model": "chosen", "inputModalities": ["text"]}, images=False)
    assert not model_ready({"model": "", "inputModalities": ["text"]}, images=False)


def load_probe():
    return runpy.run_path(str(Path(__file__).parents[2] / "scripts/probe-codex-session.py"))


@pytest.mark.parametrize("argv", [[], ["--login"], ["--smoke"], ["--login", "--smoke"]])
def test_probe_import_and_modes_never_start_processes(monkeypatch, argv):
    def forbidden(*args, **kwargs):
        pytest.fail("offline probe launched a subprocess")

    monkeypatch.setattr(subprocess, "Popen", forbidden)
    probe = load_probe()
    with pytest.raises(SystemExit) as error:
        probe["main"](argv)
    assert error.value.code == 2


def test_probe_rejects_changed_schema_or_version(tmp_path):
    probe = load_probe()
    schema = tmp_path / "schema.json"
    schema.write_text("{}")
    with pytest.raises(ValueError, match="incompatible Codex schema"):
        probe["offline_probe"](schema, probe["PINNED_VERSION"])
    with pytest.raises(ValueError, match="incompatible Codex version"):
        probe["offline_probe"](schema, "codex-cli 0.0.0")


@pytest.mark.parametrize("payload", [{}, {"login_id": "x"}, {"loginId": "x", "token": "x"}])
def test_probe_rejects_drifted_fixture_members(payload):
    check = load_probe()["check_fixture_members"]
    with pytest.raises(ValueError):
        check({"required": ["loginId"], "properties": {"loginId": {"type": "string"}}}, payload)


@pytest.mark.skipif(
    not os.environ.get("CODEX_SESSION_SCHEMA"), reason="local schema export optional"
)
def test_exported_schema_and_fixtures_pass_without_live_readiness(capsys):
    probe = load_probe()
    schema = Path(os.environ["CODEX_SESSION_SCHEMA"])
    report = probe["offline_probe"](schema, probe["PINNED_VERSION"])
    assert report["fixture_member_checks"] == len(probe["FIXTURES"])
    assert report["live_ready"] is False
    assert report["provider_verified"] is False
    assert report["windows_verified"] is False
    assert report["restrictions_verified"] is False
    assert probe["main"](["--schema", str(schema)]) == 0
    assert '"offline_contract": "passed"' in capsys.readouterr().out


def protocol_events():
    return [
        json.loads(line)
        for line in (Path(__file__).parent / "fixtures/codex_session_events.jsonl")
        .read_text()
        .splitlines()
    ]


def test_reducer_only_accepts_matching_completed_final_output():
    assert (
        collect_completed_text(
            protocol_events(), thread_id="thread-fixture", turn_id="turn-fixture"
        )
        == '{"synthetic":true}'
    )


@pytest.mark.parametrize(
    "status,code",
    [(None, "interrupted"), ("failed", "protocol_error"), ("interrupted", "interrupted")],
)
def test_reducer_never_accepts_partial_or_failed_turn(status, code):
    events = protocol_events()
    if status is None:
        events.pop()
    else:
        events[-1]["params"]["turn"]["status"] = status
    with pytest.raises(SessionError) as error:
        collect_completed_text(events, thread_id="thread-fixture", turn_id="turn-fixture")
    assert error.value.code == code


def test_reducer_rejects_unsolicited_tool_request():
    events = [{"id": "tool", "method": "item/commandExecution/requestApproval", "params": {}}]
    with pytest.raises(SessionError) as error:
        collect_completed_text(events, thread_id="thread-fixture", turn_id="turn-fixture")
    assert error.value.code == "tool_request_denied"


@pytest.fixture
def fake_session(tmp_path, monkeypatch):
    clients, wires = [], []
    real_wire = codex_session._Stdio

    def capture(*args, **kwargs):
        wire = real_wire(*args, **kwargs)
        wires.append(wire)
        return wire

    monkeypatch.setattr(codex_session, "_Stdio", capture)

    def create(scenario="happy", *, ready=True, turn_timeout=2, startup_timeout=2):
        root = tmp_path / str(len(clients))
        root.mkdir()
        executable = Path(sys.executable).resolve()
        client = CodexSessionClient(
            executable,
            root / "session",
            root / "work",
            binary_sha256=hashlib.sha256(executable.read_bytes()).hexdigest(),
            turn_timeout=turn_timeout,
            startup_timeout=startup_timeout,
            shutdown_timeout=0.2,
        )
        trace = root / "wire.jsonl"
        client._command = [
            sys.executable,
            str(Path(__file__).parent / "fixtures/codex_fake_server.py"),
            scenario,
            str(trace),
        ]
        if ready:
            monkeypatch.setattr(client, "_require_generation_ready", lambda request: None)
        clients.append(client)
        return client, trace, wires

    yield create
    for client in clients:
        client.close()
    for wire in wires:
        assert wire.process.poll() is not None
        assert not any(thread.is_alive() for thread in wire.threads)


def wire_records(trace):
    return [json.loads(line) for line in trace.read_text().splitlines()] if trace.exists() else []


def request():
    return SessionRequest(
        "run-fixture:batch-1",
        "chosen",
        "Return JSON.",
        "Synthetic source.",
        output_schema={"type": "object"},
    )


def test_real_pipe_transport_commits_lifecycle_before_dispatch(fake_session, monkeypatch):
    client, trace, wires = fake_session()
    monkeypatch.setenv("OPENAI_API_KEY", "must-not-be-inherited")
    committed = []

    def persist(event):
        if event.phase == "dispatching":
            assert not trace.exists()
        if event.phase == "thread_created":
            assert "turn/start" not in [row.get("method") for row in wire_records(trace)]
        committed.append(event)

    result = client.generate(request(), cancelled=lambda: False, on_lifecycle=persist)
    assert result.text == '{"synthetic":true}'
    assert [event.phase for event in committed] == [
        "dispatching",
        "thread_created",
        "turn_started",
        "completed",
    ]
    assert all(event.request_id == "run-fixture:batch-1" for event in committed)
    assert committed[-1].thread_id == "thread-fixture"
    assert committed[-1].turn_id == "turn-fixture"
    assert wire_records(trace)[0]["api_key_inherited"] is False
    assert Path(wire_records(trace)[0]["cwd"]).parent == client.work_root
    assert not Path(wire_records(trace)[0]["cwd"]).exists()
    assert wires[-1].process.poll() is not None


@pytest.mark.parametrize("phase", ["dispatching", "thread_created", "turn_started", "completed"])
def test_callback_failure_never_returns_or_replays_output(fake_session, phase):
    client, trace, wires = fake_session()
    events = []

    def persist(event):
        events.append(event)
        if event.phase == phase:
            raise RuntimeError("PRIVATE repository error")

    with pytest.raises(SessionError) as error:
        client.generate(request(), cancelled=lambda: False, on_lifecycle=persist)
    assert error.value.code == "interrupted"
    assert "PRIVATE" not in str(error.value)
    methods = [row.get("method") for row in wire_records(trace)]
    if phase == "dispatching":
        assert not wires and not methods
    else:
        assert events[-1].phase == "interrupted"
        assert events[-1].thread_id == "thread-fixture"
    if phase in ("turn_started", "completed"):
        assert events[-1].turn_id == "turn-fixture"
        assert methods.count("turn/start") == 1
        assert "turn/interrupt" in methods
    assert not client._lock.locked()


@pytest.mark.parametrize(
    "scenario,code",
    [
        ("eof", "interrupted"),
        ("hang", "timeout"),
        ("malformed", "protocol_error"),
        ("approval", "tool_request_denied"),
        ("tool_started", "tool_request_denied"),
        ("large", "protocol_error"),
        ("aggregate", "protocol_error"),
        ("failed", "rate_limited"),
        ("bad_json", "invalid_output"),
    ],
)
def test_pipe_failures_reap_process_and_retain_known_ids(fake_session, scenario, code):
    client, trace, wires = fake_session(scenario, turn_timeout=0.5)
    events = []
    with pytest.raises(SessionError) as error:
        client.generate(request(), cancelled=lambda: False, on_lifecycle=events.append)
    assert error.value.code == code
    assert "PRIVATE" not in str(error.value)
    assert events[-1].thread_id == "thread-fixture"
    assert events[-1].turn_id == "turn-fixture"
    assert wires[-1].process.poll() is not None
    assert not client._lock.locked()
    rows = wire_records(trace)
    assert sum(row.get("method") == "turn/start" for row in rows) == 1
    if scenario == "approval":
        assert {"id": "tool-fixture", "result": {"decision": "decline"}} in rows


def test_early_turn_notification_persists_identity_before_response(fake_session):
    client, trace, _ = fake_session("early_notification")
    events = []

    def persist(event):
        events.append(event)
        if event.phase == "turn_started":
            raise RuntimeError("write failed")

    with pytest.raises(SessionError):
        client.generate(request(), cancelled=lambda: False, on_lifecycle=persist)
    assert events[-1].turn_id == "turn-fixture"
    assert "turn/interrupt" in [row.get("method") for row in wire_records(trace)]


@pytest.mark.parametrize("device_code", [True, False])
def test_managed_login_cancel_uses_real_transport_without_generation(fake_session, device_code):
    client, trace, _ = fake_session(ready=False)
    challenge = client.start_login(device_code=device_code)
    assert challenge.login_id == "login-fixture"
    assert challenge.user_code == ("FAKE" if device_code else None)
    client.cancel_login(challenge.login_id)
    methods = [row.get("method") for row in wire_records(trace)]
    assert methods == [
        None,
        "initialize",
        "initialized",
        "account/login/start",
        "account/login/cancel",
    ]


@pytest.mark.parametrize(
    "scenario,state,reset",
    [
        ("happy", "unavailable", None),
        ("logged_out", "disconnected", None),
        ("limited", "limited", "2033-05-18T03:33:20+00:00"),
        ("limited_unknown", "limited", None),
    ],
)
def test_status_paginates_but_never_equates_account_with_readiness(
    fake_session, scenario, state, reset
):
    client, trace, _ = fake_session(scenario, ready=False)
    status = client.status()
    assert status.state == state
    assert status.account_connected is (scenario != "logged_out")
    assert status.reset_at == reset
    assert status.image_model_ids == ()
    if scenario != "logged_out":
        assert status.model_ids == ("chosen", "other")
    assert not {"account/login/start", "thread/start", "turn/start"}.intersection(
        row.get("method") for row in wire_records(trace)
    )


def test_generation_remains_blocked_before_any_process_without_restriction_proof(fake_session):
    client, trace, wires = fake_session(ready=False)
    with pytest.raises(SessionError) as error:
        client.generate(request(), cancelled=lambda: False, on_lifecycle=lambda event: None)
    assert error.value.code == "capability_unverified"
    assert not trace.exists() and not wires


def test_cancellation_interrupts_and_releases_shared_turn_lock(fake_session):
    client, trace, wires = fake_session("hang")
    events, errors = [], []
    started = threading.Event()

    def persist(event):
        events.append(event)
        if event.phase == "turn_started":
            started.set()

    def run():
        try:
            client.generate(request(), cancelled=lambda: False, on_lifecycle=persist)
        except SessionError as error:
            errors.append(error)

    thread = threading.Thread(target=run)
    thread.start()
    assert started.wait(2)
    client.cancel("thread-fixture", "turn-fixture")
    thread.join(2)
    assert not thread.is_alive()
    assert errors[0].code == "interrupted"
    assert events[-1].turn_id == "turn-fixture"
    assert "turn/interrupt" in [row.get("method") for row in wire_records(trace)]
    assert wires[-1].process.poll() is not None


def test_stderr_is_drained_and_bounded(fake_session):
    client, _, wires = fake_session("stderr")
    assert (
        client.generate(request(), cancelled=lambda: False, on_lifecycle=lambda event: None).text
        == '{"synthetic":true}'
    )
    assert 0 < len(wires[-1].stderr_tail) <= codex_session.MAX_STDERR_BYTES


def test_startup_timeout_reaps_process(fake_session):
    client, _, wires = fake_session("no_initialize", startup_timeout=0.15)
    status = client.status()
    assert status.error_code == "timeout"
    assert wires[-1].process.poll() is not None


def test_blocked_pipe_write_has_deadline_and_owned_process_cleanup(fake_session):
    client, _, _ = fake_session("no_read")
    client.work_root.mkdir()
    wire = codex_session._Stdio(
        client._command,
        cwd=client.work_root,
        env={"CODEX_HOME": str(client.session_home)},
        shutdown_timeout=0.2,
    )
    try:
        with pytest.raises(SessionError) as error:
            wire.send({"input": "x" * 1000000}, time.monotonic() + 0.15, lambda: False)
        assert error.value.code == "timeout"
    finally:
        wire.close()
    assert wire.process.poll() is not None


def test_cancelled_lock_waiter_cannot_start_a_second_turn(fake_session):
    client, trace, wires = fake_session()
    client._lock.acquire()
    cancelled = threading.Event()
    errors = []

    def run():
        try:
            client.generate(request(), cancelled=cancelled.is_set, on_lifecycle=lambda event: None)
        except SessionError as error:
            errors.append(error)

    thread = threading.Thread(target=run)
    thread.start()
    cancelled.set()
    thread.join(1)
    client._lock.release()
    assert not thread.is_alive()
    assert errors[0].code == "interrupted"
    assert not wires and not trace.exists()


def test_close_cancels_active_turn_and_reaps_before_return(fake_session):
    client, _, wires = fake_session("hang")
    started = threading.Event()
    errors = []

    def run():
        try:
            client.generate(
                request(),
                cancelled=lambda: False,
                on_lifecycle=lambda event: started.set() if event.phase == "turn_started" else None,
            )
        except SessionError as error:
            errors.append(error)

    thread = threading.Thread(target=run)
    thread.start()
    assert started.wait(2)
    client.close()
    thread.join(1)
    assert not thread.is_alive()
    assert errors[0].code == "interrupted"
    assert wires[-1].process.poll() is not None


def sanitized_image(root):
    from PIL import Image

    from oms_hub.study_generation.quiz_images import sanitize_quiz_image

    data = BytesIO()
    Image.new("RGB", (5, 5), "blue").save(data, format="PNG")
    image = sanitize_quiz_image(data.getvalue())
    root.mkdir(parents=True, exist_ok=True)
    path = root / "approved.png"
    path.write_bytes(image.payload)
    return path, image.sha256


def test_images_are_copied_only_after_hash_and_sanitization_checks(fake_session):
    client, trace, _ = fake_session()
    path, digest = sanitized_image(client.work_root / "inputs")
    result = client.generate(
        replace(request(), image_paths=(path,), image_sha256=(digest,)),
        cancelled=lambda: False,
        on_lifecycle=lambda event: None,
    )
    assert result.text == '{"synthetic":true}'
    turn = next(row for row in wire_records(trace) if row.get("method") == "turn/start")
    image = turn["params"]["input"][1]
    assert image["type"] == "localImage"
    assert Path(image["path"]).parent != path.parent
    assert not Path(image["path"]).exists()
    assert path.exists()


@pytest.mark.parametrize(
    "defect",
    [
        "missing_hash",
        "changed_bytes",
        "outside_root",
        "symlink_escape",
        "invalid_image",
        "unsanitized",
    ],
)
def test_invalid_images_fail_before_remote_dispatch(fake_session, defect, tmp_path):
    client, trace, wires = fake_session()
    root = (
        tmp_path / "outside" if defect in ("outside_root", "symlink_escape") else client.work_root
    )
    path, digest = sanitized_image(root)
    if defect == "changed_bytes":
        path.write_bytes(b"different")
    elif defect == "invalid_image":
        path.write_bytes(b"not-an-image")
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
    elif defect == "unsanitized":
        path.write_bytes(path.read_bytes() + b"untrusted trailing metadata")
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
    elif defect == "symlink_escape":
        client.work_root.mkdir()
        link = client.work_root / "linked.png"
        link.symlink_to(path)
        path = link
    with pytest.raises(SessionError) as error:
        client.generate(
            replace(
                request(),
                image_paths=(path,),
                image_sha256=() if defect == "missing_hash" else (digest,),
            ),
            cancelled=lambda: False,
            on_lifecycle=lambda event: None,
        )
    assert error.value.code == "invalid_output"
    assert not trace.exists() and not wires


def test_cli_smoke_cannot_bypass_live_restriction_gate(fake_session, capsys):
    client, _, _ = fake_session(ready=False)
    probe = load_probe()
    assert (
        probe["main"](
            [
                "--smoke",
                "--executable",
                str(client.executable),
                "--session-home",
                str(client.session_home),
                "--work-root",
                str(client.work_root),
                "--model",
                "chosen",
            ]
        )
        == 1
    )
    assert '"error_code": "capability_unverified"' in capsys.readouterr().out


@pytest.mark.parametrize("scenario,exit_code", [("happy", 0), ("logged_out", 1)])
def test_cli_managed_login_uses_transport_and_cancels_timeout(
    fake_session, monkeypatch, capsys, scenario, exit_code
):
    client, trace, wires = fake_session(scenario, ready=False)
    probe = load_probe()
    monkeypatch.setitem(
        probe["live_probe"].__globals__, "CodexSessionClient", lambda *args, **kwargs: client
    )
    assert (
        probe["main"](
            [
                "--login",
                "--executable",
                str(client.executable),
                "--session-home",
                str(client.session_home),
                "--work-root",
                str(client.work_root),
                "--login-timeout",
                "0.1",
            ]
        )
        == exit_code
    )
    output = capsys.readouterr().out
    assert '"url": "https://example.invalid/device"' in output
    assert '"live_ready": false' in output
    assert wires[-1].process.poll() is not None
    if scenario == "logged_out":
        assert "account/login/cancel" in [row.get("method") for row in wire_records(trace)]


@pytest.mark.parametrize("reset", ["unknown", "2026-09-11T00:00:00", "2026-09-11T00:00:00+01:00"])
def test_error_never_exposes_invalid_reset_or_raw_message(reset):
    error = SessionError("PRIVATE unexpected error", reset_at=reset)
    assert error.code == "protocol_error"
    assert error.reset_at is None
    assert "PRIVATE" not in str(error)


def test_limits_use_all_exhausted_windows_and_preserve_unknown():
    limited, reset = codex_session._limit_state(
        {
            "rateLimitsByLimitId": {
                "codex": {"primary": {"usedPercent": 100, "resetsAt": 2000000000}},
                "model": {"secondary": {"usedPercent": 100}},
            }
        }
    )
    assert limited and reset is None
    assert codex_session._limit_state({"rateLimits": {"spendControlReached": True}}) == (True, None)


def test_wrong_executable_pin_never_starts_and_is_not_an_account_connection(fake_session):
    client, trace, wires = fake_session(ready=False)
    client.binary_sha256 = "0" * 64
    status = client.status()
    assert status.account_connected is False
    assert status.error_code == "capability_unverified"
    assert not trace.exists() and not wires
