"""Bounded one-shot native restart acceptance for the Windows scheduled runtime."""

from __future__ import annotations

import hashlib
import json
import os
import stat
import threading
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Protocol, cast
from uuid import UUID, uuid4

CONTROLLED_RESTART_EXIT_CODE = 75
_REQUEST_SCHEMA_VERSION = 1
_REQUEST_FIELDS = {
    "schema_version",
    "nonce",
    "expected_revision",
    "expected_tree",
    "expected_schema",
    "exit_code",
    "expires_at",
}
_FIRE_FIELDS = {"schema_version", "nonce", "armed_sha256"}
_EXPECTED_WORKERS = {"generation_worker", "ingestion_worker", "studio_worker"}
_FULL_SHA_LENGTH = 40
_MAX_REQUEST_LIFETIME = timedelta(minutes=5)
_MAX_JSON_BYTES = 64 * 1024


class ControlledRestartError(RuntimeError):
    """A controlled restart request failed a fail-closed boundary."""


class QuiescableSupervisor(Protocol):
    def quiesce(self, timeout_seconds: float) -> bool: ...

    def resume(self) -> None: ...


class ServerControl(Protocol):
    should_exit: bool


@dataclass(frozen=True, slots=True)
class ControlledRestartRequest:
    schema_version: int
    nonce: str
    expected_revision: str
    expected_tree: str
    expected_schema: int
    exit_code: int
    expires_at: datetime

    @classmethod
    def from_mapping(
        cls,
        value: Mapping[str, object],
        *,
        now: datetime,
    ) -> ControlledRestartRequest:
        unknown = set(value) - _REQUEST_FIELDS
        missing = _REQUEST_FIELDS - set(value)
        if unknown:
            raise ControlledRestartError("request contains unknown fields")
        if missing:
            raise ControlledRestartError("request is missing required fields")
        if (
            value["schema_version"] != _REQUEST_SCHEMA_VERSION
            or isinstance(value["schema_version"], bool)
        ):
            raise ControlledRestartError("request schema is unsupported")
        nonce = _canonical_uuid(value["nonce"])
        revision = _full_sha(value["expected_revision"], "revision")
        tree = _full_sha(value["expected_tree"], "tree")
        expected_schema = value["expected_schema"]
        if (
            not isinstance(expected_schema, int)
            or isinstance(expected_schema, bool)
            or expected_schema <= 0
        ):
            raise ControlledRestartError("expected schema must be a positive integer")
        exit_code = value["exit_code"]
        if exit_code != CONTROLLED_RESTART_EXIT_CODE or isinstance(exit_code, bool):
            raise ControlledRestartError("request exit code is not the fixed F28 code")
        expires_at = _parse_utc_datetime(value["expires_at"])
        normalized_now = _require_utc(now)
        if expires_at <= normalized_now:
            raise ControlledRestartError("request has expired")
        if expires_at - normalized_now > _MAX_REQUEST_LIFETIME:
            raise ControlledRestartError("request lifetime exceeds five minutes")
        return cls(
            schema_version=_REQUEST_SCHEMA_VERSION,
            nonce=nonce,
            expected_revision=revision,
            expected_tree=tree,
            expected_schema=expected_schema,
            exit_code=CONTROLLED_RESTART_EXIT_CODE,
            expires_at=expires_at,
        )


