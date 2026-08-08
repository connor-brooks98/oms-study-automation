import json
import os

from cryptography.fernet import Fernet

from oms_hub.study_generation.notebook_storage import (
    NOTEBOOK_STORAGE_KEY,
    EncryptedNotebookStorage,
    migrate_encrypted_notebook_storage,
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


def test_encrypted_session_is_exported_once_for_plaintext_cli(tmp_path):
    secrets = MemorySecrets()
    encrypted_path = tmp_path / "google" / "notebooklm-storage.enc"
    plaintext_path = tmp_path / "google" / "notebooklm-storage.json"
    storage = EncryptedNotebookStorage(encrypted_path, secrets)
    payload = b'{"cookies":[{"value":"existing-session"}]}'
    with storage.plaintext(writable=True) as temporary_path:
        temporary_path.write_bytes(payload)

    assert migrate_encrypted_notebook_storage(
        encrypted_path,
        plaintext_path,
        secrets,
    )
    assert plaintext_path.read_bytes() == payload
    assert encrypted_path.is_file()
    if os.name != "nt":
        assert plaintext_path.stat().st_mode & 0o777 == 0o600

    plaintext_path.write_bytes(b'{"cookies":[{"value":"newer-session"}]}')
    assert not migrate_encrypted_notebook_storage(
        encrypted_path,
        plaintext_path,
        secrets,
    )
    assert b"newer-session" in plaintext_path.read_bytes()


def test_missing_encrypted_session_is_a_noop(tmp_path):
    secrets = MemorySecrets()

    assert not migrate_encrypted_notebook_storage(
        tmp_path / "notebooklm-storage.enc",
        tmp_path / "notebooklm-storage.json",
        secrets,
    )
    assert secrets.values == {}
