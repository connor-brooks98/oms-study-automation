import json
import os

from cryptography.fernet import Fernet

from oms_hub.study_generation.notebook_storage import (
    NOTEBOOK_STORAGE_KEY,
    EncryptedNotebookStorage,
)


class MemorySecrets:
    def __init__(self):
        self.values = {}

    def get(self, key):
        return self.values.get(key)

    def set(self, key, value):
        self.values[key] = value

    def delete(self, key):
        self.values.pop(key, None)


def test_storage_is_encrypted_and_plaintext_is_deleted(tmp_path):
    secrets = MemorySecrets()
    encrypted_path = tmp_path / "google" / "notebooklm-storage.enc"
    storage = EncryptedNotebookStorage(encrypted_path, secrets)
    observed_plaintext = None

    with storage.plaintext(writable=True) as plaintext_path:
        observed_plaintext = plaintext_path
        plaintext_path.write_text('{"cookies":[{"value":"secret"}]}')

    assert observed_plaintext is not None
    assert not observed_plaintext.exists()
    assert encrypted_path.is_file()
    assert b"secret" not in encrypted_path.read_bytes()
    encoded_key = secrets.values[NOTEBOOK_STORAGE_KEY]
    decrypted = Fernet(encoded_key.encode("ascii")).decrypt(
        encrypted_path.read_bytes()
    )
    assert json.loads(decrypted)["cookies"][0]["value"] == "secret"
    if os.name != "nt":
        assert encrypted_path.stat().st_mode & 0o777 == 0o600


def test_legacy_plaintext_is_migrated_before_use(tmp_path):
    secrets = MemorySecrets()
    legacy_path = tmp_path / "notebooklm-storage.json"
    encrypted_path = tmp_path / "notebooklm-storage.enc"
    legacy_path.write_text('{"cookies":[]}', encoding="utf-8")
    storage = EncryptedNotebookStorage(
        encrypted_path,
        secrets,
        legacy_plaintext_path=legacy_path,
    )

    with storage.plaintext() as plaintext_path:
        assert plaintext_path.read_text(encoding="utf-8") == '{"cookies":[]}'

    assert not legacy_path.exists()
    assert encrypted_path.is_file()


def test_failed_write_does_not_persist_partial_plaintext(tmp_path):
    secrets = MemorySecrets()
    encrypted_path = tmp_path / "notebooklm-storage.enc"
    storage = EncryptedNotebookStorage(encrypted_path, secrets)
    observed_plaintext = None

    try:
        with storage.plaintext(writable=True) as plaintext_path:
            observed_plaintext = plaintext_path
            plaintext_path.write_text("partial", encoding="utf-8")
            raise RuntimeError("login failed")
    except RuntimeError:
        pass

    assert observed_plaintext is not None
    assert not observed_plaintext.exists()
    assert not encrypted_path.exists()
