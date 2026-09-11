"""Default-offline contract probe; explicit managed login and proof-gated synthetic smoke."""

import argparse
import hashlib
import json
import time
from pathlib import Path
from typing import Any

from oms_hub.llm.codex_session import (
    INSPECTED_MACOS_BINARY_SHA256,
    PINNED_SCHEMA_SHA256,
    PINNED_VERSION,
    CodexSessionClient,
    SessionError,
    SessionRequest,
    model_ready,
)

# Synthetic payloads only. These are protocol-member fixtures, not a live session.
# B2 adds response correlation, output reduction, and subprocess lifecycle replay.
FIXTURES: tuple[tuple[str, dict[str, Any]], ...] = (
    ("InitializeParams", {"clientInfo": {"name": "oms-offline", "version": "1"}}),
    (
        "InitializeResponse",
        {
            "codexHome": "/synthetic/session",
            "platformFamily": "unix",
            "platformOs": "macos",
            "userAgent": "codex-cli/0.153.4",
        },
    ),
    ("ClientNotification", {"method": "initialized"}),
    ("v2/LoginAccountParams", {"type": "chatgptDeviceCode"}),
    ("v2/LoginAccountParams", {"type": "chatgpt"}),
    (
        "v2/LoginAccountResponse",
        {
            "type": "chatgptDeviceCode",
            "loginId": "login-fixture",
            "verificationUrl": "https://example.invalid/device",
            "userCode": "FAKE-CODE",
        },
    ),
    (
        "v2/LoginAccountResponse",
        {
            "type": "chatgpt",
            "loginId": "login-fixture",
            "authUrl": "https://example.invalid/browser",
        },
    ),
    ("v2/CancelLoginAccountParams", {"loginId": "login-fixture"}),
    ("v2/CancelLoginAccountResponse", {"status": "canceled"}),
    ("v2/AccountLoginCompletedNotification", {"loginId": "login-fixture", "success": True}),
    ("v2/GetAccountParams", {"refreshToken": False}),
    ("v2/GetAccountResponse", {"account": None, "requiresOpenaiAuth": True}),
    ("v2/ModelListParams", {"cursor": "page-two", "limit": 10}),
    ("v2/ModelListResponse", {"data": [], "nextCursor": "page-two"}),
    ("v2/ModelListResponse", {"data": [], "nextCursor": None}),
    ("v2/GetAccountRateLimitsResponse", {"rateLimits": {}, "rateLimitsByLimitId": None}),
    ("v2/AccountRateLimitsUpdatedNotification", {"rateLimits": {}}),
    ("v2/ThreadStartParams", {"model": "chosen", "cwd": "/synthetic/staged"}),
    (
        "v2/ThreadStartResponse",
        {
            "approvalPolicy": "untrusted",
            "approvalsReviewer": "user",
            "cwd": "/synthetic/staged",
            "model": "chosen",
            "modelProvider": "openai",
            "sandbox": {"type": "readOnly", "networkAccess": False},
            "thread": {
                "id": "thread-fixture",
                "sessionId": "session-fixture",
                "cliVersion": "0.153.4",
                "createdAt": 0,
                "updatedAt": 0,
                "cwd": "/synthetic/staged",
                "ephemeral": True,
                "modelProvider": "openai",
                "preview": "synthetic",
                "projectId": None,
                "source": "appServer",
                "status": {"type": "idle"},
                "turns": [],
            },
        },
    ),
    (
        "v2/TurnStartParams",
        {
            "threadId": "thread-fixture",
            "input": [
                {"type": "text", "text": "Return synthetic JSON."},
                {"type": "localImage", "path": "/synthetic/staged/image.png"},
            ],
            "outputSchema": {"type": "object"},
        },
    ),
    ("v2/TurnStartResponse", {"turn": {"id": "turn-fixture", "items": [], "status": "inProgress"}}),
    (
        "v2/TurnStartedNotification",
        {
            "threadId": "thread-fixture",
            "turn": {"id": "turn-fixture", "items": [], "status": "inProgress"},
        },
    ),
    ("v2/TurnInterruptParams", {"threadId": "thread-fixture", "turnId": "turn-fixture"}),
    ("v2/TurnInterruptResponse", {}),
    (
        "v2/ItemCompletedNotification",
        {
            "threadId": "thread-fixture",
            "turnId": "turn-fixture",
            "completedAtMs": 0,
            "item": {
                "type": "agentMessage",
                "id": "item-fixture",
                "phase": "final_answer",
                "text": '{"synthetic":true}',
            },
        },
    ),
    (
        "v2/TurnCompletedNotification",
        {
            "threadId": "thread-fixture",
            "turn": {"id": "turn-fixture", "items": [], "status": "completed"},
        },
    ),
    (
        "v2/TurnCompletedNotification",
        {
            "threadId": "thread-fixture",
            "turn": {
                "id": "turn-fixture",
                "items": [],
                "status": "failed",
                "error": {"message": "synthetic failure"},
            },
        },
    ),
    (
        "v2/ErrorNotification",
        {
            "threadId": "thread-fixture",
            "turnId": "turn-fixture",
            "willRetry": False,
            "error": {"message": "synthetic failure"},
        },
    ),
    ("CommandExecutionRequestApprovalResponse", {"decision": "decline"}),
    (
        "CommandExecutionRequestApprovalParams",
        {
            "itemId": "tool-fixture",
            "startedAtMs": 0,
            "threadId": "thread-fixture",
            "turnId": "turn-fixture",
        },
    ),
)

