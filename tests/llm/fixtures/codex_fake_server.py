"""Source-free pipe peer for exercising the production session transport. No network."""

import json
import os
import sys
import time
from pathlib import Path

scenario, trace_path = sys.argv[1:3]
trace = Path(trace_path)
pending_login = False


def emit(payload):
    print(json.dumps(payload), flush=True)


def log(payload):
    with trace.open("a") as output:
        output.write(json.dumps(payload) + "\n")


def completed(status):
    return {
        "method": "turn/completed",
        "params": {
            "threadId": "thread-fixture",
            "turn": {"id": "turn-fixture", "items": [], "status": status},
        },
    }


log(
    {
        "startup": True,
        "args": sys.argv[3:],
        "env": dict(os.environ),
        "cwd": os.getcwd(),
        "api_key_inherited": "OPENAI_API_KEY" in os.environ,
        "codex_home": os.environ.get("CODEX_HOME"),
    }
)
if scenario == "no_read":
    time.sleep(30)
    raise SystemExit

for raw in sys.stdin:
    request = json.loads(raw)
    log(request)
    method, request_id = request.get("method"), request.get("id")
    result = {}
    if method is None:
        continue
    if method == "initialized":
        continue
    if method == "initialize":
        if scenario == "no_initialize":
            continue
        result = {
            "codexHome": "wrong" if scenario == "wrong_home" else os.environ["CODEX_HOME"],
            "platformFamily": "unix",
            "platformOs": "macos",
            "userAgent": "codex-cli/0.153.4",
        }
        if scenario == "malformed_home":
            result["codexHome"] = None
    elif method == "account/read":
        if pending_login and scenario != "logged_out":
            emit(
                {
                    "method": "account/login/completed",
                    "params": {"loginId": "login-fixture", "success": True},
                }
            )
            pending_login = False
        result = {
            "account": None
            if scenario == "logged_out"
            else {"type": "chatgpt", "email": "fixture@example.invalid", "planType": "plus"},
            "requiresOpenaiAuth": True,
        }
    elif method == "model/list":
        second = request["params"].get("cursor")
        result = {
            "data": [
                {"model": "other" if second else "chosen", "inputModalities": ["text", "image"]}
            ],
            "nextCursor": None if second else "next-page",
        }
    elif method == "account/rateLimits/read":
        result = {"rateLimits": {}, "rateLimitsByLimitId": None}
        if scenario in ("limited", "limited_unknown"):
            window = {"usedPercent": 100}
            if scenario == "limited":
                window["resetsAt"] = 2000000000
            result["rateLimits"] = {"primary": window}
    elif method == "account/login/start":
        pending_login = True
        kind = request["params"]["type"]
        result = {"type": kind, "loginId": "login-fixture"}
        result.update(
            {"verificationUrl": "https://example.invalid/device", "userCode": "FAKE"}
            if kind == "chatgptDeviceCode"
            else {"authUrl": "https://example.invalid/auth"}
        )
    elif method == "account/login/cancel":
        result = {"status": "canceled"}
    elif method == "thread/start":
        result = {
            "thread": {"id": "thread-fixture"},
            "model": request["params"].get("model"),
            "modelProvider": "openai",
            "runtimeWorkspaceRoots": [],
            "approvalPolicy": "untrusted",
            "approvalsReviewer": "user",
            "sandbox": {"type": "readOnly", "networkAccess": scenario == "policy_drift"},
        }
        if scenario.startswith("policy_drift:"):
            result[scenario.split(":", 1)[1]] = None
    elif method == "turn/interrupt":
        emit({"id": request_id, "result": {}})
        emit(completed("interrupted"))
        continue
    elif method == "turn/start":
        result = {"turn": {"id": "turn-fixture", "items": [], "status": "inProgress"}}
        if scenario == "early_notification":
            emit(
                {
                    "method": "turn/started",
                    "params": {"threadId": "thread-fixture", "turn": result["turn"]},
                }
            )
        emit({"id": request_id, "result": result})
        if scenario == "hang":
            continue
        if scenario == "eof":
            raise SystemExit
        if scenario == "approval":
            emit(
                {
                    "id": "tool-fixture",
                    "method": "item/commandExecution/requestApproval",
                    "params": {
                        "threadId": "thread-fixture",
                        "turnId": "turn-fixture",
                        "itemId": "tool-fixture",
                        "startedAtMs": 0,
                    },
                }
            )
            continue
        if scenario == "tool_started":
            emit(
                {
                    "method": "item/started",
                    "params": {
                        "threadId": "thread-fixture",
                        "turnId": "turn-fixture",
                        "item": {"type": "commandExecution"},
                    },
                }
            )
            continue
        if scenario == "malformed":
            print("{BROKEN", flush=True)
            continue
        if scenario == "large":
            print("x" * (4 * 1024 * 1024 + 1), flush=True)
            continue
        if scenario == "aggregate":
            for _ in range(10):
                emit({"method": "fixture", "params": {"text": "x" * 1000000}})
            continue
        if scenario == "stderr":
            sys.stderr.write("private synthetic stderr" * 10000)
            sys.stderr.flush()
        if scenario == "failed":
            failure = completed("failed")
            failure["params"]["turn"]["error"] = {
                "message": "PRIVATE RAW",
                "codexErrorInfo": "usageLimitExceeded",
            }
            emit(failure)
            continue
        for line in Path(__file__).with_name("codex_session_events.jsonl").read_text().splitlines():
            event = json.loads(line)
            if scenario == "bad_json" and event.get("method") == "item/completed":
                event["params"]["item"]["text"] = "not JSON"
            emit(event)
        continue
    emit({"id": request_id, "result": result})
