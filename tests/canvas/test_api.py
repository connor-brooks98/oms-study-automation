from fastapi.testclient import TestClient

from oms_hub.app import create_app
from oms_hub.canvas.domain import CourseMappingInput
from oms_hub.canvas.pairing import PairingService
from oms_hub.config import Settings
from tests.canvas.test_pairing import MemorySecretStore


def prepared_client(tmp_path):
    settings = Settings(
        _env_file=None,
        data_dir=tmp_path,
        database_url=f"sqlite:///{tmp_path / 'api.db'}",
    )
    app = create_app(settings)
    app.state.canvas_pairing = PairingService(
        app.state.canvas_repository,
        MemorySecretStore(),
    )
    app.state.canvas_repository.replace_course_mappings(
        [CourseMappingInput("751", "Hematology & Lymph", "HEME", "Heme/Lymph")]
    )
    code = app.state.canvas_pairing.create_code()
    bearer = app.state.canvas_pairing.exchange(code.value, "test-extension")
    return TestClient(app), {"Authorization": f"Bearer {bearer}"}


def payload() -> dict[str, object]:
    return {
        "course_id": "751",
        "course_name": "Hematology & Lymph",
        "course_code": "HEME",
        "module_id": "10",
        "module_title": "Exam 1 Lectures",
        "item_id": "20",
        "item_title": "Lecture 4: Anemia I",
        "item_type": "Page",
        "page_url": "/courses/751/pages/anemia-i",
        "page_title": "Lecture 4: Anemia I",
        "file_id": "30",
        "filename": "Anemia.pptx",
        "content_type": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        "size": 1234,
        "modified_at": "2026-07-21T12:00:00Z",
        "download_url": "https://lmunet.instructure.com/files/30/download",
        "evidence_text": "",
    }


def test_canvas_api_rejects_missing_bearer(tmp_path) -> None:
    client, _ = prepared_client(tmp_path)
    response = client.post("/api/canvas/heartbeat", json={"state": "connected"})
    assert response.status_code == 401


def test_discover_rejects_non_lmu_urls(tmp_path) -> None:
    client, headers = prepared_client(tmp_path)
    item = payload()
    item["download_url"] = "https://evil.example/file"
    response = client.post("/api/canvas/discover", headers=headers, json={"items": [item]})
    assert response.status_code == 422


def test_discover_rejects_oversized_batches(tmp_path) -> None:
    client, headers = prepared_client(tmp_path)
    response = client.post(
        "/api/canvas/discover",
        headers=headers,
        json={"items": [payload()] * 501},
    )
    assert response.status_code == 422


def test_discovery_only_returns_review_without_download(tmp_path) -> None:
    client, headers = prepared_client(tmp_path)
    response = client.post(
        "/api/canvas/discover",
        headers=headers,
        json={"items": [payload()]},
    )
    assert response.status_code == 200
    disposition = response.json()["dispositions"][0]
    assert disposition["action"] == "review"