class ControlledRestartController:
    """Claim, arm, and fire one exact restart request without a kill boundary."""

    def __init__(
        self,
        *,
        gate_directory: Path,
        data_directory: Path,
        expected_revision: str,
        expected_tree: str,
        expected_schema: int,
        supervisor: QuiescableSupervisor,
        server: ServerControl,
        readiness_probe: Callable[[], Mapping[str, object]],
        now: Callable[[], datetime] | None = None,
        poll_interval_seconds: float = 0.25,
        quiesce_timeout_seconds: float = 10.0,
    ) -> None:
        self.gate_directory = validate_gate_directory(data_directory, gate_directory)
        self.expected_revision = _full_sha(expected_revision, "revision")
        self.expected_tree = _full_sha(expected_tree, "tree")
        if (
            not isinstance(expected_schema, int)
            or isinstance(expected_schema, bool)
            or expected_schema <= 0
        ):
            raise ControlledRestartError("expected schema must be positive")
        if poll_interval_seconds <= 0 or quiesce_timeout_seconds < 0:
            raise ControlledRestartError("controller timing must be non-negative")
        self.expected_schema = expected_schema
        self._supervisor = supervisor
        self._server = server
        self._readiness_probe = readiness_probe
        self._now = now or (lambda: datetime.now(UTC))
        self._poll_interval_seconds = poll_interval_seconds
        self._quiesce_timeout_seconds = quiesce_timeout_seconds
        self._request: ControlledRestartRequest | None = None
        self._active_path: Path | None = None
        self._armed_path: Path | None = None
        self._healthy_observations = 0
        self._quiesced = False
        self._fired = False
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    @property
    def active_nonce(self) -> str | None:
        return self._request.nonce if self._request is not None else None

    @property
    def fired(self) -> bool:
        return self._fired

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._run,
            name="oms-f28-controlled-restart",
            daemon=True,
        )
        self._thread.start()

    def stop(self, timeout_seconds: float = 2.0) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout_seconds)
        if self._quiesced and not self._fired:
            self._supervisor.resume()
            self._quiesced = False

    def poll_once(self) -> None:
        if self._fired:
            return
        if self._request is None:
            self._claim_request()
            if self._request is None:
                return
        assert self._request is not None
        if self._request.expires_at <= _require_utc(self._now()):
            self._expire_active()
            return
        if not self._quiesced:
            self._quiesced = True
            if not self._supervisor.quiesce(self._quiesce_timeout_seconds):
                self._reject_active("worker_quiesce_timeout")
                return
        try:
            health = self._readiness_probe()
            healthy = self._health_is_exact_and_idle(health)
        except ControlledRestartError:
            self._reject_active("runtime_identity_drift")
            return
        except Exception:  # noqa: BLE001 - request remains fail closed
            self._healthy_observations = 0
            return
        self._healthy_observations = self._healthy_observations + 1 if healthy else 0
        if self._healthy_observations < 2:
            return
        if self._armed_path is None:
            self._armed_path = self.gate_directory / f"armed-{self._request.nonce}.json"
            _atomic_json(
                self._armed_path,
                {
                    "schema_version": _REQUEST_SCHEMA_VERSION,
                    "nonce": self._request.nonce,
                    "expected_revision": self.expected_revision,
                    "expected_tree": self.expected_tree,
                    "expected_schema": self.expected_schema,
                    "exit_code": CONTROLLED_RESTART_EXIT_CODE,
                    "armed_at": _timestamp(self._now()),
                    "server_pid": os.getpid(),
                    "health": health,
                },
            )
        fire_path = self.gate_directory / f"fire-{self._request.nonce}.json"
        if not fire_path.is_file():
            return
        try:
            self._validate_fire(_read_json(fire_path))
            _atomic_json(
                self.gate_directory / f"server-boundary-{self._request.nonce}.json",
                {
                    "schema_version": _REQUEST_SCHEMA_VERSION,
                    "nonce": self._request.nonce,
                    "expected_revision": self.expected_revision,
                    "expected_tree": self.expected_tree,
                    "expected_schema": self.expected_schema,
                    "exit_code": CONTROLLED_RESTART_EXIT_CODE,
                    "fired_at": _timestamp(self._now()),
                    "server_pid": os.getpid(),
                },
            )
        except (ControlledRestartError, OSError, ValueError):
            self._reject_active("invalid_fire_record")
            return
        self._fired = True
        self._server.should_exit = True

    def finalize_server_exit(self) -> int:
        if not self._fired or self._request is None or self._active_path is None:
            return 0
        record = {
            "schema_version": _REQUEST_SCHEMA_VERSION,
            "nonce": self._request.nonce,
            "expected_revision": self.expected_revision,
            "expected_tree": self.expected_tree,
            "expected_schema": self.expected_schema,
            "exit_code": CONTROLLED_RESTART_EXIT_CODE,
            "server_pid": os.getpid(),
            "server_shutdown_completed_at": _timestamp(self._now()),
        }
        _atomic_json(
            self.gate_directory / f"server-exit-{self._request.nonce}.json",
            record,
        )
        _atomic_json(self.gate_directory / "latest-server-exit.json", record)
        self._active_path.replace(
            self.gate_directory / f"consumed-{self._request.nonce}.json"
        )
        return CONTROLLED_RESTART_EXIT_CODE

    def _run(self) -> None:
        while not self._stop.is_set() and not self._fired:
            try:
                self.poll_once()
            except Exception:  # noqa: BLE001 - never let the gate stop the server
                self._reject_active("controller_error")
            self._stop.wait(self._poll_interval_seconds)

    def _claim_request(self) -> None:
        request_path = self.gate_directory / "request.json"
        if not request_path.is_file():
            return
        active_path = self.gate_directory / "active.json"
        if active_path.exists():
            self._reject_unclaimed(request_path, "active_request_exists")
            return
        try:
            request_path.replace(active_path)
            raw = _read_json(active_path)
            request = ControlledRestartRequest.from_mapping(raw, now=self._now())
            if request.expected_revision != self.expected_revision:
                raise ControlledRestartError("request revision differs from runtime")
            if request.expected_tree != self.expected_tree:
                raise ControlledRestartError("request tree differs from runtime")
            if request.expected_schema != self.expected_schema:
                raise ControlledRestartError("request schema differs from runtime")
        except (ControlledRestartError, OSError, ValueError):
            self._active_path = active_path if active_path.exists() else None
            self._reject_active("invalid_request")
            return
        self._request = request
        self._active_path = active_path

    def _health_is_exact_and_idle(self, health: Mapping[str, object]) -> bool:
        if health.get("build_revision") != self.expected_revision:
            raise ControlledRestartError("health revision drift")
        if health.get("build_tree") != self.expected_tree:
            raise ControlledRestartError("health tree drift")
        if health.get("schema_version") != self.expected_schema:
            raise ControlledRestartError("health schema drift")
        if health.get("status") != "ok" or health.get("database_reachable") is not True:
            return False
        workers = health.get("workers")
        if not isinstance(workers, Mapping) or set(workers) != _EXPECTED_WORKERS:
            raise ControlledRestartError("health worker configuration drift")
        for worker in workers.values():
            if not isinstance(worker, Mapping):
                return False
            if worker.get("alive") is not True or worker.get("start_count") != 1:
                return False
            if worker.get("active_work_age_seconds") is not None:
                return False
        return True

    def _validate_fire(self, raw: Mapping[str, object]) -> None:
        if set(raw) != _FIRE_FIELDS:
            raise ControlledRestartError("fire record has invalid fields")
        if (
            raw.get("schema_version") != _REQUEST_SCHEMA_VERSION
            or isinstance(raw.get("schema_version"), bool)
        ):
            raise ControlledRestartError("fire schema is unsupported")
        if self._request is None or raw.get("nonce") != self._request.nonce:
            raise ControlledRestartError("fire nonce does not match")
        if self._armed_path is None:
            raise ControlledRestartError("fire arrived before arm")
        expected_hash = hashlib.sha256(self._armed_path.read_bytes()).hexdigest()
        if raw.get("armed_sha256") != expected_hash:
            raise ControlledRestartError("fire arm hash does not match")

    def _expire_active(self) -> None:
        self._finish_active("expired")

    def _reject_active(self, reason: str) -> None:
        self._finish_active("rejected", reason=reason)

    def _finish_active(self, disposition: str, *, reason: str | None = None) -> None:
        nonce = (
            self._request.nonce
            if self._request is not None
            else _safe_timestamp(self._now())
        )
        if self._active_path is not None and self._active_path.exists():
            destination = self.gate_directory / f"{disposition}-{nonce}.json"
            if destination.exists():
                destination = self.gate_directory / (
                    f"{disposition}-{nonce}-{_safe_timestamp(self._now())}.json"
                )
            self._active_path.replace(destination)
        if reason is not None:
            _atomic_json(
                self.gate_directory / f"{disposition}-reason-{nonce}.json",
                {
                    "schema_version": _REQUEST_SCHEMA_VERSION,
                    "nonce": nonce,
                    "reason": reason,
                    "recorded_at": _timestamp(self._now()),
                },
            )
        if self._quiesced:
            self._supervisor.resume()
        self._request = None
        self._active_path = None
        self._armed_path = None
        self._healthy_observations = 0
        self._quiesced = False

    def _reject_unclaimed(self, path: Path, reason: str) -> None:
        destination = self.gate_directory / (
            f"rejected-{_safe_timestamp(self._now())}-{uuid4().hex}.json"
        )
        path.replace(destination)
        _atomic_json(
            self.gate_directory / f"{destination.stem}-reason.json",
            {
                "schema_version": _REQUEST_SCHEMA_VERSION,
                "reason": reason,
                "recorded_at": _timestamp(self._now()),
            },
        )


