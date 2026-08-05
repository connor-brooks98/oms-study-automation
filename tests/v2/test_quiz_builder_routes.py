from fastapi.testclient import TestClient

from oms_hub.app import create_app
from oms_hub.config import Settings
from oms_hub.repositories import LectureInput


def _client(tmp_path) -> TestClient:
    app = create_app(
        Settings(
            _env_file=None,
            data_dir=tmp_path,
            database_url=f"sqlite:///{tmp_path / 'hub.db'}",
        )
    )
    app.state.catalog_repository.upsert_lecture(
        LectureInput("Neuro", 1, 1, "Seizures", "", None)
    )
    client = TestClient(app)
    client.get("/studio")
    return client


def _csrf_headers(client: TestClient) -> dict[str, str]:
    token = client.cookies.get("study_hub_csrf")
    assert token is not None
    return {"X-CSRF-Token": token}


def _ready_import_source(client: TestClient, title: str, filename: str) -> str:
    response = client.post(
        "/studio/import/sources/file",
        data={"subject": "Neuro", "exam_number": "1", "title": title},
        files={"file": (filename, b"fixture", "text/html")},
        headers=_csrf_headers(client),
    )
    assert response.status_code == 202
    return response.json()["id"]


def test_queue_import_accepts_separate_question_and_answer_sources(tmp_path) -> None:
    client = _client(tmp_path)
    questions = _ready_import_source(client, "Questions", "questions.html")
    answers = _ready_import_source(client, "Answers", "answers.html")

    response = client.post(
        "/studio/import/runs",
        json={
            "subject": "Neuro",
            "exam_number": 1,
            "label": "Professor practice",
            "destination_subject": "Neuro",
            "destination_exam_number": 1,
            "content_kind": "practice_questions",
            "sources": [
                {"source_id": questions, "role": "questions"},
                {"source_id": answers, "role": "answer_key"},
            ],
        },
        headers=_csrf_headers(client),
    )

    assert response.status_code == 202
    assert response.json()["state"] == "queued"


def test_import_endpoints_require_csrf_and_reject_invalid_source_ids(tmp_path) -> None:
    client = _client(tmp_path)

    unauthenticated = client.post(
        "/studio/import/sources/text",
        data={"subject": "Neuro", "exam_number": "1", "title": "Questions", "text": "Q"},
    )
    invalid = client.post(
        "/studio/import/runs",
        json={
            "subject": "Neuro",
            "exam_number": 1,
            "label": "Professor practice",
            "destination_subject": "Neuro",
            "destination_exam_number": 1,
            "sources": [{"source_id": "not-a-uuid", "role": "questions"}],
        },
        headers=_csrf_headers(client),
    )

    assert unauthenticated.status_code == 403
    assert invalid.status_code == 422
