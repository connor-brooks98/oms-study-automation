import io
import json
import queue
import runpy
import subprocess
import threading
from http.client import HTTPConnection
from http.server import HTTPServer
from pathlib import Path

import pytest


def load_probe():
    module = runpy.run_path(
        str(Path(__file__).parents[2] / "scripts/probe-codex-windows-registry.py")
    )
    assert "exchange" in module, "registry-only protocol implementation is missing"
    return module


def native_frames(work):
    return [
        {"id": 1, "result": {"codexHome": str(work.parent / "home" / "codex")}},
        {
            "id": 2,
            "result": {
                "thread": {"id": "fixture-thread"},
                "model": "gpt-5.5",
                "modelProvider": "policy_fixture",
                "runtimeWorkspaceRoots": [],
                "approvalPolicy": "untrusted",
                "approvalsReviewer": "user",
                "sandbox": {"type": "readOnly", "networkAccess": False},
            },
        },
        {"id": 3, "result": {"turn": {"id": "fixture-turn"}}},
        {
            "method": "item/completed",
            "params": {
                "item": {"type": "agentMessage", "text": "fixture complete"},
            },
        },
        {
            "method": "turn/completed",
            "params": {
                "threadId": "fixture-thread",
                "turn": {"id": "fixture-turn", "status": "completed", "error": None},
            },
        },
    ]


@pytest.mark.parametrize("early_completion", [False, True])
def test_registry_protocol_only_sends_the_three_allowed_rpcs(tmp_path, early_completion):
    module = load_probe()
    report = {"sent_frames": [], "events": []}
    sent = []
    frames = native_frames(tmp_path)
    if early_completion:
        frames.append(frames.pop(2))
    module["exchange"](iter(frames).__next__, sent.append, tmp_path, report)
    assert [f["method"] for f in sent] == [
        "initialize",
        "initialized",
        "thread/start",
        "turn/start",
    ]
    start = sent[2]["params"]
    assert start["model"] == "gpt-5.5"
    assert all(
        start[k] == []
        for k in [
            "environments",
            "selectedCapabilityRoots",
            "runtimeWorkspaceRoots",
            "dynamicTools",
        ]
    )
    assert "environments" not in sent[3]["params"]
    assert report["terminal_status"] == "completed"


@pytest.mark.parametrize(
    "unexpected",
    [
        {"id": 44, "method": "item/commandExecution/requestApproval", "params": {}},
        {"method": "item/started", "params": {"item": {"type": "commandExecution"}}},
    ],
)
def test_protocol_fails_unexpected_actions(tmp_path, unexpected):
    module = load_probe()
    sent = []
    report = {"sent_frames": [], "events": []}
    with pytest.raises(ValueError, match="unexpected"):
        module["exchange"](iter([unexpected]).__next__, sent.append, tmp_path, report)
    assert unexpected in report["events"]
    if "id" in unexpected:
        assert sent[-1]["error"]["code"] == -32601


def test_private_configuration_and_import_never_launch(tmp_path, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("offline check attempted a native launch")

    monkeypatch.setattr(subprocess, "Popen", forbidden)
    module = load_probe()
    monkeypatch.setenv("OPENAI_API_KEY", "must-not-be-inherited")
    monkeypatch.setenv("CODEX_EXEC_SERVER_URL", "must-not-be-inherited")
    env = module["windows_env"](tmp_path, r"C:\Windows")
    assert "OPENAI_API_KEY" not in env and "CODEX_EXEC_SERVER_URL" not in env
    assert env["CODEX_HOME"] == str(tmp_path / "home" / "codex")
    config = module["configuration"](12345)
    assert config["windows.sandbox"] == "elevated"
    assert config["cli_auth_credentials_store"] == "file"
    assert config["model_providers.policy_fixture.requires_openai_auth"] is False
    assert config["model_providers.policy_fixture.base_url"] == "http://127.0.0.1:12345/v1"
    wrong = tmp_path / "wrong.exe"
    wrong.write_bytes(b"not the pinned binary")
    with pytest.raises(ValueError, match="hash"):
        module["check_binary"](wrong)
    with pytest.raises(SystemExit):
        module["main"]([])


def test_capture_bounds_and_additional_registry_detection(tmp_path):
    module = load_probe()
    frames = queue.Queue()
    module["capture"](
        io.BytesIO(b"x" * (module["FRAME_LIMIT"] + 1)), tmp_path / "raw", frames, True
    )
    assert isinstance(frames.get_nowait(), ValueError)
    assert (tmp_path / "raw").stat().st_size <= module["FRAME_LIMIT"]
    request = {
        "path": "/v1/responses",
        "body": {
            "model": "gpt-5.5",
            "input": [
                {
                    "type": "additional_tools",
                    "tools": [{"type": "custom", "name": "apply_patch"}],
                }
            ],
        },
    }
    report = {
        "requests": [request],
        "terminal_status": "completed",
        "process_returncode": 0,
        "error": None,
    }
    with pytest.raises(ValueError, match="registry"):
        module["validate_result"](report)
    assert report["request_summaries"][0]["offered_tools"] == ["apply_patch"]


def test_loopback_fixed_completion_and_extra_request_failure(tmp_path):
    module = load_probe()
    frames = queue.Queue()
    report = {
        "requests": [],
        "fixture_responses": [],
        "terminal_status": "completed",
        "process_returncode": 0,
        "error": None,
    }
    server = HTTPServer(("127.0.0.1", 0), module["fixture_handler"](report, frames, tmp_path))
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    client = HTTPConnection(*server.server_address, timeout=2)
    try:
        body = {"model": "gpt-5.5", "input": [], "tools": []}
        client.request("POST", "/v1/responses", json.dumps(body))
        response = client.getresponse()
        assert response.status == 200
        events = [
            json.loads(line[6:])
            for line in response.read().decode().splitlines()
            if line.startswith("data: ")
        ]
        output = events[-1]["response"]["output"]
        assert len(output) == 1 and output[0]["type"] == "message"
        assert output[0]["content"] == [{"type": "output_text", "text": "fixture complete"}]
        assert json.loads((tmp_path / "fixture-request.json").read_text()) == body
        assert (tmp_path / "fixture-response.json").exists()
        assert frames.empty()
        module["validate_result"](report)
        client.request("POST", "/v1/responses", json.dumps(body))
        response = client.getresponse()
        assert response.status == 400
        response.read()
        assert "extra request" in str(frames.get_nowait())
        assert len(report["requests"]) == 1
    finally:
        client.close()
        server.shutdown()
        worker.join(timeout=2)
        server.server_close()
