import hashlib
import hmac
import secrets
import time
from dataclasses import dataclass

from oms_hub.canvas.repository import CanvasRepository
from oms_hub.security.secret_store import SecretStore

BEARER_KEY = "canvas_extension_bearer"


@dataclass(frozen=True, slots=True)
class PairingCode:
    value: str
    expires_in_seconds: int


class PairingService:
    def __init__(self, repository: CanvasRepository, secrets_store: SecretStore):
        self.repository = repository
        self.secrets = secrets_store
        self._code_hash: str | None = None
        self._expires_at = 0.0

    def create_code(self) -> PairingCode:
        value = f"{secrets.randbelow(1_000_000):06d}"
        self._code_hash = hashlib.sha256(value.encode()).hexdigest()
        self._expires_at = time.monotonic() + 300
        return PairingCode(value, 300)

    def exchange(self, code: str, extension_id: str) -> str:
        candidate = hashlib.sha256(code.encode()).hexdigest()
        valid = (
            self._code_hash is not None
            and time.monotonic() <= self._expires_at
            and hmac.compare_digest(candidate, self._code_hash)
        )
        self._code_hash = None
        self._expires_at = 0.0
        if not valid:
            raise ValueError("pairing code expired or already used")
        bearer = secrets.token_urlsafe(32)
        self.secrets.set(BEARER_KEY, bearer)
        fingerprint = hashlib.sha256(bearer.encode()).hexdigest()
        self.repository.set_pairing(extension_id, fingerprint)
        return bearer

    def verify(self, bearer: str) -> None:
        stored = self.secrets.get(BEARER_KEY)
        connection = self.repository.connection()
        if (
            not stored
            or not connection.credential_fingerprint
            or not hmac.compare_digest(stored, bearer)
            or not hmac.compare_digest(
                hashlib.sha256(bearer.encode()).hexdigest(),
                connection.credential_fingerprint,
            )
        ):
            raise PermissionError("invalid Canvas companion credential")

    def revoke(self) -> None:
        self.secrets.delete(BEARER_KEY)
        self.repository.clear_pairing()
