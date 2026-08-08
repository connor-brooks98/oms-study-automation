from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import oms_hub.app as app_module
from oms_hub.app import create_app
from oms_hub.config import Settings
from oms_hub.db import Database
from oms_hub.study_generation.notebook_connection import (
    NotebookConnectionStatus,
)
from oms_hub.study_generation.notebook_storage import EncryptedNotebookStorage
from oms_hub.study_generation.repository import GenerationRepository


class FakeNotebookConnection:
    def status(self):
        return NotebookConnectionStatus("connected")

    def test(self):
        return self.status()

    def start_interactive(self):
        return NotebookConnectionStatus("connecting", "Complete sign-in.")


def app(tmp_path):
    created = create_app(
        Settings(
            _env_file=None,
            data_dir=tmp_path,
            database_url=f"sqlite:///{tmp_path / 'hub.db'}",
        )
    )
    created.state.notebook_connection = FakeNotebookConnection()
    return created


def test_notebook_status_is_secret_safe_and_not_cacheable(tmp_path):
    response = TestClient(app(tmp_path)).get("/settings/notebook/status")

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.json() == {"state": "connected", "message": None}
    assert all(
        forbidden not in response.text
        for forbidden in ("refresh_token", "client_secret", "SID", "ya29.")
    )


def test_settings_shows_only_notebook_connection_card(tmp_path):
    page = TestClient(app(tmp_path)).get("/settings")

    assert "Gemini Notebook" in page.text
    assert "Connect Notebook" in page.text
    assert "data-notebook-status" in page.text
    assert "Google Docs" not in page.text
    assert "OAuth client JSON" not in page.text


def test_connect_response_has_single_notebook_state(tmp_path):
    response = TestClient(app(tmp_path)).post(
        "/settings/notebook/connect",
        json={},
    )

    assert response.status_code == 202
    assert response.json()["state"] == "connecting"
    assert "surfaces" not in response.json()


def test_retired_google_settings_routes_are_gone(tmp_path):
    client = TestClient(app(tmp_path))

    assert client.get("/settings/google/status").status_code == 404
    assert client.post("/settings/google/connect").status_code == 404
    assert client.post("/settings/google/oauth-client").status_code == 404


class MemorySecrets:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}

    def get(self, key: str) -> str | None:
        return self.values.get(key)

    def set(self, key: str, value: str) -> None:
        self.values[key] = value

    def delete(self, key: str) -> None:
        self.values.pop(key, None)


def test_app_startup_migrates_existing_encrypted_notebook_session(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secrets = MemorySecrets()
    google = tmp_path / "google"
    encrypted_path = google / "notebooklm-storage.enc"
    plaintext_path = google / "notebooklm-storage.json"
    with EncryptedNotebookStorage(encrypted_path, secrets).plaintext(
        writable=True
    ) as temporary_path:
        temporary_path.write_text(
            '{"cookies":[{"value":"preserved-session"}]}',
            encoding="utf-8",
        )
    monkeypatch.setattr(app_module, "KeyringSecretStore", lambda: secrets)

    created = app_module.create_app(
        Settings(
            _env_file=None,
            data_dir=tmp_path,
            database_url=f"sqlite:///{tmp_path / 'hub.db'}",
        )
    )

    assert created.state.notebook_storage_migrated is True
    assert "preserved-session" in plaintext_path.read_text(encoding="utf-8")
    assert encrypted_path.is_file()


def test_app_starts_disconnected_when_encrypted_notebook_session_is_unreadable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secrets = MemorySecrets()
    google = tmp_path / "google"
    google.mkdir()
    encrypted_path = google / "notebooklm-storage.enc"
    plaintext_path = google / "notebooklm-storage.json"
    encrypted_path.write_bytes(b"unreadable-session")
    monkeypatch.setattr(app_module, "KeyringSecretStore", lambda: secrets)
    database_url = f"sqlite:///{tmp_path / 'hub.db'}"
    database = Database(database_url)
    database.migrate()
    GenerationRepository(database).save_google_status(
        state="connected",
        account_email=None,
        notebook_state="connected",
        gemini_state="unused",
        docs_state="retired",
        diagnostic=None,
        tested_at="2026-08-08T00:00:00+00:00",
    )

    created = app_module.create_app(
        Settings(
            _env_file=None,
            data_dir=tmp_path,
            database_url=database_url,
        )
    )

    assert created.state.notebook_storage_migrated is False
    assert "reconnect Google" in created.state.notebook_storage_migration_error
    status = created.state.notebook_connection.status()
    assert status.state == "failed"
    assert status.message == created.state.notebook_storage_migration_error
    assert not plaintext_path.exists()
    assert encrypted_path.read_bytes() == b"unreadable-session"
