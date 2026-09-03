import json
from io import BytesIO

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
    SourceSnapshot,
)
from oms_hub.files.atomic import sha256_file
from oms_hub.models import StudioRunModel
from oms_hub.repositories import LectureInput
from oms_hub.study_generation.domain import NativeQuiz, QuizChoice, QuizQuestion
from oms_hub.study_generation.native_quiz import serialize_native_quiz
from oms_hub.study_generation.practice_domain import (
    AnswerProvenance,
    MatchingPromptDraft,
    MatchingQuestionDraft,
    QuestionDraft,
    QuestionSourceRef,
)
from oms_hub.study_generation.quiz_import_worker import _document_json
from oms_hub.study_generation.studio_domain import StudioSourceState, StudioSourceType
from oms_hub.web.public_quiz_routes import _player_asset_version


def _client(tmp_path) -> TestClient:
    app = create_app(
        Settings(
            _env_file=None,
            data_dir=tmp_path,
            database_url=f"sqlite:///{tmp_path / 'hub.db'}",
        )
    )
    app.state.catalog_repository.upsert_lecture(LectureInput("Neuro", 1, 1, "Seizures", "", None))
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


def test_quiz_builder_keeps_generate_and_import_workflows(tmp_path) -> None:
    client = _client(tmp_path)

    response = client.get("/studio")

    assert response.status_code == 200
    assert "Quiz Builder" in response.text
    assert "Generate Quiz" in response.text
    assert "Import Practice Questions" in response.text
    assert 'href="/studio/library/quizzes"' in response.text
    assert "Manage released libraries" in response.text
    assert "NotebookLM Studio" not in response.text


def test_run_history_exposes_safe_direct_import_review_metadata(tmp_path) -> None:
    client = _client(tmp_path)
    run_id = _direct_review_run(client)

    response = client.get("/studio/runs", params={"subject_key": "neuro", "exam_number": 1})

    assert response.status_code == 200
    run = response.json()["runs"][0]
    assert run["workflow_kind"] == "direct_import"
    assert run["content_kind"] == "practice_questions"
    assert run["review_url"] == f"/studio/runs/{run_id}/review"
    assert "raw_response" not in run


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
                content_kind="practice_questions",
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


def _matching_review_run(client: TestClient, *, run_id: str = "matching-run") -> str:
    _direct_review_run(client, run_id=run_id)
    client.app.state.practice_review.store(
        run_id,
        (
            MatchingQuestionDraft(
                "matching-1",
                "1",
                "Match each description with its term.",
                (
                    MatchingPromptDraft("p1", "A", "Alpha", 1),
                    MatchingPromptDraft("p2", "B", "Beta", 0),
                ),
                ("Term one", "Term two"),
                "Source-marked matches: A -> Term two; B -> Term one.",
                None,
                (QuestionSourceRef("source", "segment", "page 1"),),
                AnswerProvenance.PROVIDED_BY_SOURCE,
                1.0,
                (),
                False,
                None,
            ),
        ),
    )
    return run_id


