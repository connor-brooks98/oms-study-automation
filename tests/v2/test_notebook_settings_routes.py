from fastapi.testclient import TestClient

from oms_hub.app import create_app
from oms_hub.config import Settings
from oms_hub.study_generation.notebook_connection import (
    NotebookConnectionStatus,
)


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
