"""Explicit macOS-only, auth-free native registry probe against a loopback fixture.

This does not use the production session client or establish live readiness.
"""

import argparse
import hashlib
import json
import queue
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from oms_hub.llm.codex_session import (
    INSPECTED_MACOS_BINARY_SHA256 as PIN,
)

# Probe-only partial controls for pinned 0.153.4. Astra model-backed handlers still execute.
_POLICY_DISABLED_FEATURES = """
apply_patch_preserve_line_endings apply_patch_streaming_events apps artifact auth_elicitation
background_paginated_rollout_migration bedrock_setup_wizard browser_use browser_use_external
browser_use_full_cdp_access chronicle code_mode code_mode_host code_mode_interrupt
code_mode_only code_mode_prewarm compaction_image_budget computer_use
concurrent_reasoning_summaries content_item_kinds context_management current_time_reminder
cwd_relative_turn_diffs default_mode_request_user_input deferred_executor
deferred_tool_world_state enable_mcp_apps enable_request_compression exec_permission_approvals
executed_tool_call_metadata executor_capability_discovery external_agent_memory_import fast_mode
goals guardian_approval guardian_enhanced_node_repl_transcripts guardian_ext
guardian_node_repl_transcript_images guardian_reuse_parent_compaction guardianv2 hooks
image_generation image_resize_notice in_app_browser in_app_chat in_app_dictation
in_app_local_automation in_app_updates local_thread_store_compression mcp_2026_07_28
mcp_oauth_refresh_coordination memories mentions_v2 multi_agent multi_agent_v2 network_proxy
non_prefixed_mcp_tool_names omit_app_server_notification_media personality plugin_sharing
plugins powershell_shell_version prevent_idle_sleep psp realtime_conversation
recommended_plugins remote_compaction_v2 remote_plugin request_permissions_tool
respect_system_proxy retain_client_developer_messages rollout_budget runtime_metrics
secret_auth_storage shell_snapshot shell_snapshot_v2 shell_tool shell_zsh_fork
skill_mcp_dependency_install skill_search skip_host_skill_discovery sleep_tool
standalone_web_search step_model_switching terminal_visualization_instructions token_budget
tool_call_mcp_elicitation tool_suggest transcript_v2 unbounded_connection_retries unified_exec
unified_image_budget use_agent_identity use_legacy_landlock view_image web_search_cached
web_search_request workspace_dependencies write_stdin_approval
""".split()


def codex_tool_policy_config(binary_sha256: str) -> dict[str, str | bool]:
    """Return inspected native controls, never a statement of provider/Windows readiness."""
    if binary_sha256 != PIN:
        raise ValueError("unsupported policy pin")
    return {
        "web_search": "disabled",
        "apps._default.enabled": False,
        "tools.experimental_request_user_input.enabled": False,
        "orchestrator.skills.enabled": False,
        "orchestrator.mcp.enabled": False,
        **{f"features.{name}": False for name in _POLICY_DISABLED_FEATURES},
    }


def codex_tool_policy_args(binary_sha256: str) -> list[str]:
    return [
        "--strict-config",
        *(
            arg
            for key, value in codex_tool_policy_config(binary_sha256).items()
            for arg in ("-c", f"{key}={json.dumps(value)}")
        ),
    ]


MODES = (
    "registry",
    "skills-list",
    "thread-untrusted",
    "skills-list-untrusted",
    "denial-matrix",
    "apply-patch",
)


def fixture_calls(work: Path) -> list[dict[str, Any]]:
    """Fixed synthetic calls only; never accept caller-provided commands or paths."""
    canary = str(work / "tool-must-not-create")
    calls = [
        ("list", "skills", {"authority": {"kind": "executor"}}),
        ("read", "skills", {"package": "fixture-missing-package"}),
        ("request_user_input", None, {"questions": []}),
        ("exec_command", None, {"cmd": f"/usr/bin/touch {canary}"}),
        ("shell", None, {"command": ["/usr/bin/touch", canary]}),
        ("read_file", None, {"path": str(work / "blank-fixture.txt")}),
        ("mcp__policy_fixture__read", None, {}),
        ("web_search", None, {"query": "synthetic fixture"}),
        ("view_image", None, {"path": str(work / "missing-fixture.png")}),
        ("spawn_agent", None, {"message": "synthetic fixture"}),
        ("wait", "functions", {"cell_id": "fixture-never-created", "yield_time_ms": 1}),
        (
            "request_user_input_async",
            "functions",
            {"questions": [{"title": "Synthetic fixture only"}]},
        ),
        ("list_agents", "collaboration", {}),
    ]
    return [
        {
            "type": "function_call",
            "id": f"fc_fixture_{index}",
            "call_id": f"call_policy_{index}",
            "name": name,
            **({"namespace": namespace} if namespace else {}),
            "arguments": json.dumps(arguments),
        }
        for index, (name, namespace, arguments) in enumerate(calls)
    ] + [
        {
            "type": "custom_tool_call",
            "id": f"fc_fixture_{len(calls)}",
            "call_id": f"call_policy_{len(calls)}",
            "name": "exec",
            "namespace": "functions",
            "input": 'text("POLICY_EXECUTED")',
        }
    ]