def test_matching_review_patch_and_preview_use_group_contract_and_fingerprint(tmp_path) -> None:
    client = _client(tmp_path)
    run_id = _matching_review_run(client)
    before = client.get(f"/studio/runs/{run_id}/review/data")
    question = before.json()["questions"][0]
    assert question["kind"] == "matching"
    assert question["prompts"] == [
        {"id": "p1", "label": "A", "text": "Alpha", "correct_index": 1},
        {"id": "p2", "label": "B", "text": "Beta", "correct_index": 0},
    ]
    verification = client.post(
        f"/studio/runs/{run_id}/questions/matching-1/verify-answer",
        headers=_csrf_headers(client),
    )
    assert verification.status_code == 409
    invalid = client.patch(
        f"/studio/runs/{run_id}/questions/matching-1",
        json={
            "kind": "matching",
            "stem": "Changed",
            "prompts": [{"id": "p1", "label": "A", "text": "Alpha", "correct_index": 1}],
            "choices": ["Term one", "Term two"],
            "rationale": "custom",
        },
        headers=_csrf_headers(client),
    )
    assert invalid.status_code == 422
    assert client.get(f"/studio/runs/{run_id}/review/data").json() == before.json()
    valid = client.patch(
        f"/studio/runs/{run_id}/questions/matching-1",
        json={
            "kind": "matching",
            "stem": "Changed",
            "prompts": [
                {"id": "p1", "label": "A", "text": "Alpha", "correct_index": 1},
                {"id": "p2", "label": "B", "text": "Beta", "correct_index": 0},
            ],
            "choices": ["Term one", "Term two"],
            "rationale": "Source-marked matches: A -> Term two; B -> Term one.",
        },
        headers=_csrf_headers(client),
    )
    assert valid.status_code == 200
    content = client.get(f"/studio/runs/{run_id}/preview/content")
    page = client.get(f"/studio/runs/{run_id}/preview")
    version = content.json()["version"]
    assert version.startswith("preview:") and len(version) == len("preview:") + 64
    assert f'data-quiz-version="{version}"' in page.text
    assert "correct_choice_id" not in content.text
    answer = client.post(
        f"/studio/runs/{run_id}/preview/answer",
        json={"kind": "matching", "question_id": "q1", "matches": {"p1": "c2", "p2": "c1"}},
    )
    assert answer.json() == {
        "kind": "matching",
        "correct": True,
        "correct_matches": {"p1": "c2", "p2": "c1"},
        "row_results": {"p1": True, "p2": True},
        "rationale": "Source-marked matches: A -> Term two; B -> Term one.",
    }
    changed = client.patch(
        f"/studio/runs/{run_id}/questions/matching-1",
        json={
            "kind": "matching",
            "stem": "Changed again",
            "prompts": [
                {"id": "p1", "label": "A", "text": "Alpha", "correct_index": 1},
                {"id": "p2", "label": "B", "text": "Beta", "correct_index": 0},
            ],
            "choices": ["Term one", "Term two"],
            "rationale": "Source-marked matches: A -> Term two; B -> Term one.",
        },
        headers=_csrf_headers(client),
    )
    assert changed.status_code == 200
    assert client.get(f"/studio/runs/{run_id}/preview/content").json()["version"] != version


def test_missing_review_artifacts_use_one_recovery_envelope(tmp_path) -> None:
    client = _client(tmp_path)
    run_id = "missing-review"
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
                label="Missing",
                label_key="missing",
                prompt="",
                workflow_kind="direct_import",
                content_kind="practice_questions",
                state="awaiting_review",
                stage="review",
            )
        )
    responses = [
        client.get(f"/studio/runs/{run_id}/review"),
        client.get(f"/studio/runs/{run_id}/review/data"),
        client.patch(
            f"/studio/runs/{run_id}/questions/question-1",
            json={"stem": "Edited"},
            headers=_csrf_headers(client),
        ),
        client.post(
            f"/studio/runs/{run_id}/questions/question-1/verify-answer",
            headers=_csrf_headers(client),
        ),
        client.post(
            f"/studio/runs/{run_id}/questions/question-1/image-selection",
            json={"image_candidate_id": None},
            headers=_csrf_headers(client),
        ),
        client.get(f"/studio/runs/{run_id}/questions/question-1/candidates/missing/preview"),
        client.get(f"/studio/runs/{run_id}/preview"),
        client.get(f"/studio/runs/{run_id}/preview/content"),
        client.get(f"/studio/runs/{run_id}/preview/media/missing"),
        client.post(
            f"/studio/runs/{run_id}/preview/answer",
            json={"question_id": "q1", "choice_id": "c1"},
        ),
    ]
    expected = {
        "error": {
            "code": "review_artifact_unavailable",
            "message": "Review data is unavailable for this import run.",
            "recovery": "Return to Quiz Builder and rerun the import.",
        }
    }
    assert all(
        response.status_code == 409 and response.json() == expected for response in responses
    )


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


