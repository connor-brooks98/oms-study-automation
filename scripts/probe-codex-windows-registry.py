"""Windows-only, auth-free registry diagnostic. No OS network isolation is provided.

One app-server launch; registry or fixed apply_patch denial only. No account/setup RPCs.
"""

import argparse
import hashlib
import json
import os
import queue
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path, PureWindowsPath

PIN = "444a3f0008050605cae73cd9b7a2dcac61294062dfaab56dd20430fd6498518b"
MODEL = "gpt-5.5"
FRAME_LIMIT = 1024 * 1024
OUTPUT_LIMIT = 8 * FRAME_LIMIT
STDERR_LIMIT = 256 * 1024
DEADLINE_SECONDS = 40
# Copied from probe-codex-tool-policy.py at f3711eb3; standalone for Python -I.
# This is a partial control snapshot, not accepted Windows restriction evidence.
DISABLED_FEATURES = """
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


def configuration(port):
    if not 0 < port < 65536:
        raise ValueError("invalid fixture port")
    return {
        "model": MODEL,
        "model_provider": "policy_fixture",
        "cli_auth_credentials_store": "file",
        "sandbox_mode": "read-only",
        "approval_policy": "on-request",
        "windows.sandbox": "elevated",
        "analytics.enabled": False,
        "web_search": "disabled",
        "apps._default.enabled": False,
        "tools.experimental_request_user_input.enabled": False,
        "orchestrator.skills.enabled": False,
        "orchestrator.mcp.enabled": False,
        **{f"features.{name}": False for name in DISABLED_FEATURES},
        "model_providers.policy_fixture.name": "Auth-free loopback fixture",
        "model_providers.policy_fixture.base_url": f"http://127.0.0.1:{port}/v1",
        "model_providers.policy_fixture.wire_api": "responses",
        "model_providers.policy_fixture.requires_openai_auth": False,
        "model_providers.policy_fixture.supports_websockets": False,
        "model_providers.policy_fixture.request_max_retries": 0,
        "model_providers.policy_fixture.stream_max_retries": 0,
    }


def windows_env(root, system_root):
    if not PureWindowsPath(system_root).is_absolute():
        raise ValueError("SystemRoot must be an absolute Windows path")
    home = root / "home"
    return {
        "SystemRoot": system_root,
        "WINDIR": system_root,
        "SystemDrive": PureWindowsPath(system_root).drive,
        "PATH": str(PureWindowsPath(system_root) / "System32"),
        "HOME": str(home),
        "USERPROFILE": str(home),
        "CODEX_HOME": str(home / "codex"),
        "APPDATA": str(home / "AppData" / "Roaming"),
        "LOCALAPPDATA": str(home / "AppData" / "Local"),
        "TEMP": str(root / "temp"),
        "TMP": str(root / "temp"),
    }


def check_binary(executable):
    with executable.open("rb") as stream:
        if hashlib.file_digest(stream, "sha256").hexdigest() != PIN:
            raise ValueError("unsupported Windows executable hash")


def inspect_frame(frame):
    if not isinstance(frame, dict):
        raise ValueError("unexpected non-object native frame")
    method = frame.get("method", "")
    if method and "id" in frame:
        raise ValueError(f"unexpected native action request: {method}")
    if method in {"item/started", "item/completed"}:
        item = frame.get("params", {}).get("item", {})
        if item.get("type") not in {"userMessage", "agentMessage"}:
            raise ValueError(f"unexpected native item: {item.get('type')}")
        if item.get("type") == "agentMessage" and item.get("text") not in {"", "fixture complete"}:
            raise ValueError("unexpected assistant text")
    if method == "error" or method.startswith("windowsSandbox/"):
        raise ValueError(f"unexpected native event: {method}")


def exchange(read, write, work, report):
    def send(frame):
        report["sent_frames"].append(frame)
        write(frame)

    def receive():
        frame = read()
        if frame is None:
            raise EOFError("native stdout closed before completion")
        if isinstance(frame, Exception):
            raise frame
        report["events"].append(frame)
        if isinstance(frame, dict) and frame.get("method") and "id" in frame:
            send(
                {
                    "id": frame["id"],
                    "error": {
                        "code": -32601,
                        "message": "registry fixture denies all action requests",
                    },
                }
            )
        inspect_frame(frame)
        return frame

    def rpc(number, method, params):
        send({"id": number, "method": method, "params": params})
        while True:
            frame = receive()
            if frame.get("id") == number:
                if "error" in frame:
                    raise ValueError(f"{method} failed: {frame['error']}")
                return frame["result"]

    initialized = rpc(
        1,
        "initialize",
        {
            "clientInfo": {"name": "windows_registry_fixture", "version": "1"},
            "capabilities": {"experimentalApi": True},
        },
    )
    expected_home = str(work.parent / "home" / "codex")
    if os.path.normcase(initialized.get("codexHome", "")) != os.path.normcase(expected_home):
        raise ValueError("native CODEX_HOME does not match private fixture")
    send({"method": "initialized"})
    started = rpc(
        2,
        "thread/start",
        {
            "model": MODEL,
            "modelProvider": "policy_fixture",
            "cwd": str(work),
            "ephemeral": True,
            "allowProviderModelFallback": False,
            "dynamicTools": [],
            "environments": [],
            "selectedCapabilityRoots": [],
            "runtimeWorkspaceRoots": [],
            "sandbox": "read-only",
            "approvalPolicy": "untrusted",
            "approvalsReviewer": "user",
        },
    )
    for key, expected in {
        "model": MODEL,
        "modelProvider": "policy_fixture",
        "runtimeWorkspaceRoots": [],
        "approvalPolicy": "untrusted",
        "approvalsReviewer": "user",
        "sandbox": {"type": "readOnly", "networkAccess": False},
    }.items():
        if started.get(key) != expected:
            raise ValueError(f"unexpected effective thread {key}")
    thread_id = started["thread"]["id"]
    turn = rpc(
        3,
        "turn/start",
        {
            "threadId": thread_id,
            "model": MODEL,
            "input": [{"type": "text", "text": "Return fixture complete."}],
        },
    )
    retained = iter(list(report["events"]))
    while True:
        frame = next(retained, None)
        if frame is None:
            frame = receive()
        if frame.get("method") == "turn/completed":
            params = frame["params"]
            if params["threadId"] != thread_id or params["turn"]["id"] != turn["turn"]["id"]:
                raise ValueError("unexpected completion identity")
            report["terminal_status"] = params["turn"]["status"]
            if params["turn"].get("error"):
                raise ValueError("native turn failed")
            return


def capture(stream, path, frames, stdout):
    limit = OUTPUT_LIMIT if stdout else STDERR_LIMIT
    total = 0
    try:
        with path.open("xb", buffering=0) as raw:
            while True:
                data = stream.readline(FRAME_LIMIT + 1) if stdout else stream.read1(4096)
                if not data:
                    break
                raw.write(data[: max(0, min(limit - total, FRAME_LIMIT if stdout else 4096))])
                total += len(data)
                if total > limit or (stdout and len(data) > FRAME_LIMIT):
                    raise ValueError("native output limit exceeded")
                if stdout:
                    frames.put(json.loads(data))
    except Exception as exc:
        frames.put(exc)
    finally:
        if stdout:
            frames.put(None)


def require_patch_denial(body, injected):
    history = [item for item in body.get("input", []) if "call" in item.get("type", "")]
    if len(history) != 2 or any(history[0].get(k) != v for k, v in injected.items()):
        raise ValueError("unexpected injected-call denial history")
    output = history[1]
    if (
        output.get("type") != "custom_tool_call_output"
        or output.get("call_id") != injected["call_id"]
        or output.get("output") != "unsupported custom tool call: apply_patch"
    ):
        raise ValueError("missing correlated apply_patch router denial")


def fixture_handler(report, frames, root):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def setup(self):
            self.request.settimeout(2)
            super().setup()

        def reject(self, message):
            frames.put(ValueError(message))
            self.send_error(400)

        def do_GET(self):
            self.reject("unexpected fixture HTTP method")

        do_HEAD = do_PUT = do_PATCH = do_DELETE = do_OPTIONS = do_GET

        def do_POST(self):
            try:
                patch_mode = report.get("mode", "registry") == "apply-patch"
                index = len(report["requests"])
                if self.path != "/v1/responses" or index >= (2 if patch_mode else 1):
                    raise ValueError("unexpected fixture path or extra request")
                if self.headers.get("Authorization") or self.headers.get("api-key"):
                    raise ValueError("unexpected authentication header")
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= OUTPUT_LIMIT:
                    raise ValueError("fixture request size limit exceeded")
                raw_body = self.rfile.read(length)
                suffix = "" if index == 0 else "-2"
                (root / f"fixture-request{suffix}.json").write_bytes(raw_body)
                body = json.loads(raw_body)
                report["requests"].append({"path": self.path, "body": body})
                if body.get("model") != MODEL:
                    raise ValueError("unexpected model")
                if patch_mode:
                    if body.get("tools") or any(
                        item.get("tools")
                        for item in body.get("input", [])
                        if item.get("type") == "additional_tools"
                    ):
                        raise ValueError("refusing injection with nonempty registry")
                    if index == 0 and any(
                        "call" in item.get("type", "") for item in body.get("input", [])
                    ):
                        raise ValueError("unexpected tool history before injection")
                    if index == 1:
                        require_patch_denial(body, report["injected_call"])
                message = {
                    "type": "message",
                    "id": "msg_fixture",
                    "role": "assistant",
                    "status": "completed",
                    "content": [{"type": "output_text", "text": "fixture complete"}],
                }
                if patch_mode and index == 0:
                    message = {
                        "type": "custom_tool_call",
                        "id": "fc_fixture_apply_patch",
                        "call_id": "call_policy_apply_patch",
                        "name": "apply_patch",
                        "input": (
                            "*** Begin Patch\n"
                            f"*** Add File: {root / 'work' / 'tool-must-not-create'}\n"
                            "+synthetic fixture only\n*** End Patch\n"
                        ),
                    }
                    report["injected_call"] = message
                response = {
                    "id": "resp_fixture" if index == 0 else "resp_fixture_2",
                    "model": MODEL,
                    "status": "completed",
                    "output": [message],
                    "usage": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
                }
                events = [
                    ("response.created", {"response": {**response, "status": "in_progress"}}),
                    ("response.output_item.done", {"output_index": 0, "item": message}),
                    ("response.completed", {"response": response}),
                ]
                report["fixture_responses"].append(events)
                (root / f"fixture-response{suffix}.json").write_text(
                    json.dumps(events), encoding="utf-8"
                )
                payload = "".join(
                    f"event: {kind}\ndata: " + json.dumps({"type": kind, **data}) + "\n\n"
                    for kind, data in events
                ).encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
            except Exception as exc:
                self.reject(f"fixture failed: {type(exc).__name__}: {exc}")

    return Handler


def validate_result(report):
    patch_mode = report.get("mode", "registry") == "apply-patch"
    summaries = []
    for index, request in enumerate(report["requests"]):
        body = request["body"]
        tools = list(body.get("tools", []))
        for item in body.get("input", []):
            if item.get("type") == "additional_tools":
                tools.extend(item.get("tools", []))
            if item.get("type") in {
                "function_call",
                "custom_tool_call",
                "function_call_output",
                "custom_tool_call_output",
            }:
                if not patch_mode or index != 1:
                    raise ValueError("unexpected tool history in request")
        names = []
        for tool in tools:
            if tool["type"] == "namespace":
                names.extend(f"{tool['name']}.{child['name']}" for child in tool["tools"])
            else:
                names.append(tool.get("name", tool["type"]))
        summaries.append({"offered_tools": names, "tool_choice": body.get("tool_choice")})
    report["request_summaries"] = summaries
    expected_count = 2 if patch_mode else 1
    if len(summaries) != expected_count or any(s["offered_tools"] for s in summaries):
        raise ValueError("effective registry is not empty or request count differs")
    if len(report["fixture_responses"]) != expected_count:
        raise ValueError("fixture response count differs")
    if patch_mode:
        require_patch_denial(report["requests"][1]["body"], report["injected_call"])
        if report.get("canary_exists") is not False:
            raise ValueError("canary absence not established")
    if (
        report["error"]
        or report["terminal_status"] != "completed"
        or report["process_returncode"] != 0
    ):
        raise ValueError("native run did not complete successfully")


def probe(executable, output_dir, mode="registry"):
    if mode not in {"registry", "apply-patch"}:
        raise ValueError("unsupported diagnostic mode")
    if sys.platform != "win32":
        raise ValueError("Windows registry probe requires Windows; no platform override")
    if not executable.is_absolute() or not output_dir.is_absolute():
        raise ValueError("executable and output directory must be absolute")
    output_dir.mkdir()  # Fresh retained root; never overwrite or reuse existing state.
    root = output_dir.resolve()
    report = {
        "binary_sha256": PIN,
        "mode": mode,
        "apply_patch_denial_passed": False,
        "native_pid": None,
        "process_returncode": None,
        "error": None,
        "terminal_status": None,
        "requests": [],
        "fixture_responses": [],
        "sent_frames": [],
        "events": [],
        "cleanup": [],
        "registry_only_passed": False,
        "restrictions_verified": False,
        "provider_verified": False,
        "network_isolation_verified": False,
        "sandbox_identity_verified": False,
    }
    process = server = server_thread = None
    readers = []
    frames = queue.Queue()
    try:
        check_binary(executable)
        system_root = os.environ.get("SystemRoot", "")
        env = windows_env(root, system_root)
        for path in {env[k] for k in ["HOME", "CODEX_HOME", "APPDATA", "LOCALAPPDATA", "TEMP"]}:
            Path(path).mkdir(parents=True, exist_ok=True)
        work = root / "work"
        work.mkdir()
        Path(env["CODEX_HOME"], "config.toml").write_text("[mcp_servers]\n", encoding="utf-8")
        server = HTTPServer(("127.0.0.1", 0), fixture_handler(report, frames, root))
        config = configuration(server.server_address[1])
        command = [str(executable), "app-server", "--listen", "stdio://", "--strict-config"]
        for key, value in config.items():
            command.extend(["-c", f"{key}={json.dumps(value)}"])
        report.update({"command": command, "config": config, "environment": env, "cwd": str(work)})
        (root / "prelaunch.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
        server_thread = threading.Thread(target=server.serve_forever, daemon=True)
        server_thread.start()
        deadline = time.monotonic() + DEADLINE_SECONDS
        process = subprocess.Popen(
            command,
            cwd=work,
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        report["native_pid"] = process.pid
        (root / "native-pid.json").write_text(
            json.dumps({"native_pid": process.pid}), encoding="utf-8"
        )
        for stream, name, stdout in [
            (process.stdout, "stdout.jsonl", True),
            (process.stderr, "stderr.txt", False),
        ]:
            reader = threading.Thread(
                target=capture, args=(stream, root / name, frames, stdout), daemon=True
            )
            reader.start()
            readers.append(reader)

        def read():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("native protocol deadline exceeded")
            return frames.get(timeout=remaining)

        def write(frame):
            data = json.dumps(frame).encode() + b"\n"
            with (root / "sent-frames.jsonl").open("ab", buffering=0) as journal:
                journal.write(data)
            process.stdin.write(data)
            process.stdin.flush()

        exchange(read, write, work, report)
    except Exception as exc:
        report["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        if process is not None:
            try:
                process.stdin.close()
                report["cleanup"].append("stdin closed")
            except OSError as exc:
                report["cleanup"].append(f"stdin close failed: {exc}")
            try:
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    process.terminate()
                    report["cleanup"].append("owned process terminate")
                    try:
                        process.wait(timeout=3)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        report["cleanup"].append("owned process kill")
                        process.wait(timeout=3)
            except Exception as exc:
                report["error"] = report["error"] or f"cleanup failed: {exc}"
            report["process_returncode"] = process.poll()
            for reader in readers:
                reader.join(timeout=1)
            if any(reader.is_alive() for reader in readers):
                report["error"] = report["error"] or "native output reader did not stop"
            else:
                process.stdout.close()
                process.stderr.close()
        if server_thread is not None:
            server.shutdown()
            server_thread.join(timeout=3)
        if server is not None:
            server.server_close()
        while not frames.empty():
            frame = frames.get_nowait()
            if frame is None:
                continue
            try:
                if isinstance(frame, Exception):
                    raise frame
                report["events"].append(frame)
                inspect_frame(frame)
            except Exception as exc:
                report["error"] = report["error"] or str(exc)
        report["cleanup_owned_process_exited"] = process is not None and process.poll() is not None
        report["descendant_cleanup_verified"] = False
        report["canary_exists"] = os.path.lexists(root / "work" / "tool-must-not-create")
        try:
            validate_result(report)
            report[
                "apply_patch_denial_passed" if mode == "apply-patch" else "registry_only_passed"
            ] = True
        except Exception as exc:
            report["error"] = report["error"] or str(exc)
        with (root / "result.json").open("x", encoding="utf-8") as stream:
            json.dump(report, stream, indent=2)
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--executable", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--mode", choices=("registry", "apply-patch"), default="registry")
    args = parser.parse_args(argv)
    try:
        report = probe(args.executable, args.output_dir, args.mode)
    except Exception as exc:
        print(json.dumps({"error": f"{type(exc).__name__}: {exc}"}))
        return 1
    print(
        json.dumps(
            {
                "result": str(args.output_dir / "result.json"),
                "registry_only_passed": report["registry_only_passed"],
                "apply_patch_denial_passed": report["apply_patch_denial_passed"],
                "error": report["error"],
            }
        )
    )
    return 0 if report["registry_only_passed"] or report["apply_patch_denial_passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
