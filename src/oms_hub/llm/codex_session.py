"""Owned, bounded Codex stdio transport. Live generation remains proof-gated."""

import hashlib
import json
import math
import os
import queue
import subprocess
import tempfile
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, cast
from urllib.parse import urlsplit

PINNED_VERSION = "codex-cli 0.153.4"
PINNED_SCHEMA_SHA256 = "e8284c5cb8157554a3dd1e035aadbd4325aea501af56887e9c2e12eb1b9b9448"
INSPECTED_MACOS_BINARY_SHA256 = "87a08119b8effa519f0ecb552dc98043f58a8200bf2ec5da60f76890c33e9c3a"


@dataclass(frozen=True)
class SessionRequest:
    request_id: str
    model: str
    instructions: str
    source_text: str
    image_paths: tuple[Path, ...] = ()
    output_schema: dict[str, object] | None = None
    image_sha256: tuple[str, ...] = ()


@dataclass(frozen=True)
class SessionResult:
    thread_id: str
    turn_id: str
    text: str


@dataclass(frozen=True)
class SessionLifecycle:
    request_id: str
    phase: Literal[
        "dispatching", "thread_created", "turn_started", "completed", "failed", "interrupted"
    ]
    thread_id: str | None = None
    turn_id: str | None = None


@dataclass(frozen=True)
class SessionStatus:
    state: Literal["disconnected", "connecting", "connected", "limited", "unavailable"]
    model_ids: tuple[str, ...]
    image_model_ids: tuple[str, ...]
    reset_at: str | None
    error_code: str | None
    account_connected: bool = False


@dataclass(frozen=True)
class LoginChallenge:
    login_id: str
    url: str
    user_code: str | None


def model_ready(model: dict[str, object], *, images: bool) -> bool:
    """Check advertised inputs only, never auth, schema support, or live readiness."""
    name = model.get("model")
    modalities = model.get("inputModalities")
    return (
        isinstance(name, str)
        and bool(name.strip())
        and isinstance(modalities, list)
        and all(isinstance(value, str) for value in modalities)
        and "text" in modalities
        and (not images or "image" in modalities)
    )


ErrorCode = Literal[
    "auth_required",
    "rate_limited",
    "model_unavailable",
    "capability_unverified",
    "context_limit",
    "invalid_output",
    "timeout",
    "interrupted",
    "tool_request_denied",
    "protocol_error",
]
_MESSAGES: dict[str, str] = {
    "auth_required": "Connect the managed ChatGPT account.",
    "rate_limited": "The subscription is limited; resume after the limit resets.",
    "model_unavailable": "The selected model is unavailable.",
    "capability_unverified": "Session capabilities and tool restrictions are not verified.",
    "context_limit": "The request exceeds the session input limit.",
    "invalid_output": "The session returned invalid output.",
    "timeout": "The session timed out; inspect the interrupted attempt before resuming.",
    "interrupted": "The session was interrupted; it will not be replayed automatically.",
    "tool_request_denied": "The session requested a prohibited tool.",
    "protocol_error": "The session protocol failed.",
}
MAX_FRAME_BYTES = 4 * 1024 * 1024
MAX_OUTPUT_BYTES = 8 * 1024 * 1024
MAX_STDERR_BYTES = 64 * 1024
_POLL = 0.05
_SAFE_ITEMS = {"userMessage", "agentMessage", "reasoning"}


class SessionError(RuntimeError):
    def __init__(self, code: str, *, retryable: bool = False, reset_at: str | None = None):
        self.code = cast(ErrorCode, code if code in _MESSAGES else "protocol_error")
        self.retryable = retryable
        self.reset_at: str | None = None
        if isinstance(reset_at, str):
            try:
                parsed = datetime.fromisoformat(reset_at)
                if parsed.tzinfo is not None and parsed.utcoffset() == UTC.utcoffset(None):
                    self.reset_at = parsed.astimezone(UTC).isoformat()
            except ValueError:
                pass
        super().__init__(_MESSAGES[self.code])


def _object(value: object) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise SessionError("protocol_error")
    return value


