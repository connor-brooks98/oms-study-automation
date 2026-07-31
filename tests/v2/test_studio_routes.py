from fastapi.testclient import TestClient

from oms_hub.app import create_app
from oms_hub.config import Settings
from oms_hub.repositories import LectureInput
from tests.support import csrf_client


def _app(tmp_path, **settings):
    app = create_app(
        Settings(
            _env_file=None,
            data_dir=tmp_path,
            database_url=f"sqlite:///{tmp_path / 'hub.db'}",
            **settings,
        )
    )
    app.state.catalog_repository.upsert_lecture(
        LectureInput("Neuro Science", 2, 1, "Synapses", "", None)
    )
    return app


def test_studio_page_uses_course_and_exam_scope_without_lecture(tmp_path):
    response = TestClient(_app(tmp_path)).get("/studio")

    assert response.status_code == 200
    assert "Neuro Science" in response.text
    assert 'data-exams="2"' in response.text
    assert "lecture_id" not in response.text
    assert "only through NotebookLM chat" in response.text


def test_studio_source_submission_requires_csrf_and_current_scope(tmp_path):
    app = _app(tmp_path)
    assert (
        TestClient(app)
        .post(
            "/studio/sources/text",
            data={
                "subject": "Neuro Science",
                "exam_number": "2",
                "title": "Notes",
                "text": "content",
            },
        )
        .status_code
        == 403
    )
    client = csrf_client(app)

    invalid = client.post(
        "/studio/sources/text",
        data={
            "subject": "Forged",
            "exam_number": "2",
            "title": "Notes",
            "text": "content",
        },
    )
    accepted = client.post(
        "/studio/sources/text",
        data={
            "subject": "  neuro   science ",
            "exam_number": "2",
            "title": "Notes",
            "text": "content",
        },
    )

    assert invalid.status_code == 422
    assert accepted.status_code == 202
    listed = client.get("/studio/sources?subject_key=neuro%20science&exam_number=2")
    assert listed.json()["sources"][0]["title"] == "Notes"
    assert listed.json()["sources"][0]["state"] == "pending"


def test_studio_page_is_not_public_without_access_identity(tmp_path):
    app = _app(tmp_path, public_hostname="study.example.com")

    response = TestClient(app).get(
        "/studio",
        headers={"host": "study.example.com"},
    )

    assert response.status_code == 503
    assert response.json()["detail"] == "Cloudflare Access is not configured"


def test_prompt_only_run_is_queued_with_explicit_empty_source_snapshot(tmp_path):
    app = _app(tmp_path)
    client = csrf_client(app)

    response = client.post(
        "/studio/runs",
        json={
            "subject": "Neuro Science",
            "exam_number": 2,
            "prompt": "Create a board-style quiz.",
            "source_ids": [],
            "label": "Board review",
            "destination_subject": "Neuro Science",
            "destination_exam_number": 2,
        },
    )

    assert response.status_code == 202
    run = app.state.studio_repository.get_run(response.json()["id"])
    assert run.sources == ()
    assert "Return exactly one JSON object" in run.prompt
    history = client.get("/studio/runs?subject_key=neuro%20science&exam_number=2")
    assert history.headers["cache-control"] == "no-store"
    assert history.json()["runs"][0]["source_ids"] == []
