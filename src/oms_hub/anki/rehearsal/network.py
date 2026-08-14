from __future__ import annotations

import ipaddress
import json
import os
import re
import socket
import tempfile
import threading
import time
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, cast


class EgressDenied(RuntimeError):
    """A rehearsal process attempted an unauthorized network connection."""


class _AuthorizedSockaddr(tuple[object, ...]):
    """A getaddrinfo result carrying one guard-issued connect token."""


@dataclass(frozen=True, slots=True)
class EgressDecision:
    host: str
    port: int
    resolved_address: str | None
    allowed: bool


class EgressEvidenceLedger:
    """Nonce-bound, process-local authorization evidence for a rehearsal overlay."""

    _schema_version = 1

    def __init__(self, directory: Path, *, mode: str, run_nonce: str) -> None:
        directory.mkdir(parents=True, exist_ok=True)
        self._path = directory / "egress-decisions.json"
        self._mode = mode
        self._run_nonce = run_nonce
        self._records = self._load_records()

    def startup(self) -> None:
        self._append_marker("startup")

    def shutdown(self) -> None:
        self._append_marker("shutdown")

    def decision(self, host: str, port: int, resolved_address: str | None, allowed: bool) -> None:
        self._records.append(
            {
                "kind": "authorization",
                "mode": self._mode,
                "host": host,
                "port": port,
                "resolved_address": resolved_address,
                "allowed": allowed,
                "ordinal": len(self._records) + 1,
                "timestamp": _timestamp(),
            }
        )
        self._persist()

    def _append_marker(self, marker: Literal["startup", "shutdown"]) -> None:
        self._records.append(
            {
                "kind": marker,
                "mode": self._mode,
                "host": None,
                "port": None,
                "resolved_address": None,
                "allowed": None,
                "ordinal": len(self._records) + 1,
                "timestamp": _timestamp(),
            }
        )
        self._persist()

    def _load_records(self) -> list[dict[str, object]]:
        if not self._path.exists():
            return []
        try:
            payload = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeError("egress evidence is malformed") from exc
        if (
            not isinstance(payload, dict)
            or payload.get("schema_version") != self._schema_version
            or payload.get("run_nonce") != self._run_nonce
            or payload.get("mode") != self._mode
            or not isinstance(payload.get("records"), list)
        ):
            raise RuntimeError("egress evidence is stale or malformed")
        records = payload["records"]
        if not all(
            _valid_evidence_record(record, ordinal, self._mode)
            for ordinal, record in enumerate(records, 1)
        ):
            raise RuntimeError("egress evidence has an invalid sequence")
        return list(records)

    def _persist(self) -> None:
        _atomic_json(
            self._path,
            {
                "schema_version": self._schema_version,
                "run_nonce": self._run_nonce,
                "mode": self._mode,
                "records": self._records,
            },
        )