def _identifier(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise SessionError("protocol_error")
    return value


def _remote_error(value: object) -> SessionError:
    error = value if isinstance(value, dict) else {}
    info = error.get(
        "codexErrorInfo",
        error.get("data", {}).get("codexErrorInfo")
        if isinstance(error.get("data"), dict)
        else None,
    )
    mapped = {
        "unauthorized": "auth_required",
        "usageLimitExceeded": "rate_limited",
        "rateLimitExceeded": "rate_limited",
        "contextWindowExceeded": "context_limit",
    }
    if isinstance(info, dict) and "responseStreamDisconnected" in info:
        return SessionError("interrupted")
    return SessionError(
        mapped.get(info, "protocol_error") if isinstance(info, str) else "protocol_error"
    )


def _check_tool_event(event: dict[str, Any]) -> None:
    if "id" in event and "method" in event:
        raise SessionError("tool_request_denied")
    if event.get("method") in ("item/started", "item/completed"):
        item = _object(_object(event.get("params")).get("item"))
        if item.get("type") not in _SAFE_ITEMS:
            raise SessionError("tool_request_denied")


def collect_completed_text(events: list[dict[str, object]], *, thread_id: str, turn_id: str) -> str:
    """Reduce final items, never text deltas, and require matching successful completion."""
    messages: dict[str, dict[str, Any]] = {}
    completed = False
    for event in events:
        _check_tool_event(event)
        params = event.get("params")
        if not isinstance(params, dict) or params.get("threadId") != thread_id:
            continue
        method = event.get("method")
        if method == "turn/completed":
            turn = _object(params.get("turn"))
            if turn.get("id") != turn_id:
                continue
            if turn.get("status") == "interrupted":
                raise SessionError("interrupted")
            if turn.get("status") != "completed":
                raise _remote_error(turn.get("error"))
            completed = True
            items = turn.get("items")
            if not isinstance(items, list):
                raise SessionError("protocol_error")
        elif params.get("turnId") != turn_id:
            continue
        elif method == "error":
            raise _remote_error(params.get("error"))
        elif method == "item/completed":
            items = [params.get("item")]
        else:
            continue
        for raw_item in items:
            item = _object(raw_item)
            if item.get("type") not in _SAFE_ITEMS:
                raise SessionError("tool_request_denied")
            if item.get("type") == "agentMessage":
                messages[_identifier(item.get("id"))] = item
    if not completed:
        raise SessionError("interrupted")
    final = [item for item in messages.values() if item.get("phase") == "final_answer"]
    if not final:
        final = [item for item in messages.values() if item.get("phase") is None]
    if not final or not isinstance(final[-1].get("text"), str) or not final[-1]["text"].strip():
        raise SessionError("invalid_output")
    return cast(str, final[-1]["text"])


class _Stdio:
    """Three bounded pipe workers make reads and writes cancellable on Windows too."""

    def __init__(
        self, command: list[str], *, cwd: Path, env: dict[str, str], shutdown_timeout: float
    ):
        self.shutdown_timeout = shutdown_timeout
        self.process = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=cwd,
            env=env,
            shell=False,
        )
        self.incoming: queue.Queue[dict[str, Any] | None] = queue.Queue(maxsize=128)
        self.outgoing: queue.Queue[tuple[bytes, threading.Event, list[bool]] | None] = queue.Queue(
            1
        )
        self.read_error: SessionError | None = None
        self.stderr_tail = bytearray()
        self.stopped = threading.Event()
        self.threads = [
            threading.Thread(target=target, daemon=True)
            for target in (self._read, self._write, self._stderr)
        ]
        for thread in self.threads:
            thread.start()

    def _read(self) -> None:
        assert self.process.stdout is not None
        total = 0
        try:
            while not self.stopped.is_set():
                raw = self.process.stdout.readline(MAX_FRAME_BYTES + 1)
                if not raw:
                    self.incoming.put_nowait(None)
                    return
                total += len(raw)
                if (
                    len(raw) > MAX_FRAME_BYTES
                    or total > MAX_OUTPUT_BYTES
                    or not raw.endswith(b"\n")
                ):
                    raise SessionError("protocol_error")
                frame = _object(json.loads(raw))
                self.incoming.put_nowait(frame)
        except (ValueError, RecursionError, OSError, queue.Full, SessionError):
            self.read_error = SessionError("protocol_error")

    def _stderr(self) -> None:
        assert self.process.stderr is not None
        try:
            while not self.stopped.is_set():
                data = os.read(self.process.stderr.fileno(), 4096)
                if not data:
                    return
                self.stderr_tail.extend(data)
                del self.stderr_tail[:-MAX_STDERR_BYTES]
        except (OSError, ValueError):
            pass

    def _write(self) -> None:
        assert self.process.stdin is not None
        while not self.stopped.is_set():
            try:
                work = self.outgoing.get(timeout=_POLL)
            except queue.Empty:
                continue
            if work is None:
                return
            data, done, failed = work
            try:
                self.process.stdin.write(data)
                self.process.stdin.flush()
            except (OSError, ValueError):
                failed.append(True)
            finally:
                done.set()

    @staticmethod
    def _check_wait(deadline: float, cancelled: Callable[[], bool]) -> None:
        if cancelled():
            raise SessionError("interrupted")
        if time.monotonic() >= deadline:
            raise SessionError("timeout")

    def send(self, frame: dict[str, Any], deadline: float, cancelled: Callable[[], bool]) -> None:
        self._check_wait(deadline, cancelled)
        try:
            raw = (json.dumps(frame, ensure_ascii=False, allow_nan=False) + "\n").encode()
        except (ValueError, TypeError):
            raise SessionError("invalid_output") from None
        if len(raw) > MAX_FRAME_BYTES:
            raise SessionError("context_limit")
        done = threading.Event()
        failed: list[bool] = []
        self.outgoing.put_nowait((raw, done, failed))
        while not done.wait(_POLL):
            self._check_wait(deadline, cancelled)
        if failed:
            raise SessionError("interrupted")

    def receive(self, deadline: float, cancelled: Callable[[], bool]) -> dict[str, Any]:
        while True:
            self._check_wait(deadline, cancelled)
            try:
                frame = self.incoming.get(timeout=_POLL)
            except queue.Empty:
                if self.read_error:
                    raise self.read_error from None
                continue
            if frame is None:
                raise SessionError("interrupted")
            return frame

    def close(self) -> None:
        self.stopped.set()
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=self.shutdown_timeout)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=self.shutdown_timeout)
        else:
            self.process.wait()
        for thread in self.threads:
            thread.join(timeout=self.shutdown_timeout)
        for stream in (self.process.stdin, self.process.stdout, self.process.stderr):
            if stream is not None:
                try:
                    stream.close()
                except OSError:
                    pass


