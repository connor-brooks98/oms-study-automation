import json
import os
import runpy
import subprocess
from pathlib import Path

import pytest

from oms_hub.llm.codex_session import (
    INSPECTED_MACOS_BINARY_SHA256,
    CodexSessionClient,
    SessionError,
    SessionRequest,
)


def load_probe():
    return runpy.run_path(str(Path(__file__).parents[2] / "scripts/probe-codex-tool-policy.py"))


def test_partial_preparation_is_pinned_and_does_not_open_generation(tmp_path):
    client = CodexSessionClient(tmp_path / "codex", tmp_path / "home", tmp_path / "work")
    original = list(client._command)
    command = load_probe()["codex_tool_policy_args"](INSPECTED_MACOS_BINARY_SHA256)
    assert client._command == original
    assert command[0] == "--strict-config"
    overrides = {
        value.split("=", 1)[0]: json.loads(value.split("=", 1)[1]) for value in command[2::2]
    }
    assert overrides["features.shell_tool"] is False
    assert overrides["features.code_mode_host"] is False
    assert overrides["features.hooks"] is False
    assert overrides["features.apps"] is False
    assert overrides["features.plugins"] is False
    assert overrides["orchestrator.skills.enabled"] is False
    assert overrides["orchestrator.mcp.enabled"] is False
    assert overrides["tools.experimental_request_user_input.enabled"] is False
    assert overrides["web_search"] == "disabled"
    with pytest.raises(ValueError, match="pin"):
        load_probe()["codex_tool_policy_args"]("0" * 64)
    with pytest.raises(SessionError, match="not verified"):
        client.generate(
            SessionRequest("fixture", "gpt-5.5", "Fixture", "Fixture"),
            cancelled=lambda: False,
            on_lifecycle=lambda _: None,
        )
    assert not client.session_home.exists() and not client.work_root.exists()


def test_probe_import_and_pin_rejection_never_launch(tmp_path, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("unexpected native launch")

    monkeypatch.setattr(subprocess, "Popen", forbidden)
    module = load_probe()
    with pytest.raises(SystemExit):
        module["main"]([])
    binary = tmp_path / "not-codex"
    binary.write_bytes(b"wrong executable")
    with pytest.raises(ValueError, match="hash"):
        module["probe"](binary)


def test_registry_summary_includes_additional_tools_and_custom_results():
    module = load_probe()
    request = {
        "path": "/v1/responses",
        "body": {
            "tool_choice": "auto",
            "tools": [{"type": "function", "name": "request_user_input"}],
            "input": [
                {
                    "type": "additional_tools",
                    "tools": [
                        {
                            "type": "namespace",
                            "name": "functions",
                            "tools": [{"type": "custom", "name": "exec"}],
                        }
                    ],
                },
                {
                    "type": "custom_tool_call_output",
                    "call_id": "call_policy_10",
                    "output": "code-mode host is disabled",
                },
            ],
        },
    }
    summary = module["summarize_requests"]([request])[0]
    assert summary["offered_tools"] == ["request_user_input", "functions.exec"]
    assert summary["call_outputs"][0]["output"] == "code-mode host is disabled"


@pytest.mark.skipif(
    not os.environ.get("CODEX_TOOL_POLICY_NATIVE"), reason="explicit local native probe"
)
def test_pinned_native_astra_records_denials_and_remaining_handler_execution(tmp_path):
    module = load_probe()
    report = module["probe"](
        Path(os.environ["CODEX_TOOL_POLICY_NATIVE"]),
        mode="denial-matrix",
        explicit_tool_controls=True,
        stable_thread=True,
        model="gpt-6-astra",
    )
    output = Path(
        os.environ.get("CODEX_TOOL_POLICY_EVIDENCE", str(tmp_path / "native-evidence.json"))
    )
    with output.open("x") as stream:
        json.dump(report, stream, indent=2)
    assert report["error"] is None
    assert report["terminal_status"] == "completed"
    assert report["process_returncode"] == 0
    assert report["policy"] == module["codex_tool_policy_config"](INSPECTED_MACOS_BINARY_SHA256)
    assert len(report["request_summaries"]) == 2
    expected_tools = [
        "functions.exec",
        "functions.wait",
        "functions.request_user_input_async",
        "collaboration.followup_task",
        "collaboration.interrupt_agent",
        "collaboration.list_agents",
        "collaboration.send_message",
        "collaboration.spawn_agent",
        "collaboration.wait_agent",
    ]
    assert all(r["offered_tools"] == expected_tools for r in report["request_summaries"])
    outputs = report["request_summaries"][1]["call_outputs"]
    assert [item["call_id"] for item in outputs] == [f"call_policy_{i}" for i in range(14)]
    assert all(item["output"].startswith("unsupported call:") for item in outputs[:10])
    assert outputs[10]["output"] == "code-mode host is disabled"
    assert json.loads(outputs[11]["output"]) == {"accepted": True}
    assert json.loads(outputs[12]["output"])["agents"]
    assert outputs[13]["output"] == "code-mode host is disabled"
    assert report["canary_created"] is False and report["fixture_unchanged"] is True
    assert report["restrictions_verified"] is False and report["provider_verified"] is False


@pytest.mark.skipif(
    not os.environ.get("CODEX_TOOL_POLICY_NATIVE"), reason="explicit local native probe"
)
def test_pinned_native_gpt55_no_environments_rejects_apply_patch(tmp_path):
    module = load_probe()
    report = module["probe"](
        Path(os.environ["CODEX_TOOL_POLICY_NATIVE"]),
        mode="apply-patch",
        explicit_tool_controls=True,
        stable_thread=False,
        model="gpt-5.5",
    )
    output = Path(
        os.environ.get("CODEX_TOOL_POLICY_EVIDENCE", str(tmp_path / "native-evidence.json"))
    )
    with output.open("x") as stream:
        json.dump(report, stream, indent=2)
    assert report["error"] is None
    assert report["terminal_status"] == "completed"
    assert report["process_returncode"] == 0
    start = next(f["params"] for f in report["sent_frames"] if f.get("method") == "thread/start")
    assert start["environments"] == []
    assert start["selectedCapabilityRoots"] == []
    assert start["runtimeWorkspaceRoots"] == []
    assert start["approvalPolicy"] == "untrusted"
    assert len(report["request_summaries"]) == 2
    assert all(r["offered_tools"] == [] for r in report["request_summaries"])
    outputs = report["request_summaries"][1]["call_outputs"]
    assert len(outputs) == 1
    assert outputs[0]["type"] == "custom_tool_call_output"
    assert outputs[0]["call_id"] == "call_policy_apply_patch"
    assert outputs[0]["output"] == "unsupported custom tool call: apply_patch"
    assert report["canary_created"] is False and report["fixture_unchanged"] is True
    assert report["restrictions_verified"] is False and report["provider_verified"] is False
