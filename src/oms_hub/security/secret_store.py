from typing import Protocol

import keyring

VOYAGE_API_KEY_SECRET = "voyage-api-key"


class SecretStore(Protocol):
    def get(self, key: str) -> str | None: ...

    def set(self, key: str, value: str) -> None: ...

    def delete(self, key: str) -> None: ...


class KeyringSecretStore:
    def __init__(self, service_name: str = "OMSStudyHub"):
        self.service_name = service_name

    def get(self, key: str) -> str | None:
        return keyring.get_password(self.service_name, key)

    def set(self, key: str, value: str) -> None:
        keyring.set_password(self.service_name, key, value)

    def delete(self, key: str) -> None:
        try:
            keyring.delete_password(self.service_name, key)
        except keyring.errors.PasswordDeleteError:
            return