def test_sources_expose_safe_persisted_import_defaults(tmp_path) -> None:
    client = _client(tmp_path)
    created = client.post(
        "/studio/import/sources/text",
        data={
            "subject": "Neuro",
            "exam_number": "1",
            "title": "Reference",
            "text": "facts",
            "role": "supporting_reference",
            "attach_to_notebook": "true",
        },
        headers=_csrf_headers(client),
    )
    assert created.status_code == 202
    sources = client.get("/studio/sources", params={"subject_key": "neuro", "exam_number": 1})
    assert sources.status_code == 200
    record = sources.json()["sources"][0]
    assert record["purpose"] == "local_import"
    assert record["import_defaults"] == {"role": "supporting_reference", "attach_to_notebook": True}

    invalid = client.post(
        "/studio/import/sources/text",
        data={
            "subject": "Neuro",
            "exam_number": "1",
            "title": "Questions",
            "text": "facts",
            "role": "questions",
            "attach_to_notebook": "true",
        },
        headers=_csrf_headers(client),
    )
    assert invalid.status_code == 422


def test_all_import_source_forms_persist_checked_and_unchecked_defaults_across_refresh(
    tmp_path,
) -> None:
    client = _client(tmp_path)

    class Snapshotter:
        def fetch(self, source_id: str, title: str, url: str) -> SourceSnapshot:
            path = tmp_path / f"{source_id}.html"
            path.write_text("<h1>Reference</h1>", encoding="utf-8")
            return SourceSnapshot(source_id, title, path, "text/html", sha256_file(path), url)

    client.app.state.studio_service.url_snapshot_service = Snapshotter()
    page = client.get("/studio")
    assert page.text.count('name="role" data-import-role') == 3
    assert page.text.count('name="attach_to_notebook" value="true" data-import-notebook') == 3

    submissions = tuple(
        (
            source_type,
            attach_to_notebook,
            {
                "title": f"{source_type.title()} reference {attach_to_notebook}",
                **({"text": "text facts"} if source_type == "text" else {}),
                **(
                    {"url": f"https://example.test/{source_type}/{attach_to_notebook}"}
                    if source_type == "url"
                    else {}
                ),
            },
            (
                {
                    "file": (
                        f"reference-{attach_to_notebook}.txt",
                        b"file facts",
                        "text/plain",
                    )
                }
                if source_type == "file"
                else None
            ),
        )
        for source_type in ("file", "text", "url")
        for attach_to_notebook in (True, False)
    )
    expected_defaults: dict[str, dict[str, object]] = {}
    for source_type, attach_to_notebook, fields, files in submissions:
        data = {
            "subject": "Neuro",
            "exam_number": "1",
            "role": "supporting_reference",
            "attach_to_notebook": str(attach_to_notebook).lower(),
            **fields,
        }
        response = client.post(
            f"/studio/import/sources/{source_type}",
            data=data,
            files=files,
            headers=_csrf_headers(client),
        )
        assert response.status_code == 202, response.text
        expected_defaults[fields["title"]] = {
            "role": "supporting_reference",
            "attach_to_notebook": attach_to_notebook,
        }

    refreshed = client.get("/studio/sources", params={"subject_key": "neuro", "exam_number": 1})
    assert refreshed.status_code == 200
    records = {record["title"]: record for record in refreshed.json()["sources"]}
    assert set(records) == set(expected_defaults)
    for title, record in records.items():
        assert record["purpose"] == "local_import"
        assert record["import_defaults"] == expected_defaults[title]