class EgressPolicy:
    def __init__(
        self,
        mode: Literal["deterministic", "shadow"],
        pinned_hosts: dict[str, frozenset[str]],
        evidence: EgressEvidenceLedger | None = None,
    ) -> None:
        self.mode = mode
        self.pinned_hosts = pinned_hosts
        self.decisions: list[EgressDecision] = []
        self._evidence = evidence
        # Resolution grants are scoped to one execution context.  They are
        # deliberately not a process-wide IP allowlist: a later raw numeric
        # connection must not inherit a hostname lookup's authority.
        self._resolution_tokens: ContextVar[dict[tuple[int, str, int], list[float]] | None] = (
            ContextVar("rehearsal_resolution_tokens", default=None)
        )
        self._token_ttl_seconds = 5.0

    @classmethod
    def deterministic(cls, evidence: EgressEvidenceLedger | None = None) -> EgressPolicy:
        return cls("deterministic", {}, evidence)

    @classmethod
    def shadow(
        cls,
        pinned_hosts: dict[str, set[str]],
        evidence: EgressEvidenceLedger | None = None,
    ) -> EgressPolicy:
        normalized = {
            _pinned_dns_hostname(host): frozenset(addresses)
            for host, addresses in pinned_hosts.items()
        }
        if not normalized or any(not addresses for addresses in normalized.values()):
            raise ValueError("shadow egress requires nonempty pinned host addresses")
        return cls("shadow", normalized, evidence)

    def record_startup(self) -> None:
        if self._evidence is not None:
            self._evidence.startup()

    def record_shutdown(self) -> None:
        if self._evidence is not None:
            self._evidence.shutdown()

    def authorize(
        self,
        host: str,
        port: int,
        *,
        resolved_address: str | None = None,
    ) -> None:
        normalized = host.casefold().rstrip(".")
        allowed = _loopback(normalized) and _loopback(resolved_address or normalized)
        if not allowed and self.mode == "shadow":
            pinned = self.pinned_hosts.get(normalized)
            allowed = port == 443 and pinned is not None and resolved_address in pinned
        self.decisions.append(EgressDecision(normalized, port, resolved_address, allowed))
        if self._evidence is not None:
            # Persist before EgressDenied so an intentional denied probe is evidence.
            self._evidence.decision(normalized, port, resolved_address, allowed)
        if not allowed:
            raise EgressDenied(f"rehearsal egress denied for {normalized}:{port}")

    def grant_resolution_tokens(self, rows: list[tuple[object, ...]]) -> list[tuple[object, ...]]:
        """Grant one short-lived right to each exact returned socket address."""
        tokens = {
            key: list(expiries) for key, expiries in (self._resolution_tokens.get() or {}).items()
        }
        expiry = time.monotonic() + self._token_ttl_seconds
        authorized: list[tuple[object, ...]] = []
        for row in rows:
            address = _AuthorizedSockaddr(cast(tuple[object, ...], row[-1]))
            host, port = _socket_destination(address)
            tokens.setdefault((id(address), host, port), []).append(expiry)
            authorized.append((*row[:-1], address))
        self._resolution_tokens.set(tokens)
        return authorized

    def authorize_connect(self, host: str, port: int, address: object) -> None:
        normalized = host.casefold().rstrip(".")
        if _loopback(normalized):
            self.authorize(normalized, port, resolved_address=normalized)
            return
        key = (id(address), normalized, port)
        now = time.monotonic()
        tokens = {
            item: [expiry for expiry in expiries if expiry >= now]
            for item, expiries in (self._resolution_tokens.get() or {}).items()
        }
        expiries = tokens.get(key, [])
        allowed = bool(expiries)
        if allowed:
            expiries.pop(0)
            if expiries:
                tokens[key] = expiries
            else:
                tokens.pop(key, None)
            self._resolution_tokens.set(tokens)
        self.decisions.append(EgressDecision(normalized, port, normalized, allowed))
        if self._evidence is not None:
            self._evidence.decision(normalized, port, normalized, allowed)
        if not allowed:
            raise EgressDenied(f"rehearsal egress denied for {normalized}:{port}")

    def resolve(self, host: str, port: int) -> list[tuple[object, ...]]:
        normalized = host.casefold().rstrip(".")
        if _loopback(normalized):
            self.authorize(normalized, port, resolved_address=normalized)
            return list(socket.getaddrinfo(host, port, type=socket.SOCK_STREAM))
        pinned = self.pinned_hosts.get(normalized)
        if self.mode != "shadow" or pinned is None or port != 443:
            self.authorize(normalized, port)
        assert pinned is not None and port == 443
        rows: list[tuple[object, ...]] = []
        for address in sorted(pinned):
            self.authorize(normalized, port, resolved_address=address)
            family = (
                socket.AF_INET6 if ipaddress.ip_address(address).version == 6 else socket.AF_INET
            )
            sockaddr: tuple[object, ...] = (
                (address, port, 0, 0) if family == socket.AF_INET6 else (address, port)
            )
            rows.append((family, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", sockaddr))
        return rows


class SocketEgressGuard:
    """Process-wide socket guard installed only for an isolated rehearsal process."""

    _lock = threading.Lock()
    _active: SocketEgressGuard | None = None

    def __init__(self, policy: EgressPolicy) -> None:
        self.policy = policy
        self._original_getaddrinfo = socket.getaddrinfo
        self._original_connect = socket.socket.connect
        self._original_connect_ex = socket.socket.connect_ex
        self._original_send = socket.socket.send
        self._original_sendto = socket.socket.sendto
        self._original_sendmsg = getattr(socket.socket, "sendmsg", None)

    def install(self) -> None:
        with self._lock:
            if self.__class__._active is not None:
                raise RuntimeError("a socket egress guard is already installed")

            # Evidence initialization can fail (notably, Windows does not support
            # the POSIX directory-fsync pattern).  Do it before touching global
            # socket methods so a failed startup cannot leak this guard into the
            # next test or process lifecycle.
            self.policy.record_startup()

            def guarded_getaddrinfo(
                host: str | bytes | None,
                port: str | int | None,
                *args: Any,
                **kwargs: Any,
            ) -> list[tuple[object, ...]]:
                normalized = host.decode() if isinstance(host, bytes) else str(host or "")
                numeric_port = int(port or 0)
                requested_type = kwargs.get("type", args[1] if len(args) > 1 else 0)
                if requested_type not in (0, socket.SOCK_STREAM):
                    raise EgressDenied("rehearsal egress permits TCP stream resolution only")
                if _loopback(normalized):
                    self.policy.authorize(normalized, numeric_port, resolved_address=normalized)
                    return cast(
                        list[tuple[object, ...]],
                        self._original_getaddrinfo(host, port, *args, **kwargs),
                    )
                return self.policy.grant_resolution_tokens(
                    self.policy.resolve(normalized, numeric_port)
                )

            def guarded_connect(sock: socket.socket, address: object) -> None:
                _require_tcp_socket(sock)
                host, port = _socket_destination(address)
                self.policy.authorize_connect(host, port, address)
                self._original_connect(sock, address)  # type: ignore[arg-type]

            def guarded_connect_ex(sock: socket.socket, address: object) -> int:
                _require_tcp_socket(sock)
                host, port = _socket_destination(address)
                self.policy.authorize_connect(host, port, address)
                return self._original_connect_ex(sock, address)  # type: ignore[arg-type]

            def guarded_send(sock: socket.socket, data: bytes, flags: int = 0) -> int:
                _require_tcp_socket(sock)
                return self._original_send(sock, data, flags)

            def guarded_sendto(sock: socket.socket, data: bytes, *args: object) -> int:
                _require_tcp_socket(sock)
                address = args[-1] if args else None
                host, port = _socket_destination(address)
                self.policy.authorize_connect(host, port, address)
                if len(args) == 1:
                    return self._original_sendto(sock, data, cast(tuple[Any, ...], address))
                if len(args) == 2 and isinstance(args[0], int):
                    return self._original_sendto(
                        sock, data, args[0], cast(tuple[Any, ...], address)
                    )
                raise EgressDenied("rehearsal egress denied for an invalid sendto call")

            def guarded_sendmsg(sock: socket.socket, *args: object, **kwargs: object) -> int:
                _require_tcp_socket(sock)
                assert self._original_sendmsg is not None
                return cast(int, self._original_sendmsg(sock, *args, **kwargs))

            try:
                socket.getaddrinfo = guarded_getaddrinfo  # type: ignore[assignment]
                socket.socket.connect = guarded_connect  # type: ignore[assignment,method-assign]
                socket.socket.connect_ex = guarded_connect_ex  # type: ignore[assignment,method-assign]
                socket.socket.send = guarded_send  # type: ignore[assignment,method-assign]
                socket.socket.sendto = guarded_sendto  # type: ignore[assignment,method-assign]
                if self._original_sendmsg is not None:
                    setattr(socket.socket, "sendmsg", guarded_sendmsg)  # noqa: B010
                self.__class__._active = self
            except BaseException:
                self.__class__._active = None
                try:
                    self._restore_socket_methods()
                except BaseException:
                    # A broken socket implementation can reject restoration
                    # too.  The patch-assignment error remains authoritative.
                    pass
                try:
                    self.policy.record_shutdown()
                except BaseException:
                    # Preserve the installation failure; cleanup evidence must
                    # never conceal the exception that triggered rollback.
                    pass
                raise

    def uninstall(self) -> None:
        with self._lock:
            if self.__class__._active is not self:
                return
            try:
                self._restore_socket_methods()
            finally:
                self.__class__._active = None
            self.policy.record_shutdown()

    def _restore_socket_methods(self) -> None:
        errors: list[BaseException] = []
        restorations: list[tuple[object, str, object]] = [
            (socket, "getaddrinfo", self._original_getaddrinfo),
            (socket.socket, "connect", self._original_connect),
            (socket.socket, "connect_ex", self._original_connect_ex),
            (socket.socket, "send", self._original_send),
            (socket.socket, "sendto", self._original_sendto),
        ]
        if self._original_sendmsg is not None:
            restorations.append((socket.socket, "sendmsg", self._original_sendmsg))
        for owner, name, original in restorations:
            try:
                setattr(owner, name, original)
            except BaseException as exc:
                errors.append(exc)
        if errors:
            raise errors[0]


def _socket_destination(address: object) -> tuple[str, int]:
    if not isinstance(address, tuple) or len(address) < 2:
        raise EgressDenied("rehearsal egress denied for a non-IP socket destination")
    host, port = address[:2]
    if not isinstance(host, str) or not isinstance(port, int):
        raise EgressDenied("rehearsal egress denied for an invalid socket destination")
    return host, port


def _loopback(value: str | None) -> bool:
    if value is None:
        return False
    if value.casefold().rstrip(".") == "localhost":
        return True
    try:
        return ipaddress.ip_address(value).is_loopback
    except ValueError:
        return False


def _is_ip_address(value: str) -> bool:
    try:
        ipaddress.ip_address(value)
        return True
    except ValueError:
        return False


_DNS_LABEL = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")


def _pinned_dns_hostname(host: str) -> str:
    """Normalize a DNS pin key while refusing every numeric address spelling."""
    normalized = host.casefold().rstrip(".")
    if not normalized or normalized.startswith("[") or normalized.endswith("]"):
        raise ValueError("shadow egress pin keys must be DNS hostnames, not IP literals")
    if _is_numeric_host_syntax(normalized):
        raise ValueError("shadow egress pin keys must be DNS hostnames, not IP literals")
    labels = normalized.split(".")
    if normalized.isdecimal() or all(label.isdecimal() for label in labels) or (
        len(labels) == 1 and normalized.startswith("0x")
    ):
        raise ValueError("shadow egress pin keys must be DNS hostnames, not IP literals")
    if len(normalized) > 253 or any(not _DNS_LABEL.fullmatch(label) for label in labels):
        raise ValueError("shadow egress pin keys must be DNS hostnames")
    return normalized


def _is_numeric_host_syntax(value: str) -> bool:
    """Reject every libc-recognized numeric address spelling without DNS."""
    try:
        ipaddress.ip_address(value)
        return True
    except ValueError:
        pass
    # inet_aton intentionally recognizes legacy dotted hex/octal/integer forms
    # (for example 0x7f.1 and 2130706433) which are not IPAddress literals.
    try:
        socket.inet_aton(value)
        return True
    except OSError:
        return False


def _require_tcp_socket(sock: socket.socket) -> None:
    # ``proto=0`` is the platform default for SOCK_STREAM and resolves to TCP.
    if sock.type & socket.SOCK_STREAM != socket.SOCK_STREAM or sock.proto not in (
        0,
        socket.IPPROTO_TCP,
    ):
        raise EgressDenied("rehearsal egress permits TCP sockets only")


def _valid_evidence_record(value: object, ordinal: int, mode: str) -> bool:
    if not isinstance(value, dict):
        return False
    expected = {
        "kind",
        "mode",
        "host",
        "port",
        "resolved_address",
        "allowed",
        "ordinal",
        "timestamp",
    }
    if set(value) != expected or value.get("mode") != mode or value.get("ordinal") != ordinal:
        return False
    if not isinstance(value.get("timestamp"), str):
        return False
    kind = value.get("kind")
    if kind in {"startup", "shutdown"}:
        return (
            value.get("host") is None
            and value.get("port") is None
            and value.get("resolved_address") is None
            and value.get("allowed") is None
        )
    return (
        kind == "authorization"
        and isinstance(value.get("host"), str)
        and isinstance(value.get("port"), int)
        and (
            value.get("resolved_address") is None or isinstance(value.get("resolved_address"), str)
        )
        and isinstance(value.get("allowed"), bool)
    )


def _atomic_json(path: Path, payload: dict[str, object]) -> None:
    parent = path.parent.resolve()
    target = path.resolve(strict=False)
    try:
        target.relative_to(parent)
    except ValueError as exc:
        raise ValueError("egress evidence path escapes its overlay directory") from exc
    encoded = (json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n").encode()
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=parent)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
        _fsync_directory(parent)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _timestamp() -> str:
    return datetime.now(UTC).isoformat()


def _fsync_directory(path: Path) -> None:
    """Durably sync a POSIX directory; Windows has no compatible directory handle."""
    if os.name == "nt":
        return
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