def validate_gate_directory(data_directory: Path, gate_directory: Path) -> Path:
    """Require the exact non-linked gate path beneath the effective data root."""
    lexical_data = data_directory.absolute()
    lexical_gate = gate_directory.absolute()
    expected = lexical_data / "acceptance" / "f28"
    for candidate in (lexical_data, lexical_data / "acceptance", lexical_gate):
        if candidate.exists() and _is_link_or_reparse(candidate):
            raise ControlledRestartError("gate path contains a link or reparse point")
    if os.path.normcase(str(lexical_gate)) != os.path.normcase(str(expected)):
        raise ControlledRestartError("gate directory must be exactly data/acceptance/f28")
    try:
        resolved_data = lexical_data.resolve(strict=True)
        resolved_gate = lexical_gate.resolve(strict=True)
    except OSError as error:
        raise ControlledRestartError("gate directory must already exist") from error
    if resolved_gate.parent.parent != resolved_data:
        raise ControlledRestartError("gate directory escapes the effective data root")
    return resolved_gate


def fetch_readiness(port: int) -> Mapping[str, object]:
    """Read the existing loopback-only readiness contract without a new endpoint."""
    with urllib.request.urlopen(  # noqa: S310 - fixed loopback destination
        f"http://127.0.0.1:{port}/health/ready",
        timeout=3,
    ) as response:
        payload = response.read(_MAX_JSON_BYTES + 1)
    if len(payload) > _MAX_JSON_BYTES:
        raise ControlledRestartError("readiness payload is too large")
    value = json.loads(payload)
    if not isinstance(value, Mapping):
        raise ControlledRestartError("readiness payload is not an object")
    return cast(Mapping[str, object], value)