def test_remote_source_delete_is_queued_before_any_worker_effect(tmp_path) -> None:
    client = _client(tmp_path)
    source = client.app.state.studio_repository.create_source(
        "Neuro",
        1,
        StudioSourceType.TEXT,
        "Remote notes",
    )
    client.app.state.studio_repository.complete(
        source.id,
        "notebook-1",
        "remote-1",
    )

    response = client.delete(
        f"/studio/sources/{source.id}",
        headers=_csrf_headers(client),
    )

    assert response.status_code == 202
    assert response.json() == {"id": source.id, "state": "deleting"}
    stored = client.app.state.studio_repository.get(source.id)
    assert stored is not None
    assert stored.state is StudioSourceState.DELETING
    assert stored.remote_source_id == "remote-1"


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
    assert "Back to Quiz Builder" in page.text
    assert data.status_code == 200
    question = data.json()["questions"][0]
    assert question["source_refs"] == [
        {"source_id": "source", "segment_key": "segment", "locator": "page 1"}
    ]
    assert "path" not in question
    assert "diagnostics" not in question
    assert all("path" not in candidate for candidate in question["candidates"])
    assert data.json()["issues"]
    assert data.json()["issues"][0]["role"] == "err"
    assert all("path" not in issue for issue in data.json()["issues"])
    assert denied.status_code == 403
    assert unsafe.status_code == 422
    assert blocked_publication.status_code == 409
    assert verified.status_code == 200
    assert verified.json()["blockers"] == []


def test_run_diagnostic_acknowledgement_is_csrf_protected_and_hard_items_reject(
    tmp_path,
) -> None:
    client = _client(tmp_path)
    run_id = _direct_review_run(client)
    client.app.state.practice_review.verify_generated_answer(run_id, "question-1")
    client.app.state.studio_repository.save_run_artifact(
        run_id,
        "review:run-diagnostics",
        "a" * 64,
        json.dumps(
            [
                {
                    "code": "incomplete-sequential-question-extraction",
                    "message": "Count needs review",
                    "severity": "blocker",
                    "overridable": True,
                    "acknowledged": False,
                },
                {
                    "code": "parser-blocker",
                    "message": "OCR unavailable",
                    "severity": "blocker",
                    "overridable": False,
                    "acknowledged": False,
                },
            ]
        ),
    )
    soft_url = (
        f"/studio/runs/{run_id}/run-diagnostics/"
        "incomplete-sequential-question-extraction/acknowledgement"
    )

    data = client.get(f"/studio/runs/{run_id}/review/data")
    denied = client.post(soft_url)
    acknowledged = client.post(soft_url, headers=_csrf_headers(client))
    hard = client.post(
        f"/studio/runs/{run_id}/run-diagnostics/parser-blocker/acknowledgement",
        headers=_csrf_headers(client),
    )

    assert [item["code"] for item in data.json()["run_diagnostics"]] == [
        "incomplete-sequential-question-extraction",
        "parser-blocker",
    ]
    assert denied.status_code == 403
    assert acknowledged.status_code == 200
    stored = {item["code"]: item for item in acknowledged.json()["run_diagnostics"]}
    assert stored["incomplete-sequential-question-extraction"]["acknowledged"] is True
    assert acknowledged.json()["blockers"] == ["OCR unavailable"]
    assert hard.status_code == 409


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
    assert "Back to Quiz Builder" in notebook.text


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
    cross_question = client.get(candidate["preview_url"].replace("question-1", "question-2"))
    forbidden = client.post(
        f"/studio/runs/{run_id}/questions/question-1/image-selection",
        json={"image_candidate_id": candidate["candidate_id"]},
    )
    selected = client.post(
        f"/studio/runs/{run_id}/questions/question-1/image-selection",
        json={"image_candidate_id": candidate["candidate_id"]},
        headers=_csrf_headers(client),
    )
    waived = client.put(
        f"/studio/runs/{run_id}/questions/question-1/image-override",
        headers=_csrf_headers(client),
    )
    restored = client.delete(
        f"/studio/runs/{run_id}/questions/question-1/image-override",
        headers=_csrf_headers(client),
    )

    assert preview.status_code == 200
    assert preview.headers["content-type"] == "image/png"
    assert "candidate.png" not in preview.text
    assert cross_question.status_code == 404
    assert forbidden.status_code == 403
    assert selected.status_code == 200
    assert selected.json()["questions"][0]["selected_candidate_id"] == candidate["candidate_id"]
    assert waived.status_code == 200
    assert waived.json()["questions"][0]["image_not_needed"] is True
    assert "required image is unresolved" not in waived.text
    assert restored.status_code == 200
    assert restored.json()["questions"][0]["image_not_needed"] is False
    assert "required image is unresolved" in restored.text


