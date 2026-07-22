import pytest

from oms_hub.canvas.pairing import PairingService
from oms_hub.canvas.repository import CanvasRepository


class MemorySecretStore:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}

    def get(self, key: str) -> str | None:
        return self.values.get(key)

    def set(self, key: str, value: str) -> None:
        self.values[key] = value

    def delete(self, key: str) -> None:
        self.values.pop(key, None)


def test_pairing_code_is_one_time_and_database_stores_only_fingerprint(database) -> None:
    repository = CanvasRepository(database)
    secrets = MemorySecretStore()
    service = PairingService(repository, secrets)
    code = service.create_code()
    bearer = service.exchange(code.value, "extension-test")
    service.verify(bearer)
    assert repository.connection().credential_fingerprint != bearer
    assert secrets.get("canvas_extension_bearer") == bearer
    with pytest.raises(ValueError, match="expired or already used"):
        service.exchange(code.value, "second-extension")


def test_revocation_removes_bearer(database) -> None:
    repository = CanvasRepository(database)
    secrets = MemorySecretStore()
    service = PairingService(repository, secrets)
    code = service.create_code()
    bearer = service.exchange(code.value, "extension-test")
    service.revoke()
    with pytest.raises(PermissionError):
        service.verify(bearer)
