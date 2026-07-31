import json
from io import BytesIO

from fastapi.testclient import TestClient
from PIL import Image

from oms_hub.app import create_app
from oms_hub.config import Settings
from oms_hub.repositories import LectureInput
from oms_hub.study_generation.native_quiz import parse_native_quiz
from oms_hub.study_generation.studio_domain import StudioSourceState, StudioSourceType
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


def _png() -> bytes:
    output = BytesIO()
    Image.new("RGB", (12, 8), "navy").save(output, format="PNG")
    return output.getvalue()


def _awaiting_image_run(app):
    run = app.state.studio_repository.queue_run(
        "Neuro Science",
        2,
        "Prompt with image contract",
        [],
        "Image review",
        "Neuro Science",
        2,
    )
    quiz = parse_native_quiz(
        json.dumps(
            {
                "title": "Image review",
                "questions": [
                    {
                        "stem": f"Question {number}",
                        "choices": ["A", "B"],
                        "correct_index": 0,
                        "rationale": "A is correct.",
                        "image_ref": {
                            "key": "image-1",
                            "source_title": "<Professor website>",
                            "locator": "Image before question 4",
                            "description": "Reference image for questions 4-7",
                        },
                    }
                    for number in (4, 5, 6, 7)
                ],
            }
        )
    )
    app.state.studio_repository.await_image_review(
        run.id,
        "notebook-1",
        "private raw response",
        quiz,
    )
    return app.state.studio_repository.get_run(run.id)


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


def test_awaiting_run_lists_private_image_review_action(tmp_path):
    app = _app(tmp_path)
    run = _awaiting_image_run(app)

    response = csrf_client(app).get(
        "/studio/runs?subject_key=neuro%20science&exam_number=2"
    )

    item = response.json()["runs"][0]
    assert item["id"] == run.id
    assert item["state"] == "awaiting_images"
    assert item["image_review_url"] == f"/studio/runs/{run.id}/images"
    assert item["published_url"] is None


def test_image_review_page_and_json_group_shared_key_without_private_fields(tmp_path):
    app = _app(tmp_path)
    run = _awaiting_image_run(app)
    client = csrf_client(app)

    page = client.get(f"/studio/runs/{run.id}/images")
    response = client.get(f"/studio/runs/{run.id}/image-review")

    assert page.status_code == 200
    assert "Quiz images" in page.text
    payload = response.json()
    assert payload["run_id"] == run.id
    assert payload["resolved"] is False
    assert payload["requirements"][0]["image_key"] == "image-1"
    assert [item["number"] for item in payload["requirements"][0]["questions"]] == [
        1,
        2,
        3,
        4,
    ]
    assert "raw_response" not in response.text
    assert "private raw response" not in response.text
    assert "asset_path" not in response.text
    assert "sha256" not in response.text
    assert "rationale" not in response.text


def test_image_upload_requires_csrf_and_returns_only_safe_metadata(tmp_path):
    app = _app(tmp_path)
    run = _awaiting_image_run(app)
    path = f"/studio/runs/{run.id}/images/image-1"

    rejected = TestClient(app).post(
        path,
        files={"file": ("figure.png", _png(), "image/png")},
    )
    accepted = csrf_client(app).post(
        path,
        files={"file": ("figure.png", _png(), "image/png")},
    )

    assert rejected.status_code == 403
    assert accepted.status_code == 200
    assert accepted.json() == {
        "image_key": "image-1",
        "media_type": "image/png",
        "width": 12,
        "height": 8,
        "original_filename": "figure.png",
        "resolved": True,
    }


def test_invalid_replacement_preserves_current_image_binding(tmp_path):
    app = _app(tmp_path)
    run = _awaiting_image_run(app)
    client = csrf_client(app)
    path = f"/studio/runs/{run.id}/images/image-1"
    assert client.post(
        path,
        files={"file": ("figure.png", _png(), "image/png")},
    ).status_code == 200
    before = app.state.studio_repository.quiz_review(run.id).requirements[0].image

    rejected = client.post(
        path,
        files={"file": ("bad.png", b"not an image", "image/png")},
    )

    assert rejected.status_code == 422
    assert app.state.studio_repository.quiz_review(run.id).requirements[0].image == before


def test_question_image_override_is_confirmable_and_reversible(tmp_path):
    app = _app(tmp_path)
    run = _awaiting_image_run(app)
    client = csrf_client(app)
    path = f"/studio/runs/{run.id}/questions/q1/image-override"

    overridden = client.put(path)
    restored = client.delete(path)

    assert overridden.status_code == 200
    assert overridden.json()["requirements"][0]["questions"][0]["overridden"] is True
    assert restored.status_code == 200
    assert restored.json()["requirements"][0]["questions"][0]["overridden"] is False


def test_delete_source_removes_remote_binding_and_hides_it_from_future_picker(tmp_path):
    app = _app(tmp_path)
    source = app.state.studio_repository.create_source(
        "Neuro Science",
        2,
        StudioSourceType.URL,
        "Professor URL",
        source_url="https://example.com",
    )
    app.state.studio_repository.complete(source.id, "notebook-1", "remote-1")

    class Gateway:
        def __init__(self):
            self.deleted = []

        def delete_studio_source(self, notebook_id, source_id):
            self.deleted.append((notebook_id, source_id))

    gateway = Gateway()
    app.state.notebook_gateway = gateway

    response = csrf_client(app).delete(f"/studio/sources/{source.id}")

    assert response.status_code == 200
    assert gateway.deleted == [("notebook-1", "remote-1")]
    assert app.state.studio_repository.get(source.id).state is StudioSourceState.DELETED


def test_rerun_and_unpublish_keep_private_audit_history(tmp_path):
    app = _app(tmp_path)
    run = app.state.studio_repository.queue_run(
        "Neuro Science",
        2,
        "Prompt with contract",
        [],
        "Exam Review",
        "Neuro Science",
        2,
    )
    quiz = parse_native_quiz(
        '{"title":"Exam Review","questions":[{"stem":"Q?",'
        '"choices":["A","B"],"correct_index":0,"rationale":"Because."}]}'
    )
    published = app.state.generation_repository.publish_studio_quiz(run.id, quiz)
    client = csrf_client(app)

    rerun = client.post(f"/studio/runs/{run.id}/rerun")
    removed = client.delete(f"/studio/runs/{run.id}/publication")

    assert rerun.status_code == 202
    assert app.state.studio_repository.get_run(rerun.json()["id"]).supersedes_run_id == run.id
    assert removed.status_code == 200
    assert app.state.generation_repository.published_quiz(published.token) is None
    assert app.state.studio_repository.get_run(run.id).label == "Exam Review"