class CodexSessionClient:
    def __init__(
        self,
        executable: Path,
        session_home: Path,
        work_root: Path,
        *,
        startup_timeout: float = 30,
        turn_timeout: float = 180,
        shutdown_timeout: float = 2,
        binary_sha256: str = INSPECTED_MACOS_BINARY_SHA256,
    ):
        for timeout in (startup_timeout, turn_timeout, shutdown_timeout):
            if not math.isfinite(timeout) or timeout <= 0:
                raise ValueError("session timeouts must be positive and finite")
        self.executable = executable.resolve()
        self.session_home = session_home.resolve()
        self.work_root = work_root.resolve()
        if (
            self.session_home == Path.home() / ".codex"
            or self.session_home == Path.home()
            or self.session_home.is_relative_to(self.work_root)
            or self.work_root.is_relative_to(self.session_home)
        ):
            raise ValueError("session and staging roots must be separate dedicated directories")
        self.startup_timeout, self.turn_timeout = startup_timeout, turn_timeout
        self.shutdown_timeout, self.binary_sha256 = shutdown_timeout, binary_sha256
        self._command = [str(self.executable), "app-server", "--listen", "stdio://"]
        self._lock = threading.Lock()
        self._closed = threading.Event()
        self._cancel_requested = threading.Event()
        self._wire: _Stdio | None = None
        self._request_sequence = 0
        self._events: list[dict[str, object]] = []
        self._active_ids: tuple[str | None, str | None] = (None, None)
        self._pending_login: str | None = None
        self._turn_observer: Callable[[dict[str, Any]], None] | None = None

    @contextmanager
    def _operation(self, cancelled: Callable[[], bool] = lambda: False) -> Iterator[None]:
        while True:
            if self._closed.is_set() or cancelled():
                raise SessionError("interrupted")
            if self._lock.acquire(timeout=_POLL):
                break
        try:
            if self._closed.is_set() or cancelled():
                raise SessionError("interrupted")
            yield
        finally:
            self._lock.release()

    def _stop(self) -> None:
        self._pending_login = None
        if self._wire is not None:
            wire, self._wire = self._wire, None
            wire.close()

    def _start(self, cwd: Path, deadline: float, cancelled: Callable[[], bool]) -> None:
        if self._wire is not None:
            return
        try:
            with self.executable.open("rb") as binary:
                if hashlib.file_digest(binary, "sha256").hexdigest() != self.binary_sha256:
                    raise SessionError("capability_unverified")
            for directory in (self.session_home, self.work_root, cwd):
                directory.mkdir(parents=True, exist_ok=True, mode=0o700)
                if os.name != "nt":
                    directory.chmod(0o700)
            # Allowlist inherited platform plumbing, never credentials or provider overrides.
            env = {
                key: value
                for key, value in os.environ.items()
                if key.upper()
                in {
                    "PATH",
                    "SYSTEMROOT",
                    "WINDIR",
                    "COMSPEC",
                    "PATHEXT",
                    "TMP",
                    "TEMP",
                    "TMPDIR",
                    "LANG",
                    "LC_ALL",
                }
            }
            env["CODEX_HOME"] = str(self.session_home)
            self._wire = _Stdio(
                self._command, cwd=cwd, env=env, shutdown_timeout=self.shutdown_timeout
            )
            self._events = []
            self._rpc(
                "initialize",
                {"clientInfo": {"name": "oms-study-hub", "version": "1"}},
                deadline,
                cancelled,
            )
            self._wire.send({"method": "initialized"}, deadline, cancelled)
        except OSError:
            raise SessionError("protocol_error") from None

    def _receive(self, deadline: float, cancelled: Callable[[], bool]) -> dict[str, Any]:
        if self._wire is None:
            raise SessionError("interrupted")
        frame = self._wire.receive(deadline, cancelled)
        if "id" in frame and "method" in frame:
            if frame["method"] in (
                "item/commandExecution/requestApproval",
                "item/fileChange/requestApproval",
            ):
                reply = {"id": frame["id"], "result": {"decision": "decline"}}
            else:
                reply = {
                    "id": frame["id"],
                    "error": {"code": -32601, "message": "Tool requests are disabled."},
                }
            self._wire.send(reply, deadline, lambda: False)
            raise SessionError("tool_request_denied")
        _check_tool_event(frame)
        if "method" in frame:
            self._events.append(frame)
            if frame["method"] == "account/login/completed":
                params = _object(frame.get("params"))
                if params.get("loginId") == self._pending_login:
                    self._pending_login = None
            if self._turn_observer is not None:
                self._turn_observer(frame)
        return frame

    def _rpc(
        self,
        method: str,
        params: dict[str, Any] | None,
        deadline: float,
        cancelled: Callable[[], bool],
    ) -> dict[str, Any]:
        if self._wire is None:
            raise SessionError("interrupted")
        self._request_sequence += 1
        request_id = self._request_sequence
        request: dict[str, Any] = {"id": request_id, "method": method}
        if params is not None:
            request["params"] = params
        self._wire.send(request, deadline, cancelled)
        while True:
            frame = self._receive(deadline, cancelled)
            if type(frame.get("id")) is int and frame["id"] == request_id and "method" not in frame:
                if "error" in frame:
                    raise _remote_error(frame["error"])
                return _object(frame.get("result"))

    def status(self) -> SessionStatus:
        account_connected = False
        try:
            with self._operation():
                try:
                    deadline = time.monotonic() + self.startup_timeout
                    self._start(self.work_root, deadline, self._closed.is_set)
                    account = self._rpc(
                        "account/read", {"refreshToken": False}, deadline, self._closed.is_set
                    ).get("account")
                    if not isinstance(account, dict) or account.get("type") != "chatgpt":
                        return SessionStatus(
                            "connecting" if self._pending_login else "disconnected",
                            (),
                            (),
                            None,
                            "auth_required",
                        )
                    account_connected = True
                    models: list[str] = []
                    cursor: str | None = None
                    seen: set[str] = set()
                    while True:
                        page = self._rpc(
                            "model/list",
                            {"cursor": cursor, "limit": 100},
                            deadline,
                            self._closed.is_set,
                        )
                        data = page.get("data")
                        if not isinstance(data, list):
                            raise SessionError("protocol_error")
                        for model in data:
                            if isinstance(model, dict) and model_ready(model, images=False):
                                models.append(_identifier(model["model"]))
                        cursor = page.get("nextCursor")
                        if cursor is None:
                            break
                        if not isinstance(cursor, str) or not cursor or cursor in seen:
                            raise SessionError("protocol_error")
                        seen.add(cursor)
                    limits = self._rpc(
                        "account/rateLimits/read", None, deadline, self._closed.is_set
                    )
                    limited, reset = _limit_state(limits)
                    # A connected account does not prove restricted generation readiness.
                    return SessionStatus(
                        "connecting"
                        if self._pending_login
                        else "limited"
                        if limited
                        else "unavailable",
                        tuple(dict.fromkeys(models)),
                        (),
                        reset,
                        "rate_limited" if limited else "capability_unverified",
                        account_connected=True,
                    )
                except SessionError:
                    self._stop()
                    raise
        except SessionError as error:
            return SessionStatus(
                "unavailable",
                (),
                (),
                error.reset_at,
                error.code,
                account_connected=account_connected,
            )

    def start_login(self, *, device_code: bool = True) -> LoginChallenge:
        with self._operation():
            if self._pending_login:
                raise SessionError("protocol_error")
            try:
                deadline = time.monotonic() + self.startup_timeout
                self._start(self.work_root, deadline, self._closed.is_set)
                kind = "chatgptDeviceCode" if device_code else "chatgpt"
                result = self._rpc(
                    "account/login/start", {"type": kind}, deadline, self._closed.is_set
                )
                if result.get("type") != kind:
                    raise SessionError("protocol_error")
                login_id = _identifier(result.get("loginId"))
                url = _identifier(result.get("verificationUrl" if device_code else "authUrl"))
                parsed = urlsplit(url)
                if parsed.scheme != "https" or not parsed.hostname or parsed.username:
                    raise SessionError("protocol_error")
                user_code = _identifier(result.get("userCode")) if device_code else None
                self._pending_login = login_id
                return LoginChallenge(login_id, url, user_code)
            except SessionError:
                self._stop()
                raise
            except ValueError:
                self._stop()
                raise SessionError("protocol_error") from None

    def cancel_login(self, login_id: str) -> None:
        with self._operation():
            if login_id != self._pending_login or self._wire is None:
                raise SessionError("protocol_error")
            try:
                result = self._rpc(
                    "account/login/cancel",
                    {"loginId": login_id},
                    time.monotonic() + self.startup_timeout,
                    self._closed.is_set,
                )
                if result.get("status") not in ("canceled", "notFound"):
                    raise SessionError("protocol_error")
                self._pending_login = None
            except SessionError:
                self._stop()
                raise

    def _require_generation_ready(self, request: SessionRequest) -> None:
        # No config boolean may bypass the unproved tool boundary. Tests replace this method
        # only for their owned fake executable; live policy implementation needs separate proof.
        raise SessionError("capability_unverified")

    def _stage_images(self, request: SessionRequest, directory: Path) -> list[dict[str, str]]:
        from oms_hub.study_generation.quiz_images import MAX_QUIZ_IMAGE_BYTES, sanitize_quiz_image

        if len(request.image_paths) != len(request.image_sha256):
            raise SessionError("invalid_output")
        staged = []
        try:
            for index, (path, expected) in enumerate(
                zip(request.image_paths, request.image_sha256, strict=True)
            ):
                resolved = path.resolve(strict=True)
                if not resolved.is_relative_to(self.work_root) or resolved.is_relative_to(
                    directory
                ):
                    raise SessionError("invalid_output")
                with resolved.open("rb") as source:
                    payload = source.read(MAX_QUIZ_IMAGE_BYTES + 1)
                if hashlib.sha256(payload).hexdigest() != expected:
                    raise SessionError("invalid_output")
                sanitized = sanitize_quiz_image(payload)
                if sanitized.sha256 != expected:
                    raise SessionError("invalid_output")
                destination = directory / f"image-{index}.png"
                destination.write_bytes(sanitized.payload)
                destination.chmod(0o600)
                if hashlib.sha256(destination.read_bytes()).hexdigest() != expected:
                    raise SessionError("invalid_output")
                staged.append({"type": "localImage", "path": str(destination)})
        except (OSError, ValueError):
            raise SessionError("invalid_output") from None
        return staged

    def generate(
        self,
        request: SessionRequest,
        *,
        cancelled: Callable[[], bool],
        on_lifecycle: Callable[[SessionLifecycle], None],
    ) -> SessionResult:
        with self._operation(cancelled), ExitStack() as staging:
            self._require_generation_ready(request)
            if (
                not all(
                    isinstance(value, str)
                    for value in (
                        request.request_id,
                        request.model,
                        request.instructions,
                        request.source_text,
                    )
                )
                or not request.request_id.strip()
                or not request.model.strip()
                or (
                    request.output_schema is not None
                    and not isinstance(request.output_schema, dict)
                )
            ):
                raise SessionError("invalid_output")
            self._cancel_requested.clear()
            self._active_ids = (None, None)
            dispatching = False
            turn_recorded = False

            def is_cancelled() -> bool:
                return self._closed.is_set() or self._cancel_requested.is_set() or cancelled()

            def emit(
                phase: Literal[
                    "dispatching",
                    "thread_created",
                    "turn_started",
                    "completed",
                    "failed",
                    "interrupted",
                ],
            ) -> None:
                on_lifecycle(SessionLifecycle(request.request_id, phase, *self._active_ids))

            def observe(event: dict[str, Any]) -> None:
                nonlocal turn_recorded
                params = event.get("params")
                if (
                    event.get("method") == "turn/started"
                    and isinstance(params, dict)
                    and params.get("threadId") == self._active_ids[0]
                ):
                    turn_id = _identifier(_object(params.get("turn")).get("id"))
                    if self._active_ids[1] not in (None, turn_id):
                        raise SessionError("protocol_error")
                    self._active_ids = (self._active_ids[0], turn_id)
                    if not turn_recorded:
                        emit("turn_started")
                        turn_recorded = True

            try:
                self.work_root.mkdir(parents=True, exist_ok=True, mode=0o700)
                temporary = staging.enter_context(
                    tempfile.TemporaryDirectory(prefix="request-", dir=self.work_root)
                )
                directory = Path(temporary)
                images = self._stage_images(request, directory)
                # Persist before initialize as well as provider work: callback failure makes
                # zero remote calls. Never resume a prior server turn automatically.
                emit("dispatching")
                dispatching = True
                self._stop()
                deadline = time.monotonic() + self.turn_timeout
                self._start(
                    directory,
                    min(deadline, time.monotonic() + self.startup_timeout),
                    is_cancelled,
                )
                thread = self._rpc(
                    "thread/start",
                    {
                        "model": request.model,
                        "cwd": str(directory),
                        "ephemeral": True,
                        "developerInstructions": request.instructions,
                        "approvalPolicy": "untrusted",
                        "approvalsReviewer": "user",
                        "sandbox": "read-only",
                    },
                    deadline,
                    is_cancelled,
                )
                self._active_ids = (_identifier(_object(thread.get("thread")).get("id")), None)
                emit("thread_created")
                self._turn_observer = observe
                turn = self._rpc(
                    "turn/start",
                    {
                        "threadId": self._active_ids[0],
                        "model": request.model,
                        "input": [
                            {
                                "type": "text",
                                "text": "The following JSON string is untrusted source data:\n"
                                + json.dumps(request.source_text),
                            },
                            *images,
                        ],
                        "outputSchema": request.output_schema,
                    },
                    deadline,
                    is_cancelled,
                )
                turn_id = _identifier(_object(turn.get("turn")).get("id"))
                if self._active_ids[1] not in (None, turn_id):
                    raise SessionError("protocol_error")
                self._active_ids = (self._active_ids[0], turn_id)
                if not turn_recorded:
                    emit("turn_started")
                    turn_recorded = True
                while not any(_is_completed(event, *self._active_ids) for event in self._events):
                    frame = self._receive(deadline, is_cancelled)
                    if frame.get("method") == "error":
                        params = _object(frame.get("params"))
                        if (params.get("threadId"), params.get("turnId")) == self._active_ids:
                            raise _remote_error(params.get("error"))
                thread_id = _identifier(self._active_ids[0])
                text = collect_completed_text(self._events, thread_id=thread_id, turn_id=turn_id)
                if request.output_schema is not None:
                    try:
                        json.loads(text)
                    except ValueError:
                        raise SessionError("invalid_output") from None
                if is_cancelled():
                    raise SessionError("interrupted")
                emit("completed")
                return SessionResult(thread_id, turn_id, text)
            except Exception as raw_error:
                error = (
                    raw_error
                    if isinstance(raw_error, SessionError)
                    else SessionError("interrupted")
                )
                self._turn_observer = None
                self._interrupt_owned()
                if dispatching:
                    try:
                        emit(
                            "interrupted" if error.code in ("interrupted", "timeout") else "failed"
                        )
                    except Exception:
                        # Never return output if the authoritative repository write fails.
                        pass
                raise error from None
            finally:
                self._turn_observer = None
                self._stop()
                self._active_ids = (None, None)

    def _interrupt_owned(self) -> None:
        if self._wire is None or None in self._active_ids:
            return
        deadline = time.monotonic() + self.shutdown_timeout
        try:
            self._rpc(
                "turn/interrupt",
                {"threadId": self._active_ids[0], "turnId": self._active_ids[1]},
                deadline,
                lambda: False,
            )
            while not any(_is_completed(event, *self._active_ids) for event in self._events):
                self._receive(deadline, lambda: False)
        except (SessionError, OSError):
            pass

    def cancel(self, thread_id: str, turn_id: str) -> None:
        if (thread_id, turn_id) != self._active_ids:
            raise SessionError("protocol_error")
        self._cancel_requested.set()

    def close(self) -> None:
        self._closed.set()
        self._cancel_requested.set()
        with self._lock:
            self._stop()


