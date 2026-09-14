import json
import runpy
import subprocess
import tomllib
from pathlib import Path

import pytest

from oms_hub.llm.codex_session import CodexSessionClient, SessionError, SessionRequest


def test_macos_diagnostic_offline_contract_and_closed_hub_gate(tmp_path, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("offline check launched a native process")

    monkeypatch.setattr(subprocess, "Popen", forbidden)
    probe = runpy.run_path(
        str(Path(__file__).parents[2] / "scripts/probe-codex-macos-generation.py")
    )
    work = tmp_path / "work"
    params = probe["thread_params"](work)
    assert params["model"] == "gpt-5.5"
    assert params["modelProvider"] == "openai" and params["allowProviderModelFallback"] is False
    assert all(
        params[key] == []
        for key in (
            "environments",
            "dynamicTools",
            "selectedCapabilityRoots",
            "runtimeWorkspaceRoots",
        )
    )
    reply = {
        **params,
        "sandbox": {"type": "readOnly", "networkAccess": False},
        "thread": {"id": "t"},
    }
    assert probe["check_thread"](reply) == "t"
    for key, drift in (
        ("model", "gpt-6-astra"),
        ("modelProvider", "other"),
        ("runtimeWorkspaceRoots", ["/"]),
    ):
        with pytest.raises(SessionError):
            probe["check_thread"]({**reply, key: drift})
    probe["validate_output"]('{"number":7,"color":"red"}')
    for value in (
        {"number": 7, "color": "blue"},
        {"number": 7.0, "color": "red"},
        {"number": 7, "color": "red", "extra": 1},
        "bad",
    ):
        with pytest.raises(SessionError):
            probe["validate_output"](json.dumps(value))
    from io import BytesIO

    from PIL import Image

    image = Image.open(BytesIO(probe["synthetic_png"]()))
    assert image.size == (32, 32) and image.getpixel((0, 0)) == (255, 0, 0)
    sandbox = probe["profile"](tmp_path / "codex", tmp_path / "session", work)
    assert "(deny file-read-data)" in sandbox and "(deny file-write*)" in sandbox
    assert '(subpath "/System")' not in sandbox  # Would expose /System/Volumes/Data/Users.
    client = CodexSessionClient(
        tmp_path / "codex", tmp_path / "session", work, binary_sha256="0" * 64
    )
    with pytest.raises(SessionError, match="not verified"):
        client.generate(
            SessionRequest("test", "gpt-5.5", "test", "test"),
            cancelled=lambda: False,
            on_lifecycle=lambda _: None,
        )
    assert not work.exists()
    policy = probe["current_policy"]()
    features = {
        key.removeprefix("features."): value
        for key, value in policy.items()
        if key.startswith("features.")
    }
    assert len(features) == 103
    assert all(value is False for value in features.values())
    args = probe["current_policy_args"]()
    assert args[0] == "--strict-config" and "--strict-config" not in args[1:]
    feature_arg = next(value for value in args if value.startswith("features="))
    parsed = tomllib.loads(feature_arg)["features"]
    assert parsed == features and parsed["guardianv2.thread_context"] is False
    assert "guardianv2" in parsed and parsed["guardianv2"] is False
    effective = features | {"unified_exec": True}
    assert probe["check_effective_features"](effective) == features
    for wrong in (features, effective | {"shell_tool": True}, effective | {"new_tool": False}):
        with pytest.raises(ValueError, match="feature policy differs"):
            probe["check_effective_features"](wrong)
    command = probe["provider_command"](tmp_path / "codex")
    assert command[:4] == [str(tmp_path / "codex"), "app-server", "--listen", "stdio://"]
    assert "--strict-config" in command and 'model_provider="openai"' in command
    assert 'cli_auth_credentials_store="file"' in command
    assert not any("request_max_retries" in value for value in command)
    with pytest.raises(ValueError, match="fixture receipt"):
        probe["probe"](
            tmp_path / "codex", tmp_path / "session", tmp_path / "evidence", provider=True
        )


def test_macos_matrix_exact_valid_calls_and_rejection_oracles(tmp_path, monkeypatch):
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: pytest.fail("native launch"))
    probe = runpy.run_path(
        str(Path(__file__).parents[2] / "scripts/probe-codex-macos-generation.py")
    )
    fixture = runpy.run_path(str(Path(__file__).parents[2] / "scripts/probe-codex-tool-policy.py"))
    assert probe["INTERRUPT_MODE"] in fixture["MODES"]
    calls = probe["matrix_calls"](tmp_path)
    assert len(calls) == 15
    assert calls[9]["namespace"] == "collaboration" and calls[9]["name"] == "spawn_agent"
    assert json.loads(calls[9]["arguments"])["task_name"] == "policy_fixture_child"
    assert calls[14]["type"] == "custom_tool_call" and "*** Add File:" in calls[14]["input"]
    expected_names = [
        "skillslist",
        "skillsread",
        "request_user_input",
        "exec_command",
        "shell",
        "read_file",
        "mcp__policy_fixture__read",
        "web_search",
        "view_image",
        "collaborationspawn_agent",
        "wait",
        "request_user_input_async",
        "collaborationlist_agents",
        "exec",
        "apply_patch",
    ]
    outputs = [
        {
            "type": "custom_tool_call_output" if i >= 13 else "function_call_output",
            "call_id": f"call_policy_{i}",
            "output": ("unsupported custom tool call: " if i >= 13 else "unsupported call: ")
            + name,
        }
        for i, name in enumerate(expected_names)
    ]
    probe["check_matrix_outputs"](calls, outputs)
    for i in range(15):
        for key, wrong in (("type", "wrong"), ("output", outputs[i]["output"] + " unexpected")):
            bad = [dict(row) for row in outputs]
            bad[i][key] = wrong
            with pytest.raises(ValueError):
                probe["check_matrix_outputs"](calls, bad)
    with pytest.raises(ValueError):
        probe["check_matrix_outputs"](calls, outputs[::-1])
    stderr = "".join(
        "2026-09-14T18:31:58.553861Z ERROR codex_core::tools::router: error=" + row["output"] + "\n"
        for row in outputs
    )
    check = probe["check_fixture_events"]
    assert check([], stderr, outputs) and check([], "", [])
    assert not check([], stderr + "ERROR unexpected\n", outputs)
    assert not check([{"method": "tool/call", "id": 8}], stderr, outputs)
    assert not check(
        [{"method": "item/started", "params": {"item": {"type": "commandExecution"}}}],
        stderr,
        outputs,
    )
