from fastapi.testclient import TestClient
from PIL import Image

from oms_hub.app import create_app
from oms_hub.config import Settings
from oms_hub.document_processing.domain import (
    DocumentLocator,
    ParsedAsset,
    ParsedDocument,
    ParsedSegment,
    SegmentKind,
)
from oms_hub.files.atomic import sha256_file
from oms_hub.models import StudioRunModel
from oms_hub.repositories import LectureInput
from oms_hub.study_generation.domain import NativeQuiz, QuizChoice, QuizQuestion
from oms_hub.study_generation.native_quiz import serialize_native_quiz
from oms_hub.study_generation.practice_domain import (
    AnswerProvenance,
    QuestionDraft,
    QuestionSourceRef,
)
from oms_hub.study_generation.quiz_import_worker import _document_json


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


def _direct_review_run(client: TestClient, *, run_id: str = "direct-run") -> str:
    with client.app.state.database.session() as session:
        session.add(
            StudioRunModel(
                id=run_id,
                subject="Neuro",
                subject_key="neuro",
                exam_number=1,
                destination_subject="Neuro",
                destination_subject_key="neuro",
                destination_exam_number=1,
                label="Imported practice",
                label_key=f"imported practice {run_id}",
                prompt="",
                workflow_kind="direct_import",
                state="awaiting_review",
                stage="review",
            )
        )
    client.app.state.practice_review.store(
        run_id,
        (
            QuestionDraft(
                "question-1",
                "1",
                "Which answer is correct?",
                ("A", "B"),
                0,
                "Because.",
                None,
                (QuestionSourceRef("source", "segment", "page 1"),),
                AnswerProvenance.GENERATED_BY_AI,
                0.8,
                (),
                True,
                None,
            ),
        ),
    )
    return run_id


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


def test_direct_review_data_is_safe_and_edits_and_verification_require_csrf(tmp_path) -> None:
    client = _client(tmp_path)
    run_id = _direct_review_run(client)

    page = client.get(f"/studio/runs/{run_id}/review")
    data = client.get(f"/studio/runs/{run_id}/review/data")
    denied = client.patch(
        f"/studio/runs/{run_id}/questions/question-1",
        json={"stem": "Edited", "choices": ["A", "B"], "correct_index": 0, "rationale": "Because."},
    )
    unsafe = client.patch(
        f"/studio/runs/{run_id}/questions/question-1",
        json={"chosen_image": {"path": "/private/image.png"}},
        headers=_csrf_headers(client),
    )
    blocked_publication = client.post(
        f"/studio/runs/{run_id}/publication",
        headers=_csrf_headers(client),
    )
    verified = client.post(
        f"/studio/runs/{run_id}/questions/question-1/verify-answer",
        headers=_csrf_headers(client),
    )

    assert page.status_code == 200
    assert "Review imported questions" in page.text
    assert data.status_code == 200
    question = data.json()["questions"][0]
    assert question["source_refs"] == [
        {"source_id": "source", "segment_key": "segment", "locator": "page 1"}
    ]
    assert "path" not in question
    assert "diagnostics" not in question
    assert all("path" not in candidate for candidate in question["candidates"])
    assert denied.status_code == 403
    assert unsafe.status_code == 422
    assert blocked_publication.status_code == 409
    assert verified.status_code == 200
    assert verified.json()["blockers"] == []


def test_direct_review_rejects_wrong_state_and_notebook_review_stays_compatible(tmp_path) -> None:
    client = _client(tmp_path)
    run_id = _direct_review_run(client)
    with client.app.state.database.session() as session:
        session.get(StudioRunModel, run_id).state = "complete"
        session.add(
            StudioRunModel(
                id="notebook-run",
                subject="Neuro",
                subject_key="neuro",
                exam_number=1,
                destination_subject="Neuro",
                destination_subject_key="neuro",
                destination_exam_number=1,
                label="Notebook quiz",
                label_key="notebook quiz",
                prompt="",
                workflow_kind="notebook_generation",
                state="awaiting_images",
                stage="image_review",
                draft_payload_json=serialize_native_quiz(
                    NativeQuiz(
                        "Notebook quiz",
                        (
                            QuizQuestion(
                                "q1",
                                "Stem",
                                (QuizChoice("c1", "A"), QuizChoice("c2", "B")),
                                "c1",
                                "Why",
                            ),
                        ),
                    )
                ),
            )
        )

    blocked = client.get(f"/studio/runs/{run_id}/review/data")
    redirected = client.get("/studio/runs/notebook-run/images", follow_redirects=False)
    notebook = client.get("/studio/runs/notebook-run/review")

    assert blocked.status_code == 409
    assert redirected.status_code == 307
    assert redirected.headers["location"] == "/studio/runs/notebook-run/review"
    assert notebook.status_code == 200
    assert "data-image-review-page" in notebook.text


def test_candidate_preview_is_question_scoped_and_selection_is_csrf_protected(tmp_path) -> None:
    client = _client(tmp_path)
    run_id = _direct_review_run(client)
    image_path = tmp_path / "candidate.png"
    Image.new("RGB", (2, 2), "red").save(image_path, format="PNG")
    document = ParsedDocument(
        "source",
        "a" * 64,
        "pdf",
        "test",
        "1",
        (
            ParsedSegment(
                "segment",
                SegmentKind.PARAGRAPH,
                "source",
                DocumentLocator("page 1", page_number=1),
                ("asset-1",),
            ),
        ),
        (
            ParsedAsset(
                "asset-1",
                image_path,
                "image/png",
                sha256_file(image_path),
                DocumentLocator("page 1 image", page_number=1),
                2,
                2,
                "embedded-pdf-image",
            ),
        ),
        (),
    )
    client.app.state.studio_repository.save_run_artifact(
        run_id, "parse:source", "b" * 64, _document_json(document)
    )
    data = client.get(f"/studio/runs/{run_id}/review/data").json()
    candidate = data["questions"][0]["candidates"][0]

    preview = client.get(candidate["preview_url"])
    cross_question = client.get(
        candidate["preview_url"].replace("question-1", "question-2")
    )
    forbidden = client.post(
        f"/studio/runs/{run_id}/questions/question-1/image-selection",
        json={"image_candidate_id": candidate["candidate_id"]},
    )
    selected = client.post(
        f"/studio/runs/{run_id}/questions/question-1/image-selection",
        json={"image_candidate_id": candidate["candidate_id"]},
        headers=_csrf_headers(client),
    )

    assert preview.status_code == 200
    assert preview.headers["content-type"] == "image/png"
    assert "candidate.png" not in preview.text
    assert cross_question.status_code == 404
    assert forbidden.status_code == 403
    assert selected.status_code == 200
    assert selected.json()["questions"][0]["selected_candidate_id"] == candidate["candidate_id"]
