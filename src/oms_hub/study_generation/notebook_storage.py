import getpass
import os
import shutil
import subprocess
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

from cryptography.fernet import Fernet, InvalidToken

from oms_hub.security.secret_store import SecretStore

NOTEBOOK_STORAGE_KEY = "notebooklm-storage-encryption-key"


class NotebookStorageError(RuntimeError):
    pass


class EncryptedNotebookStorage:
    def __init__(
        self,
        encrypted_path: Path,
        secrets: SecretStore,
        *,
        legacy_plaintext_path: Path | None = None,
    ) -> None:
        self.encrypted_path = encrypted_path.resolve()
        self.legacy_plaintext_path = (
            legacy_plaintext_path.resolve()
            if legacy_plaintext_path is not None
            else None
        )
        self.secrets = secrets

    @contextmanager
    def plaintext(self, *, writable: bool = False) -> Iterator[Path]:
        self._migrate_legacy()
        temporary_dir = Path(tempfile.mkdtemp(prefix="oms-notebooklm-"))
        plaintext_path = temporary_dir / "notebooklm-storage.json"
        try:
            _restrict_owner_only(temporary_dir, directory=True)
            if self.encrypted_path.is_file():
                encrypted = self.encrypted_path.read_bytes()
                try:
                    payload = self._fernet(create=False).decrypt(encrypted)
                except InvalidToken as error:
                    raise NotebookStorageError(
                        "NotebookLM storage could not be decrypted; reconnect Google."
                    ) from error
                _private_write(plaintext_path, payload)
            yield plaintext_path
            if writable and plaintext_path.is_file():
                self._persist(plaintext_path.read_bytes())
        finally:
            try:
                plaintext_path.unlink()
            except FileNotFoundError:
                pass
            shutil.rmtree(temporary_dir, ignore_errors=True)

    def _migrate_legacy(self) -> None:
        legacy = self.legacy_plaintext_path
        if (
            legacy is None
            or not legacy.is_file()
            or self.encrypted_path.is_file()
        ):
            return
        self._persist(legacy.read_bytes())
        legacy.unlink()

    def _persist(self, payload: bytes) -> None:
        self.encrypted_path.parent.mkdir(parents=True, exist_ok=True)
        _restrict_owner_only(self.encrypted_path.parent, directory=True)
        encrypted = self._fernet(create=True).encrypt(payload)
        part = self.encrypted_path.with_name(
            f".{self.encrypted_path.name}.{uuid4().hex}.part"
        )
        try:
            _private_write(part, encrypted)
            os.replace(part, self.encrypted_path)
            _restrict_owner_only(self.encrypted_path, directory=False)
        finally:
            try:
                part.unlink()
            except FileNotFoundError:
                pass

    def _fernet(self, *, create: bool) -> Fernet:
        encoded_key = self.secrets.get(NOTEBOOK_STORAGE_KEY)
        if encoded_key is None:
            if not create:
                raise NotebookStorageError(
                    "NotebookLM storage key is missing; reconnect Google."
                )
            encoded_key = Fernet.generate_key().decode("ascii")
            self.secrets.set(NOTEBOOK_STORAGE_KEY, encoded_key)
        try:
            return Fernet(encoded_key.encode("ascii"))
        except (UnicodeEncodeError, ValueError) as error:
            raise NotebookStorageError(
                "NotebookLM storage key is invalid; reconnect Google."
            ) from error


class PlaintextNotebookStorage:
    """Compatibility adapter for tests and explicit non-production callers."""

    def __init__(self, path: Path) -> None:
        self.path = path.resolve()

    @contextmanager
    def plaintext(self, *, writable: bool = False) -> Iterator[Path]:
        del writable
        yield self.path


def _private_write(path: Path, payload: bytes) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(payload)
    _restrict_owner_only(path, directory=False)


def _restrict_owner_only(path: Path, *, directory: bool) -> None:
    if os.name != "nt":
        path.chmod(0o700 if directory else 0o600)
        return
    permission = "(OI)(CI)F" if directory else "F"
    result = subprocess.run(
        [
            "icacls",
            str(path),
            "/inheritance:r",
            "/grant:r",
            f"{getpass.getuser()}:{permission}",
        ],
        capture_output=True,
        text=True,
        check=False,
        shell=False,
    )
    if result.returncode != 0:
        raise NotebookStorageError(
            "NotebookLM credential storage permissions could not be secured."
        )
