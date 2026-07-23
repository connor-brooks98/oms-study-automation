from datetime import UTC, datetime

from fastapi.testclient import TestClient

from oms_hub.app import create_app
from oms_hub.canvas.pairing import PairingService
from oms_hub.config import Settings
from oms_hub.panopto.browser_domain import BrowserCommandKind
from oms_hub.repositories import CatalogRepository, LectureInput
from tests.canvas.test_pairing import MemorySecretStore

NOW = datetime(2026, 7, 23, 13, 20, tzinfo=UTC)
SESSION_ID = "8796399e-393c-4256-b6e4-b48f0150d156"
VIEWER_URL = (
    "https://lmunet.hosted.panopto.com/Panopto/Pages/Viewer.aspx?"
    f"id={SESSION_ID}"
)


def _prepared_client(tmp_path):
    settings = Settings(
        _env_file=None,
        data_dir=tmp_path,
        database_url=f"sqlite:///{tmp_path / 'api.db'}",
        panopto_revision_root=tmp_path / "revisions",
        study_root=tmp_path / "OMS II",
        transcript_prompt_path=tmp_path / "Transcript Cleaning.md",
        panopto_max_caption_bytes=128,
    )
    app = create_app(settings)
    app.state.canvas_pairing = PairingService(
        app.state.canvas_repository,
        MemorySecretStore(),
    )
    code = app.state.canvas_pairing.create_code()
    bearer = app.state.canvas_pairing.exchange(code.value, "test-extension")
    catalog = CatalogRepository(app.state.database)
    lecture_id = catalog.upsert_lecture(
        LectureInput(
            "MSK",
            1,
            6,
            "Shoulder Disease Injury and Treatment",
            "Joseph Silvers, DO",
            None,
        )
    )
    catalog.update_schedule(lecture_id, "2026-07-23T12:00:00+00:00", "DCOM 101")
    return TestClient(app), {"Authorization": f"Bearer {bearer}"}


def _recording_json() -> dict[str, object]:
    return {
        "session_id": SESSION_ID,
        "name": "6H. MSK Shoulder Disease Injury and Treatment Joseph Silvers",
        "created_utc": "2026-07-23T13:05:00Z",
        "duration_seconds": 3600,
        "folder_name": "Shared with Me",
        "viewer_url": VIEWER_URL,
    }


def _running_scan(client: TestClient, headers: dict[str, str]) -> str:
    client.app.state.panopto_repository.queue_browser_command(
        BrowserCommandKind.SCAN,
        {"manual": True},
        NOW,
    )
    return client.get("/api/panopto/command", headers=headers).json()["id"]


def test_panopto_api_requires_existing_companion_bearer(tmp_path):
    client, _ = _prepared_client(tmp_path)

    assert client.get("/api/panopto/command").status_code == 401


def test_command_is_claimed_once(tmp_path):
    client, headers = _prepared_client(tmp_path)
    command_id = client.app.state.panopto_repository.queue_browser_command(
        BrowserCommandKind.SCAN,
        {"manual": True},
        NOW,
    )

    response = client.get("/api/panopto/command", headers=headers)
    empty = client.get("/api/panopto/command", headers=headers)

    assert response.status_code == 200
    assert response.json() == {
        "id": command_id,
        "kind": "scan",
        "payload": {"manual": True},
    }
    assert empty.status_code == 204


def test_discover_rejects_extra_fields(tmp_path):
    client, headers = _prepared_client(tmp_path)
    item = _recording_json()
    item["cookie"] = "must-not-be-accepted"

    response = client.post(
        "/api/panopto/discover",
        headers=headers,
        json={"command_id": "84729a54-a9a7-4835-9535-e44f8bbcb375", "recordings": [item]},
    )

    assert response.status_code == 422


def test_discovery_returns_bounded_extract_disposition(tmp_path):
    client, headers = _prepared_client(tmp_path)
    command_id = _running_scan(client, headers)

    response = client.post(
        "/api/panopto/discover",
        headers=headers,
        json={
            "command_id": command_id,
            "recordings": [_recording_json()],
        },
    )

    assert response.status_code == 200
    disposition = response.json()["dispositions"][0]
    assert disposition["action"] == "extract_transcript"
    assert disposition["viewer_url"] == VIEWER_URL


def test_discovery_rejects_unknown_command(tmp_path):
    client, headers = _prepared_client(tmp_path)

    response = client.post(
        "/api/panopto/discover",
        headers=headers,
        json={
            "command_id": "84729a54-a9a7-4835-9535-e44f8bbcb375",
            "recordings": [_recording_json()],
        },
    )

    assert response.status_code == 409


def test_transcript_body_obeys_configured_limit(tmp_path):
    client, headers = _prepared_client(tmp_path)

    response = client.post(
        "/api/panopto/transcript",
        headers=headers,
        json={
            "command_id": "84729a54-a9a7-4835-9535-e44f8bbcb375",
            "recording_id": 1,
            "session_id": SESSION_ID,
            "viewer_url": VIEWER_URL,
            "language": "English_USA",
            "line_count": 1,
            "complete": True,
            "text": "x" * 129,
        },
    )

    assert response.status_code == 413
