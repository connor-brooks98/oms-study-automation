from fastapi.testclient import TestClient

from oms_hub.app import create_app
from oms_hub.config import Settings
from oms_hub.study_generation.google_connection import (
    GoogleConnectionStatus,
    GoogleSurfaceStatus,
)


class FakeGoogleConnection:
    def status(self):
        return GoogleConnectionStatus(
            "connected",
            "student@example.com",
            tuple(
                GoogleSurfaceStatus(name, "connected")
                for name in ("notebook", "docs")
            ),
            None,
        )

    def test(self):
        return self.status()

    def start_interactive(self):
        return GoogleConnectionStatus("connecting", None, (), None)


def test_google_status_is_secret_safe_and_not_cacheable(tmp_path):
    app = create_app(
        Settings(
            _env_file=None,
            data_dir=tmp_path,
            database_url=f"sqlite:///{tmp_path / 'hub.db'}",
        )
    )
    app.state.google_connection = FakeGoogleConnection()

    response = TestClient(app).get("/settings/google/status")

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.json()["account_email"] == "student@example.com"
    assert all(
        forbidden not in response.text
        for forbidden in ("refresh_token", "client_secret", "SID", "ya29.")
    )


def test_settings_shows_google_connection_card(tmp_path):
    app = create_app(
        Settings(
            _env_file=None,
            data_dir=tmp_path,
            database_url=f"sqlite:///{tmp_path / 'hub.db'}",
        )
    )
    page = TestClient(app).get("/settings")

    assert "Google workspace" in page.text
    assert "Connect Google" in page.text
    assert "OAuth client JSON" in page.text
    assert "data-google-status" in page.text


def test_oauth_client_upload_does_not_echo_secret(tmp_path):
    app = create_app(
        Settings(
            _env_file=None,
            data_dir=tmp_path,
            database_url=f"sqlite:///{tmp_path / 'hub.db'}",
        )
    )
    client = TestClient(app)
    payload = {
        "installed": {
            "client_id": "client.apps.googleusercontent.com",
            "client_secret": "super-secret",
            "redirect_uris": ["http://localhost"],
        }
    }

    response = client.post(
        "/settings/google/oauth-client",
        files={"client_file": ("client.json", __import__("json").dumps(payload))},
    )

    assert response.status_code == 200
    assert response.json() == {"configured": True}
    assert "super-secret" not in response.text


def test_connect_response_keeps_all_google_surfaces_connecting(tmp_path):
    app = create_app(
        Settings(
            _env_file=None,
            data_dir=tmp_path,
            database_url=f"sqlite:///{tmp_path / 'hub.db'}",
        )
    )
    app.state.google_connection = FakeGoogleConnection()

    response = TestClient(app).post("/settings/google/connect", json={})

    assert response.status_code == 202
    assert response.json() == {
        "state": "connecting",
        "account_email": None,
        "surfaces": [
            {"name": "notebook", "state": "connecting", "message": None},
            {"name": "docs", "state": "connecting", "message": None},
        ],
        "message": "Complete Google sign-in in the browser window.",
    }