def probe(
    executable: Path,
    *,
    mode: str = "registry",
    explicit_tool_controls: bool = False,
    stable_thread: bool = False,
    model: str = "fixture-model",
) -> dict[str, Any]:
    if mode not in MODES:
        raise ValueError("unsupported fixture mode")
    if model not in {"fixture-model", "gpt-6-astra", "gpt-5.5"}:
        raise ValueError("unsupported fixture model slug")
    with executable.open("rb") as stream:
        if hashlib.file_digest(stream, "sha256").hexdigest() != PIN:
            raise ValueError("unsupported executable hash")
    if sys.platform != "darwin" or not Path("/usr/bin/sandbox-exec").is_file():
        raise ValueError("probe requires the macOS loopback network sandbox")
    with tempfile.TemporaryDirectory(prefix="codex-tool-policy-") as temporary:
        root = Path(temporary).resolve()
        home, work = root / "home", root / "work"
        home.mkdir()
        work.mkdir()
        (work / "blank-fixture.txt").write_text("synthetic fixture only")
        codex_home = home / "codex"
        codex_home.mkdir()
        env = {"HOME": str(home), "CODEX_HOME": str(codex_home), "PATH": "/usr/bin:/bin"}
        listing = subprocess.run(
            [
                "/usr/bin/sandbox-exec",
                "-p",
                "(version 1)(allow default)(deny network*)",
                str(executable),
                "features",
                "list",
            ],
            cwd=work,
            env=env,
            capture_output=True,
            text=True,
            check=True,
            timeout=15,
        ).stdout
        policy = codex_tool_policy_config(PIN)
        disabled = [key.removeprefix("features.") for key in policy if key.startswith("features.")]
        native_features = {
            line.split()[0]
            for line in listing.splitlines()
            if line.strip() and "removed" not in line.split()
        }
        if native_features != set(disabled):
            raise ValueError("pinned feature catalog does not match shared policy")
        policy_args = codex_tool_policy_args(PIN)
        if not explicit_tool_controls:
            policy = {
                key: value
                for key, value in policy.items()
                if key.startswith("features.") or key in {"web_search", "apps._default.enabled"}
            }
            policy_args = [
                "--strict-config",
                *(
                    arg
                    for key, value in policy.items()
                    for arg in ("-c", f"{key}={json.dumps(value)}")
                ),
            ]
        requests: list[dict[str, Any]] = []
        fixture_responses = []
        sent_frames = []

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format: str, *args: object) -> None:
                pass

            def do_POST(self) -> None:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= 8 * 1024 * 1024 or len(requests) >= 2:
                    self.send_error(400)
                    return
                body = json.loads(self.rfile.read(length))
                requests.append({"path": self.path, "body": body})
                message: dict[str, Any] = {
                    "type": "message",
                    "id": "msg_fixture",
                    "role": "assistant",
                    "status": "completed",
                    "content": [{"type": "output_text", "text": "fixture complete"}],
                }
                if mode.startswith("skills-list") and len(requests) == 1:
                    message = {
                        "type": "function_call",
                        "id": "fc_fixture",
                        "call_id": "call_policy_fixture",
                        "name": "list",
                        "namespace": "skills",
                        "arguments": json.dumps({"authority": {"kind": "executor"}}),
                    }
                output = (
                    fixture_calls(work)
                    if mode == "denial-matrix" and len(requests) == 1
                    else [message]
                )
                if mode == "apply-patch" and len(requests) == 1:
                    output = [
                        {
                            "type": "custom_tool_call",
                            "id": "fc_fixture_apply_patch",
                            "call_id": "call_policy_apply_patch",
                            "name": "apply_patch",
                            "input": (
                                f"*** Begin Patch\n*** Add File: {work / 'tool-must-not-create'}\n"
                                "+synthetic fixture only\n*** End Patch\n"
                            ),
                        }
                    ]
                response = {
                    "id": "resp_fixture",
                    "model": model,
                    "status": "completed",
                    "output": output,
                    "usage": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
                }
                events: list[tuple[str, dict[str, Any]]] = [
                    ("response.created", {"response": {**response, "status": "in_progress"}}),
                    *(
                        ("response.output_item.done", {"output_index": i, "item": item})
                        for i, item in enumerate(output)
                    ),
                    ("response.completed", {"response": response}),
                ]
                fixture_responses.append(events)
                payload = "".join(
                    f"event: {kind}\ndata: " + json.dumps({"type": kind, **data}) + "\n\n"
                    for kind, data in events
                ).encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        port = server.server_address[1]
        server_thread = threading.Thread(target=server.serve_forever, daemon=True)
        server_thread.start()
        config = (
            f'model = "{model}"\nmodel_provider = "policy_fixture"\n'
            'cli_auth_credentials_store = "file"\nweb_search = "disabled"\n'
            'sandbox_mode = "read-only"\napproval_policy = "on-request"\n'
            '[model_providers.policy_fixture]\nname = "Auth-free loopback fixture"\n'
            f'base_url = "http://127.0.0.1:{port}/v1"\nwire_api = "responses"\n'
            "requires_openai_auth = false\nsupports_websockets = false\n"
            "request_max_retries = 0\nstream_max_retries = 0\n"
            "[mcp_servers]\n"
        )
        (codex_home / "config.toml").write_text(config)
        profile = (
            f"(version 1)(allow default)(deny network*)"
            f'(allow network-outbound (remote ip "localhost:{port}"))'
        )
        command = [
            "/usr/bin/sandbox-exec",
            "-p",
            profile,
            str(executable),
            "app-server",
            "--listen",
            "stdio://",
            *policy_args,
        ]
        frames: queue.Queue[dict[str, Any] | Exception] = queue.Queue()
        events = []
        error = None
        process = None
        with (root / "stderr.txt").open("wb") as stderr:
            try:
                process = subprocess.Popen(
                    command,
                    cwd=work,
                    env=env,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=stderr,
                )

                def read_frames() -> None:
                    assert process.stdout is not None
                    total = 0
                    while line := process.stdout.readline(1024 * 1024 + 1):
                        total += len(line)
                        if len(line) > 1024 * 1024 or total > 8 * 1024 * 1024:
                            frames.put(ValueError("native frame limit exceeded"))
                            return
                        try:
                            frames.put(json.loads(line))
                        except ValueError as exc:
                            frames.put(exc)
                            return
                    frames.put(EOFError("native process closed stdout"))

                reader = threading.Thread(target=read_frames, daemon=True)
                reader.start()
                deadline = time.monotonic() + 30

                def send(value: dict[str, Any]) -> None:
                    assert process.stdin is not None
                    sent_frames.append(value)
                    process.stdin.write(json.dumps(value).encode() + b"\n")
                    process.stdin.flush()

                def receive() -> dict[str, Any]:
                    frame = frames.get(timeout=max(0.01, deadline - time.monotonic()))
                    if isinstance(frame, Exception):
                        raise frame
                    events.append(frame)
                    if "method" in frame and "id" in frame:
                        send(
                            {
                                "id": frame["id"],
                                "error": {"code": -32601, "message": "fixture denies requests"},
                            }
                        )
                    return frame

                def rpc(number: int, method: str, params: dict[str, Any]) -> Any:
                    send({"id": number, "method": method, "params": params})
                    while True:
                        frame = receive()
                        if frame.get("id") == number:
                            if "error" in frame:
                                raise ValueError(frame["error"])
                            return frame["result"]

                rpc(
                    1,
                    "initialize",
                    {
                        "clientInfo": {"name": "tool_policy_fixture", "version": "1"},
                        **({} if stable_thread else {"capabilities": {"experimentalApi": True}}),
                    },
                )
                send({"method": "initialized"})
                thread = rpc(
                    2,
                    "thread/start",
                    {
                        "model": model,
                        "cwd": str(work),
                        "ephemeral": True,
                        **(
                            {
                                "developerInstructions": "Return the fixture response only.",
                                "approvalsReviewer": "user",
                                "approvalPolicy": "untrusted",
                            }
                            if stable_thread
                            else {
                                "modelProvider": "policy_fixture",
                                "allowProviderModelFallback": False,
                                "dynamicTools": [],
                                "environments": [],
                                "selectedCapabilityRoots": [],
                                "runtimeWorkspaceRoots": [],
                            }
                        ),
                        "sandbox": "read-only",
                        **(
                            {"approvalPolicy": "untrusted"}
                            if mode.endswith("untrusted")
                            or mode in {"denial-matrix", "apply-patch"}
                            else {}
                        ),
                    },
                )
                rpc(
                    3,
                    "turn/start",
                    {
                        "threadId": thread["thread"]["id"],
                        "model": model,
                        "input": [{"type": "text", "text": "Return fixture complete."}],
                    },
                )
                while receive().get("method") != "turn/completed":
                    pass
            except (ValueError, OSError, EOFError, queue.Empty) as exc:
                error = f"{type(exc).__name__}: {exc}"
            finally:
                if process is not None:
                    assert process.stdin is not None and process.stdout is not None
                    process.stdin.close()
                    try:
                        process.wait(timeout=3)
                    except subprocess.TimeoutExpired:
                        process.terminate()
                        try:
                            process.wait(timeout=3)
                        except subprocess.TimeoutExpired:
                            process.kill()
                            process.wait(timeout=3)
                    reader.join(timeout=3)
                    process.stdout.close()
                server.shutdown()
                server.server_close()
                server_thread.join(timeout=3)
        return {
            "binary_sha256": PIN,
            "mode": mode,
            "explicit_tool_controls": explicit_tool_controls,
            "stable_thread": stable_thread,
            "model_slug": model,
            "restrictions_verified": False,
            "provider_verified": False,
            "disabled_features": disabled,
            "config": config,
            "policy": policy,
            "policy_args": policy_args,
            "request_summaries": summarize_requests(requests),
            "network_profile": profile,
            "requests": requests,
            "fixture_responses": fixture_responses,
            "sent_frames": sent_frames,
            "events": events,
            "error": error,
            "process_returncode": process.returncode if process is not None else None,
            "canary_created": (work / "tool-must-not-create").exists(),
            "fixture_unchanged": (work / "blank-fixture.txt").read_text()
            == "synthetic fixture only",
            "terminal_status": next(
                (
                    e["params"]["turn"]["status"]
                    for e in reversed(events)
                    if e.get("method") == "turn/completed"
                ),
                None,
            ),
            "stderr": (root / "stderr.txt").read_bytes()[-65536:].decode(errors="replace"),
        }