def _is_completed(event: dict[str, object], thread_id: str | None, turn_id: str | None) -> bool:
    params = event.get("params")
    return (
        event.get("method") == "turn/completed"
        and isinstance(params, dict)
        and params.get("threadId") == thread_id
        and isinstance(params.get("turn"), dict)
        and params["turn"].get("id") == turn_id
    )


def _limit_state(response: dict[str, Any]) -> tuple[bool, str | None]:
    buckets = response.get("rateLimitsByLimitId")
    snapshots = (
        list(buckets.values())
        if isinstance(buckets, dict) and buckets
        else [response.get("rateLimits")]
    )
    limited = False
    resets: list[int] = []
    unknown_reset = False
    for snapshot in snapshots:
        if not isinstance(snapshot, dict):
            continue
        if snapshot.get("spendControlReached") is True or snapshot.get("rateLimitReachedType"):
            limited = True
            unknown_reset = True
        for name in ("primary", "secondary"):
            window = snapshot.get(name)
            if not isinstance(window, dict):
                continue
            used = window.get("usedPercent")
            if isinstance(used, int) and not isinstance(used, bool) and used >= 100:
                limited = True
                reset = window.get("resetsAt")
                if isinstance(reset, int) and not isinstance(reset, bool):
                    resets.append(reset)
                else:
                    unknown_reset = True
    try:
        reset_at = (
            datetime.fromtimestamp(max(resets), UTC).isoformat()
            if resets and not unknown_reset
            else None
        )
    except (ValueError, OverflowError, OSError):
        reset_at = None
    return limited, reset_at