def _read_json(path: Path) -> Mapping[str, object]:
    if _is_link_or_reparse(path):
        raise ControlledRestartError("gate JSON file is a link or reparse point")
    if not path.is_file() or path.stat().st_size > _MAX_JSON_BYTES:
        raise ControlledRestartError("gate JSON file is missing or too large")
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, Mapping):
        raise ControlledRestartError("gate JSON payload is not an object")
    return cast(Mapping[str, object], value)


def _atomic_json(path: Path, value: Mapping[str, object]) -> None:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n"
    temporary = path.with_name(
        f".{path.name}.{os.getpid()}.{threading.get_ident()}.{uuid4().hex}.tmp"
    )
    try:
        with temporary.open("x", encoding="utf-8", newline="\n") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _canonical_uuid(value: object) -> str:
    if not isinstance(value, str):
        raise ControlledRestartError("request nonce must be a UUID")
    try:
        parsed = UUID(value)
    except ValueError as error:
        raise ControlledRestartError("request nonce must be a UUID") from error
    if str(parsed) != value:
        raise ControlledRestartError("request nonce must be canonical")
    return value


def _full_sha(value: object, label: str) -> str:
    if not isinstance(value, str) or len(value) != _FULL_SHA_LENGTH:
        raise ControlledRestartError(f"expected {label} must be a full SHA")
    lowered = value.casefold()
    if any(character not in "0123456789abcdef" for character in lowered):
        raise ControlledRestartError(f"expected {label} must be a full SHA")
    return lowered


def _parse_utc_datetime(value: object) -> datetime:
    if not isinstance(value, str):
        raise ControlledRestartError("request expiry must be an ISO timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise ControlledRestartError("request expiry must be an ISO timestamp") from error
    return _require_utc(parsed)


def _require_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ControlledRestartError("request time must be timezone-aware")
    return value.astimezone(UTC)


def _timestamp(value: datetime) -> str:
    return _require_utc(value).isoformat().replace("+00:00", "Z")


def _safe_timestamp(value: datetime) -> str:
    return _require_utc(value).strftime("%Y%m%dT%H%M%S%fZ")


def _is_link_or_reparse(path: Path) -> bool:
    if path.is_symlink():
        return True
    attributes = getattr(path.lstat(), "st_file_attributes", 0)
    reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return bool(attributes & reparse)