def summarize_requests(requests: list[dict[str, Any]]) -> list[dict[str, Any]]:
    summaries = []
    for request in requests:
        body = request["body"]
        tools = list(body.get("tools", []))
        for item in body.get("input", []):
            if item.get("type") == "additional_tools":
                tools.extend(item.get("tools", []))
        names: list[str] = []
        for tool in tools:
            if tool["type"] == "namespace":
                names.extend(f"{tool['name']}.{child['name']}" for child in tool["tools"])
            else:
                names.append(tool.get("name", tool["type"]))
        summaries.append(
            {
                "path": request["path"],
                "offered_tools": names,
                "tool_choice": body.get("tool_choice"),
                "call_outputs": [
                    item
                    for item in body.get("input", [])
                    if item.get("type") in {"function_call_output", "custom_tool_call_output"}
                ],
            }
        )
    return summaries


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--executable", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--explicit-tool-controls", action="store_true")
    parser.add_argument("--stable-thread", action="store_true")
    parser.add_argument(
        "--model", choices=("fixture-model", "gpt-6-astra", "gpt-5.5"), default="fixture-model"
    )
    parser.add_argument("--mode", choices=MODES, default="registry")
    args = parser.parse_args(argv)
    if args.output.exists():
        parser.error("output must be a new file")
    report = probe(
        args.executable,
        mode=args.mode,
        explicit_tool_controls=args.explicit_tool_controls,
        stable_thread=args.stable_thread,
        model=args.model,
    )
    with args.output.open("x") as stream:
        json.dump(report, stream, indent=2)
    print(
        json.dumps(
            {
                "output": str(args.output),
                "requests": len(report["requests"]),
                "error": report["error"],
                "restrictions_verified": False,
            }
        )
    )
    return 1 if report["error"] or report["terminal_status"] != "completed" else 0


if __name__ == "__main__":
    raise SystemExit(main())