REQUEST_METHODS = (
    "initialize",
    "account/login/start",
    "account/login/cancel",
    "account/read",
    "model/list",
    "account/rateLimits/read",
    "thread/start",
    "turn/start",
    "turn/interrupt",
)


def check_fixture_members(schema: dict[str, Any], payload: dict[str, Any]) -> None:
    """Check top-level required/known members and discriminator; not JSON Schema validation."""
    if "oneOf" in schema:
        matches = [
            branch
            for branch in schema["oneOf"]
            if all(
                payload.get(key) in value["enum"]
                for key, value in branch.get("properties", {}).items()
                if "enum" in value
            )
        ]
        if len(matches) != 1:
            raise ValueError("fixture discriminator mismatch")
        schema = matches[0]
    if not set(schema.get("required", ())).issubset(payload):
        raise ValueError("fixture missing required members")
    if not set(payload).issubset(schema.get("properties", {})):
        raise ValueError("fixture has unknown members")


def offline_probe(schema_path: Path, version: str) -> dict[str, object]:
    if version != PINNED_VERSION:
        raise ValueError("incompatible Codex version")
    raw = schema_path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != PINNED_SCHEMA_SHA256:
        raise ValueError("incompatible Codex schema")
    definitions = json.loads(raw)["definitions"]
    methods = {
        branch["properties"]["method"]["enum"][0]
        for branch in definitions["ClientRequest"]["oneOf"]
    }
    if not set(REQUEST_METHODS).issubset(methods):
        raise ValueError("missing required protocol method")
    # Round-trip each fixture as a JSONL frame; no fake result implies provider readiness.
    for name, payload in FIXTURES:
        schema = definitions
        for part in name.split("/"):
            schema = schema[part]
        check_fixture_members(schema, json.loads(json.dumps(payload) + "\n"))
    if model_ready({"model": "chosen"}, images=True):
        raise ValueError("missing metadata enabled images")
    return {
        "version": version,
        "schema_sha256": PINNED_SCHEMA_SHA256,
        "fixture_member_checks": len(FIXTURES),
        "offline_contract": "passed",
        "windows_verified": False,
        "provider_verified": False,
        "restrictions_verified": False,
        "live_ready": False,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--offline", action="store_true", help="default; inspect existing schema only"
    )
    mode.add_argument("--login", action="store_true", help="start managed login; coordinate with O")
    mode.add_argument(
        "--smoke", action="store_true", help="synthetic turn; requires verified execution policy"
    )
    parser.add_argument(
        "--schema", type=Path, help="exported codex_app_server_protocol.schemas.json"
    )
    parser.add_argument("--version", default=PINNED_VERSION, help="recorded codex --version output")
    parser.add_argument("--executable", type=Path)
    parser.add_argument("--session-home", type=Path)
    parser.add_argument("--work-root", type=Path)
    parser.add_argument("--binary-sha256", default=INSPECTED_MACOS_BINARY_SHA256)
    parser.add_argument("--model", help="exact model slug for an authorized synthetic smoke")
    parser.add_argument("--browser-login", action="store_true")
    parser.add_argument("--login-timeout", type=float, default=180)
    args = parser.parse_args(argv)
    if args.login or args.smoke:
        if not all((args.executable, args.session_home, args.work_root)):
            parser.error("live modes require --executable, --session-home and --work-root")
        if args.smoke and not args.model:
            parser.error("--smoke requires --model")
        if not 0 < args.login_timeout <= 600:
            parser.error("--login-timeout must be between 0 and 600 seconds")
        return live_probe(args)
    if args.schema is None:
        parser.error("--schema is required in offline mode; no executable is launched")
    try:
        report = offline_probe(args.schema, args.version)
    except (OSError, ValueError, KeyError, TypeError):
        parser.exit(
            1, "Offline compatibility check failed; inspect the version and schema export.\n"
        )
    print(json.dumps(report, sort_keys=True))
    return 0


