import hashlib
import hmac
import os
import secrets
from pathlib import Path
from urllib.parse import urlsplit


def origin_is_allowed(origin: str | None, allowed_origins: set[str]) -> bool:
    candidate = _normalized_origin(origin)
    return candidate is not None and any(
        candidate == _normalized_origin(allowed) for allowed in allowed_origins
    )


def browser_csrf_required(method: str, path: str) -> bool:
    return (
        method.upper() in {"POST", "PUT", "PATCH", "DELETE"}
        and not path.startswith("/agent/v1/")
    )


def _normalized_origin(value: str | None) -> tuple[str, str, int] | None:
    if not value or value.casefold() == "null":
        return None
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        return None
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        return None
    if port is None:
        port = 443 if parsed.scheme == "https" else 80
    return parsed.scheme, parsed.hostname.casefold().rstrip("."), port


class CsrfProtector:
    cookie_name = "study_hub_csrf"
    header_name = "X-CSRF-Token"

    def __init__(self, secret: bytes) -> None:
        if len(secret) < 32:
            raise ValueError("CSRF secret must be at least 32 bytes")
        self.secret = secret

    @classmethod
    def from_data_dir(cls, data_dir: Path) -> "CsrfProtector":
        security_dir = data_dir / "security"
        security_dir.mkdir(parents=True, exist_ok=True)
        secret_path = security_dir / "csrf.key"
        try:
            secret = secret_path.read_bytes()
        except FileNotFoundError:
            secret = secrets.token_bytes(32)
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
            descriptor = os.open(secret_path, flags, 0o600)
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(secret)
        return cls(secret)

    def issue(self) -> str:
        nonce = secrets.token_urlsafe(32)
        signature = hmac.new(
            self.secret,
            nonce.encode("ascii"),
            hashlib.sha256,
        ).hexdigest()
        return f"{nonce}.{signature}"

    def verify(self, cookie_token: str | None, submitted_token: str | None) -> bool:
        if not cookie_token or not submitted_token:
            return False
        if not hmac.compare_digest(cookie_token, submitted_token):
            return False
        try:
            nonce, signature = cookie_token.rsplit(".", 1)
        except ValueError:
            return False
        expected = hmac.new(
            self.secret,
            nonce.encode("ascii"),
            hashlib.sha256,
        ).hexdigest()
        return hmac.compare_digest(signature, expected)