def test_custom_image_can_be_uploaded_to_any_imported_question(tmp_path) -> None:
    client = _client(tmp_path)
    run_id = _direct_review_run(client)
    payload = BytesIO()
    Image.new("RGB", (4, 3), "purple").save(payload, format="PNG")
    files = {"file": ("rash.png", payload.getvalue(), "image/png")}

    forbidden = client.post(f"/studio/runs/{run_id}/questions/question-1/image", files=files)
    uploaded = client.post(
        f"/studio/runs/{run_id}/questions/question-1/image",
        files=files,
        headers=_csrf_headers(client),
    )

    question = uploaded.json()["questions"][0]
    preview = client.get(question["image_preview_url"])
    assert forbidden.status_code == 403
    assert uploaded.status_code == 200
    assert question["image_attached"] is True
    assert question["image_required"] is True
    assert question["selected_candidate_id"] is None
    assert preview.status_code == 200
    assert preview.headers["content-type"] == "image/png"
    stored = client.app.state.practice_review.question(run_id, "question-1")
    assert stored.chosen_image is not None
    assert stored.chosen_image.source_title == "Reviewer upload"


def test_imported_private_question_ids_are_replaced_for_preview_and_public_grading(
    tmp_path,
) -> None:
    client = _client(tmp_path)
    run_id = _direct_review_run(client)
    verified = client.post(
        f"/studio/runs/{run_id}/questions/question-1/verify-answer",
        headers=_csrf_headers(client),
    )
    preview_page = client.get(f"/studio/runs/{run_id}/preview")
    preview_content = client.get(f"/studio/runs/{run_id}/preview/content")
    preview_answer = client.post(
        f"/studio/runs/{run_id}/preview/answer",
        json={"question_id": "q1", "choice_id": "c1"},
    )
    published = client.post(
        f"/studio/runs/{run_id}/publication",
        headers=_csrf_headers(client),
    )
    token = published.json()["token"]
    public_content = client.get(f"/public/quizzes/{token}/content")
    public_page = client.get(f"/public/quizzes/{token}")
    public_answer = client.post(
        f"/public/quizzes/{token}/answer",
        json={"question_id": "q1", "choice_id": "c1"},
        headers=_csrf_headers(client),
    )

    assert verified.status_code == 200
    assert preview_page.status_code == 200
    assert "/public/quizzes/assets/" not in preview_page.text
    version = _player_asset_version()
    for asset in ("reset.css", "tokens.css", "study-hub.css", "public_quiz.css"):
        assert f"/static/{asset}?v={version}" in preview_page.text
        assert client.get(f"/static/{asset}").status_code == 200
    assert f"/static/public_quiz.js?v={version}" in preview_page.text
    assert client.get("/static/public_quiz.js").status_code == 200
    assert preview_content.status_code == 200
    assert preview_content.json()["questions"][0]["id"] == "q1"
    assert "question-1" not in preview_content.text
    assert preview_answer.status_code == 200
    assert preview_answer.json()["correct"] is True
    assert published.status_code == 200
    assert public_content.status_code == 200
    assert public_content.json()["questions"][0]["id"] == "q1"
    assert "question-1" not in public_content.text
    assert public_page.status_code == 200
    assert public_answer.status_code == 200
    assert public_answer.json()["correct"] is True