def live_probe(args: argparse.Namespace) -> int:
    """Only called by an explicit CLI mode; no credential files are opened by Hub."""
    client = CodexSessionClient(
        args.executable, args.session_home, args.work_root, binary_sha256=args.binary_sha256
    )
    challenge = None
    try:
        if args.login:
            challenge = client.start_login(device_code=not args.browser_login)
            print(
                json.dumps(
                    {
                        "login_id": challenge.login_id,
                        "url": challenge.url,
                        "user_code": challenge.user_code,
                    }
                ),
                flush=True,
            )
            deadline = time.monotonic() + args.login_timeout
            while time.monotonic() < deadline:
                status = client.status()
                if status.account_connected and status.state != "connecting":
                    print(json.dumps({"managed_login": "connected", "live_ready": False}))
                    challenge = None
                    return 0
                if status.state not in ("connecting", "disconnected"):
                    raise SessionError(status.error_code or "protocol_error")
                time.sleep(0.25)
            raise SessionError("timeout")
        result = client.generate(
            SessionRequest(
                "synthetic-probe",
                args.model,
                "Return a JSON object with ok=true.",
                "Synthetic transport check; no source material.",
                output_schema={
                    "type": "object",
                    "properties": {"ok": {"const": True}},
                    "required": ["ok"],
                    "additionalProperties": False,
                },
            ),
            cancelled=lambda: False,
            on_lifecycle=lambda event: print(
                json.dumps(
                    {"phase": event.phase, "thread_id": event.thread_id, "turn_id": event.turn_id}
                ),
                flush=True,
            ),
        )
        if json.loads(result.text) != {"ok": True}:
            raise SessionError("invalid_output")
        print(
            json.dumps(
                {
                    "synthetic_smoke": "passed",
                    "thread_id": result.thread_id,
                    "turn_id": result.turn_id,
                }
            )
        )
        return 0
    except (SessionError, KeyboardInterrupt) as error:
        print(
            json.dumps(
                {
                    "error_code": error.code if isinstance(error, SessionError) else "interrupted",
                    "live_ready": False,
                }
            )
        )
        return 1
    finally:
        if challenge is not None:
            try:
                client.cancel_login(challenge.login_id)
            except SessionError:
                pass
        client.close()


if __name__ == "__main__":
    raise SystemExit(main())
