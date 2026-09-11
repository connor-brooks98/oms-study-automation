import json
from io import BytesIO

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from PIL import Image
from sqlalchemy import select

from oms_hub.db import Database
from oms_hub.models import BankReviewQuestionModel, PublishedQuizModel
from oms_hub.question_bank.imports import preview_import
from oms_hub.question_bank.native_review import stage_native_review
from oms_hub.question_bank.repository import BankRepository
from oms_hub.study_generation.practice_review import PracticeReviewService
from oms_hub.study_generation.quiz_images import StudioQuizImageService
from oms_hub.study_generation.repository import GenerationRepository
from oms_hub.study_generation.studio_repository import StudioRepository
from oms_hub.web.public_quiz_routes import router as public_quiz_router


def body():
    return {
        "stem": "Choose A.",
        "choices": ["A", "B"],
        "correct_index": 0,
        "rationale": "A is specified.",
    }


def data(rows=None):
    return {
        "schema_version": 1,
        "source": "user",
        "product": "fixture",
        "export_id": "batch",
        "provenance": {"kind": "authorized_question_export", "description": "Synthetic"},
        "rows": rows or [{"question_id": "00123", "question": body()}],
    }


@pytest.fixture
def setup(tmp_path):
    with Database(f"sqlite:///{tmp_path / 'bank.db'}") as database:
        database.migrate()
        bank = BankRepository(database.session)
        studio = StudioRepository(database)
        review = PracticeReviewService(
            studio, image_service=StudioQuizImageService(studio, tmp_path / "media")
        )
        yield bank, studio, review, GenerationRepository(database, practice_review=review)


def imported(bank, payload):
    preview = preview_import(json.dumps(payload).encode())
    return bank.commit_import(preview, learner_id="test", expected_digest=preview.digest).import_id


def stage(bank, studio, import_id, **changes):
    return stage_native_review(
        bank,
        studio,
        **(
            dict(
                import_id=import_id,
                learner_id="test",
                subject="Fixture",
                exam_number=1,
                label="Review",
            )
            | changes
        ),
    )


def test_stage_retains_identity_and_existing_publication_gates(setup):
    bank, studio, review, generation = setup
    image_body = body() | {
        "image_ref": {
            "key": "figure-1",
            "source_title": "Fixture",
            "locator": "page 1",
            "description": "Synthetic image",
        }
    }
    import_id = imported(
        bank,
        data(
            [
                {"question_id": "00123", "question": body()},
                {"question_id": "00124", "question": image_body},
            ]
        ),
    )
    ref = stage(bank, studio, import_id)
    assert stage(bank, studio, import_id) == ref
    assert ref.review_url == f"/studio/runs/{ref.run_id}/review"
    assert ref.original_keys[0][1].question_id == "00123"
    assert studio.get_run(ref.run_id).state.value == "awaiting_review"
    assert studio.claim_next_run() is None
    assert review.question(ref.run_id, "q2").draft.verification_required
    with studio.database.session() as session:
        assert session.scalar(select(PublishedQuizModel)) is None
        assert len(session.scalars(select(BankReviewQuestionModel)).all()) == 2
    with pytest.raises(ValueError):
        generation.publish_reviewed_studio_quiz(ref.run_id)
    for qid in ("q1", "q2"):
        review.verify_generated_answer(ref.run_id, qid)
    with pytest.raises(ValueError):
        generation.publish_reviewed_studio_quiz(ref.run_id)
    image = BytesIO()
    Image.new("RGB", (30, 30), "white").save(image, format="PNG")
    review.upload_image(ref.run_id, "q2", "fixture.png", image.getvalue())
    published = generation.publish_reviewed_studio_quiz(ref.run_id)
    assert generation.published_quiz(published.token) is not None
    app = FastAPI()
    app.state.generation_repository = generation
    app.include_router(public_quiz_router)
    with TestClient(app) as client:
        assert client.get(f"/public/quizzes/{published.token}").status_code == 200
        content = client.get(f"/public/quizzes/{published.token}/content").json()
        assert len(content["questions"]) == 2
        assert client.get(content["questions"][1]["image_url"]).status_code == 200
    with pytest.raises(ValueError):
        stage(bank, studio, import_id, learner_id="other")


def test_unknown_answer_and_rationale_remain_blocked_but_editable(setup):
    bank, studio, review, generation = setup
    import_id = imported(
        bank,
        data([{"question_id": "q", "question": body() | {"correct_index": -1, "rationale": ""}}]),
    )
    ref = stage(bank, studio, import_id)
    draft = review.question(ref.run_id, "q1").draft
    assert (
        draft.correct_index is None and draft.rationale is None and draft.answer_provenance is None
    )
    with pytest.raises(ValueError):
        generation.publish_reviewed_studio_quiz(ref.run_id)
    assert "q1: answer is missing" in review.blockers(ref.run_id)