def test_selected_import_image_preview_and_public_media_keep_private_metadata_hidden(
    tmp_path,
) -> None:
    client = _client(tmp_path)
    run_id = _direct_review_run(client, run_id="secret-run")
    private_question_id = "question-1-private"
    private_source_title = "Professor Secret Packet"
    private_asset_key = "figure-secret"
    client.app.state.practice_review.store(
        run_id,
        (
            QuestionDraft(
                private_question_id,
                "1",
                "Which answer is correct?",
                ("A", "B"),
                0,
                "Because.",
                None,
                (QuestionSourceRef(private_source_title, "segment", "page 1"),),
                AnswerProvenance.GENERATED_BY_AI,
                0.8,
                (),
                True,
                None,
            ),
        ),
    )
    image_path = tmp_path / "secret-candidate.png"
    Image.new("RGB", (3, 2), "red").save(image_path, format="PNG")
    document = ParsedDocument(
        private_source_title,
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
                (private_asset_key,),
            ),
        ),
        (
            ParsedAsset(
                private_asset_key,
                image_path,
                "image/png",
                sha256_file(image_path),
                DocumentLocator("page 1 image", page_number=1),
                3,
                2,
                "embedded-pdf-image",
            ),
        ),
        (),
    )
    client.app.state.studio_repository.save_run_artifact(
        run_id,
        f"parse:{private_source_title}",
        "b" * 64,
        _document_json(document),
    )
    candidate = client.get(f"/studio/runs/{run_id}/review/data").json()["questions"][0][
        "candidates"
    ][0]
    selected = client.post(
        f"/studio/runs/{run_id}/questions/{private_question_id}/image-selection",
        json={"image_candidate_id": candidate["candidate_id"]},
        headers=_csrf_headers(client),
    )
    verified = client.post(
        f"/studio/runs/{run_id}/questions/{private_question_id}/verify-answer",
        headers=_csrf_headers(client),
    )
    preview_content = client.get(f"/studio/runs/{run_id}/preview/content")
    preview_question = preview_content.json()["questions"][0]
    image_key = preview_question["image_url"].rsplit("/", 1)[-1]
    preview_media = client.get(preview_question["image_url"])
    unknown_media = client.get(f"/studio/runs/{run_id}/preview/media/img-unknown")
    bound = client.app.state.studio_repository.import_review_image(run_id, image_key)
    original = bound.path.read_bytes()
    bound.path.write_bytes(b"tampered")
    tampered = client.get(preview_question["image_url"])
    bound.path.write_bytes(original)
    published = client.post(
        f"/studio/runs/{run_id}/publication",
        headers=_csrf_headers(client),
    )
    token = published.json()["token"]
    public_content = client.get(f"/public/quizzes/{token}/content")
    public_question = public_content.json()["questions"][0]
    public_media = client.get(public_question["image_url"])
    native = client.app.state.generation_repository.published_quiz(token)
    private_values = (private_question_id, private_source_title, private_asset_key, str(bound.path))

    assert selected.status_code == 200
    assert verified.status_code == 200
    assert preview_content.status_code == 200
    assert image_key.startswith("img-") and len(image_key) <= 64
    assert all(value not in image_key for value in private_values[:3])
    assert preview_question["image_alt"] == "Question image"
    assert preview_question["image_width"] == 3
    assert preview_question["image_height"] == 2
    assert preview_media.status_code == 200
    assert unknown_media.status_code == 404
    assert tampered.status_code == 404
    assert published.status_code == 200
    assert public_content.status_code == 200
    assert public_question["image_alt"] == "Question image"
    assert public_media.status_code == 200
    assert native is not None
    for value in private_values:
        assert value not in preview_content.text
        assert value not in public_content.text
        assert value not in serialize_native_quiz(native.quiz)
