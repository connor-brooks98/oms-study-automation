import hashlib
from datetime import UTC, datetime
from urllib.parse import parse_qs, urlparse

import httpx
import respx
from fastapi.testclient import TestClient

from oms_hub.app import create_app
from oms_hub.config import Settings
from oms_hub.panopto.domain import PanoptoSession, RecordingMatch
from oms_hub.repositories import CatalogRepository, LectureInput


class MemorySecrets:
    def __init__(self):
        self.values = {
            "panopto-client-secret": "configured",
            "panopto-refresh-token": "refresh-token",
            "openai-api-key": "configured",
        }

    def get(self, key: str) -> str | None:
        return self.values.get(key)

    def set(self, key: str, value: str) -> None:
        self.values[key] = value

    def delete(self, key: str) -> None:
        self.values.pop(key, None)


def panopto_client_for(tmp_path):
    prompt_path = tmp_path / "vault" / "Transcript Cleaning.md"
    prompt_path.parent.mkdir()
    prompt_path.write_text("Preserve every substantive fact.", encoding="utf-8")
    settings = Settings(
        _env_file=None,
        data_dir=tmp_path,
        database_url=f"sqlite:///{tmp_path / 'panopto-web.db'}",
        panopto_client_id="client-id",
        transcript_prompt_path=prompt_path,
        panopto_revision_root=tmp_path / "revisions",
        study_root=tmp_path / "OMS II",
    )
    app = create_app(settings)
    app.state.secrets = MemorySecrets()
    app.state.panopto_tokens.secrets = app.state.secrets
    app.state.openai_cleaner.secrets = app.state.secrets
    return TestClient(app), app, prompt_path


def test_setup_never_renders_secrets(tmp_path):
    client, _, _ = panopto_client_for(tmp_path)

    page = client.get("/panopto/setup")

    assert page.status_code == 200
    assert "Panopto client secret" in page.text
    assert "OpenAI credential" in page.text
    assert "Configured" in page.text
    assert "configured" not in page.text


def test_connect_redirects_to_panopto_with_callback_and_offline_access(tmp_path):
    client, _, _ = panopto_client_for(tmp_path)

    response = client.post("/panopto/oauth/connect", follow_redirects=False)

    assert response.status_code == 303
    location = response.headers["location"]
    query = parse_qs(urlparse(location).query)
    assert location.startswith(
        "https://lmunet.hosted.panopto.com/Panopto/oauth2/connect/authorize?"
    )
    assert query["redirect_uri"] == [
        "http://127.0.0.1:8765/panopto/oauth/callback"
    ]
    assert "offline_access" in query["scope"][0].split()


@respx.mock
def test_oauth_callback_saves_connection_without_exposing_tokens(tmp_path):
    client, app, _ = panopto_client_for(tmp_path)
    app.state.secrets.delete("panopto-refresh-token")
    connect = client.post("/panopto/oauth/connect", follow_redirects=False)
    state = parse_qs(urlparse(connect.headers["location"]).query)["state"][0]
    respx.post(
        "https://lmunet.hosted.panopto.com/Panopto/oauth2/connect/token"
    ).mock(
        return_value=httpx.Response(
            200,
            json={
                "access_token": "private-access",
                "refresh_token": "private-refresh",
                "expires_in": 3600,
            },
        )
    )

    response = client.get(
        "/panopto/oauth/callback",
        params={"code": "authorization-code", "state": state},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/panopto/setup"
    assert app.state.secrets.get("panopto-refresh-token") == "private-refresh"
    page = client.get("/panopto/setup")
    assert "private-access" not in page.text
    assert "private-refresh" not in page.text
    assert "Connected as Panopto user" in page.text


def test_oauth_callback_rejects_bad_state_and_disconnect_removes_connection(tmp_path):
    client, app, _ = panopto_client_for(tmp_path)
    app.state.panopto_repository.mark_acceptance_validated()
    app.state.panopto_repository.set_enabled(True)

    rejected = client.get(
        "/panopto/oauth/callback",
        params={"code": "authorization-code", "state": "wrong"},
    )
    assert rejected.status_code == 409

    disconnected = client.post("/panopto/oauth/disconnect", follow_redirects=False)
    assert disconnected.status_code == 303
    assert app.state.secrets.get("panopto-refresh-token") is None
    connection = app.state.panopto_repository.connection()
    assert connection.enabled is False
    assert connection.acceptance_validated_at is None


def test_enable_requires_acceptance_current_prompt_and_both_credentials(tmp_path):
    client, app, prompt_path = panopto_client_for(tmp_path)
    response = client.post("/panopto/enable", follow_redirects=False)
    assert response.status_code == 409

    digest = hashlib.sha256(prompt_path.read_bytes()).hexdigest()
    app.state.panopto_repository.mark_acceptance_validated(
        datetime(2026, 7, 23, 14, tzinfo=UTC)
    )
    app.state.panopto_repository.approve_prompt(digest, str(prompt_path))
    app.state.panopto_prompt.approved_sha256 = digest

    response = client.post("/panopto/enable", follow_redirects=False)

    assert response.status_code == 303
    assert app.state.panopto_repository.connection().enabled is True


def test_prompt_change_after_approval_blocks_enable(tmp_path):
    client, app, prompt_path = panopto_client_for(tmp_path)
    digest = hashlib.sha256(prompt_path.read_bytes()).hexdigest()
    app.state.panopto_repository.mark_acceptance_validated()
    app.state.panopto_repository.approve_prompt(digest, str(prompt_path))
    app.state.panopto_prompt.approved_sha256 = digest
    prompt_path.write_text("Changed prompt.", encoding="utf-8")

    response = client.post("/panopto/enable")

    assert response.status_code == 409


def test_pause_and_cross_site_protection(tmp_path):
    client, app, _ = panopto_client_for(tmp_path)
    app.state.panopto_repository.set_enabled(True)

    response = client.post("/panopto/pause", follow_redirects=False)
    assert response.status_code == 303
    assert app.state.panopto_repository.connection().enabled is False

    response = client.post(
        "/panopto/pause",
        headers={"Origin": "https://malicious.example"},
    )
    assert response.status_code == 403


def test_review_remaps_recording_without_rendering_transcript_body(tmp_path):
    client, app, _ = panopto_client_for(tmp_path)
    catalog = CatalogRepository(app.state.database)
    lecture_id = catalog.upsert_lecture(
        LectureInput("MSK", 1, 6, "Shoulder", "Silvers", None)
    )
    disposition = app.state.panopto_repository.upsert_recording(
        PanoptoSession(
            "8796399e-393c-4256-b6e4-b48f0150d156",
            "Ambiguous Shoulder",
            datetime(2026, 7, 23, 13, tzinfo=UTC),
            3600,
            "MSK",
            "English_USA",
            None,
        ),
        RecordingMatch(None, 0.70, ("competing candidates",), True),
    )

    page = client.get("/panopto/review")
    assert page.status_code == 200
    assert "Ambiguous Shoulder" in page.text
    assert "Raw shoulder transcript" not in page.text

    response = client.post(
        f"/panopto/review/{disposition.recording_id}/remap",
        data={"lecture_id": str(lecture_id)},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert (
        app.state.panopto_repository.get_recording(disposition.recording_id).lecture_id
        == lecture_id
    )