def test_selection_batches_and_unparseable_rows_are_not_silently_staged(setup):
    bank, studio, _, _ = setup
    import_id = imported(
        bank, data([{"question_id": str(i), "question": body()} for i in range(501)])
    )
    with pytest.raises(ValueError, match="500"):
        stage(bank, studio, import_id)
    first = stage(bank, studio, import_id, row_numbers=(2, 1))
    assert [k.question_id for _, k in first.original_keys] == ["0", "1"]
    assert stage(bank, studio, import_id, row_numbers=(1, 2)) == first
    second = stage(bank, studio, import_id, row_numbers=(501,), label="Second")
    assert second.run_id != first.run_id
    with pytest.raises(ValueError):
        stage(bank, studio, import_id, row_numbers=(1, 1))
    with pytest.raises(ValueError):
        stage(bank, studio, import_id, row_numbers=(999,))


def test_results_only_and_unsupported_bodies_stay_out_of_review(setup):
    bank, studio, _, _ = setup
    import_id = imported(bank, data([{"question_id": "q"}]))
    with pytest.raises(ValueError, match="no authorized question content"):
        stage(bank, studio, import_id)
    payload = data([{"question_id": "q", "question": {"stem": "broken"}}])
    payload["export_id"] = "broken"
    import_id = imported(bank, payload)
    with pytest.raises(ValueError, match="reviewable"):
        stage(bank, studio, import_id)


def test_normalized_acceptance_reconciles_records_replay_and_publication(setup):
    bank, studio, review, generation = setup
    rows = [
        {"question_id": "00123", "attempt_id": "a", "result": "correct", "question": body()},
        {"question_id": "002", "attempt_id": "b", "result": "incorrect", "user_note": "Revisit"},
        {"question_id": "003", "attempt_id": "c", "result": "omitted"},
        {"question_id": "004", "attempt_id": "d", "result": "unknown"},
        {"question_id": "005", "user_note": "QID only — no attempt", "tags": ["Original::Tag"]},
        {"question_id": "006", "question": body() | {"correct_index": -1, "rationale": ""}},
        {"question_id": "007", "question": body() | {"image_ref": {
            "key": "figure", "source_title": "Fixture", "locator": "page 1",
            "description": "Synthetic image",
        }}},
    ]
    rows.append(dict(rows[0]))
    payload = data(rows) | {"source": "uworld"}
    preview = preview_import(json.dumps(payload).encode())
    assert preview.ready_question_rows == (1,)
    assert [(i.row, i.code) for i in preview.issues] == [
        (6, "invalid_question"), (7, "missing_image"), (8, "duplicate_row")
    ]
    receipt = bank.commit_import(preview, learner_id="test", expected_digest=preview.digest)
    original = bank.review_rows(import_id=receipt.import_id, learner_id="test")
    assert len(rows) == len(original) + len(receipt.conflicts) == 8
    assert original == preview.envelope.rows
    assert (receipt.inserted_questions, receipt.inserted_attempts, receipt.duplicate_rows) == (
        7, 4, 1
    )
    facts = bank.iter_attempts(learner_id="test")
    assert [f.result for f in facts] == ["correct", "incorrect", "omitted", "unknown"]
    assert all(f.occurred_at is None and f.elapsed_ms is None for f in facts)
    assert bank.commit_import(preview, learner_id="test", expected_digest=preview.digest) == receipt
    assert bank.iter_attempts(learner_id="test") == facts
    assert len(bank.list_ready_questions(learner_id="test")) == 1

    other = data([rows[0]]) | {"source": "truelearn"}
    imported(bank, other)
    facts = bank.iter_attempts(learner_id="test")
    assert len(facts) == 5
    assert [(f.key.source, f.key.question_id) for f in (facts[0], facts[-1])] == [
        ("uworld", "00123"), ("truelearn", "00123")
    ]
    conflict = payload | {"export_id": "conflict", "rows": [
        {"question_id": "new", "attempt_id": "new", "result": "correct"},
        rows[0] | {"result": "incorrect"},
    ]}
    checked = preview_import(json.dumps(conflict).encode())
    rejected = bank.commit_import(checked, learner_id="test", expected_digest=checked.digest)
    assert rejected.conflicts and rejected.conflicts[0].row == 2
    assert rejected.inserted_questions == rejected.inserted_attempts == 0
    with pytest.raises(ValueError):
        bank.get_import(import_id=rejected.import_id, learner_id="test")
    assert bank.iter_attempts(learner_id="test") == facts

    ref = stage(bank, studio, receipt.import_id)
    assert [key.question_id for _, key in ref.original_keys] == ["00123", "006", "007"]
    assert "q2: answer is missing" in review.blockers(ref.run_id)
    with pytest.raises(ValueError):
        generation.publish_reviewed_studio_quiz(ref.run_id)
    with studio.database.session() as session:
        assert session.scalar(select(PublishedQuizModel)) is None
