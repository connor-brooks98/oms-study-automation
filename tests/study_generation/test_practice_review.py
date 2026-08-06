from pathlib import Path

import pytest

from oms_hub.db import Database
from oms_hub.models import StudioRunModel
from oms_hub.study_generation.practice_domain import (
    AnswerProvenance,
    QuestionDraft,
    QuestionSourceRef,
)
from oms_hub.study_generation.practice_review import PracticeReviewService
from oms_hub.study_generation.repository import GenerationRepository
from oms_hub.study_generation.studio_repository import StudioRepository


def _service(tmp_path: Path) -> PracticeReviewService:
    database = Database(f"sqlite:///{tmp_path / 'hub.db'}")
    database.migrate()
    with database.session() as session:
        session.add(
            StudioRunModel(
                id="run-1",
                subject="Neuro",
                subject_key="neuro",
                exam_number=1,
                destination_subject="Neuro",
                destination_subject_key="neuro",
                destination_exam_number=1,
                label="Imported practice",
                label_key="imported practice",
                prompt="",
                workflow_kind="direct_import",
                state="awaiting_review",
                stage="review",
            )
        )
    return PracticeReviewService(StudioRepository(database))


def _draft(question_id: str, *, generated: bool) -> QuestionDraft:
    return QuestionDraft(
        question_id,
        question_id,
        "What is correct?",
        ("A", "B"),
        0,
        "Because.",
        None,
        (QuestionSourceRef("source", "segment", "page 1"),),
        AnswerProvenance.GENERATED_BY_AI if generated else AnswerProvenance.PROVIDED_BY_SOURCE,
        0.8,
        (),
        generated,
        None,
    )


def test_generated_answer_blocks_until_same_question_is_verified(tmp_path: Path) -> None:
    service = _service(tmp_path)
    run_id = "run-1"
    service.store(run_id, (_draft("q1", generated=True), _draft("q2", generated=False)))

    assert service.blockers(run_id) == ("q1: AI-generated answer requires verification",)
    with pytest.raises(ValueError, match="requires verification"):
        service.to_native_quiz(run_id)
    service.verify_generated_answer(run_id, "q1")
    assert service.blockers(run_id) == ()


def test_editing_answer_clears_verification_and_marks_manual(tmp_path: Path) -> None:
    service = _service(tmp_path)
    run_id = "run-1"
    service.store(run_id, (_draft("q1", generated=True),))
    service.verify_generated_answer(run_id, "q1")

    updated = service.update_question(run_id, "q1", {"correct_index": 1})

    assert updated.answer_provenance is AnswerProvenance.MANUALLY_CORRECTED
    assert updated.verification_required is True
    assert updated.verified_at is None


def test_verifying_one_answer_does_not_verify_another_and_later_edit_reopens_it(
    tmp_path: Path,
) -> None:
    service = _service(tmp_path)
    service.store("run-1", (_draft("q1", generated=True), _draft("q2", generated=True)))
    service.verify_generated_answer("run-1", "q1")

    assert service.question("run-1", "q1").verified_at is not None
    assert service.question("run-1", "q2").verified_at is None
    service.update_question("run-1", "q1", {"choices": ["A", "C"]})
    assert service.question("run-1", "q1").verified_at is None


def test_direct_publication_uses_current_review_state_in_the_same_gate(tmp_path: Path) -> None:
    service = _service(tmp_path)
    service.store("run-1", (_draft("q1", generated=True),))
    service.verify_generated_answer("run-1", "q1")
    assert service.blockers("run-1") == ()

    # A stale client may have observed the empty blocker list; the server sees
    # this later edit and must reject publication without creating a quiz row.
    service.update_question("run-1", "q1", {"correct_index": 1})
    publisher = GenerationRepository(service.repository.database, practice_review=service)
    with pytest.raises(ValueError, match="requires verification"):
        publisher.publish_reviewed_studio_quiz("run-1")
    assert publisher.published_quizzes() == ()
    assert service.repository.get_run("run-1").published_token is None


def test_blocker_free_direct_review_publishes_without_private_review_fields(tmp_path: Path) -> None:
    service = _service(tmp_path)
    service.store("run-1", (_draft("q1", generated=True),))
    service.verify_generated_answer("run-1", "q1")
    publisher = GenerationRepository(service.repository.database, practice_review=service)

    published = publisher.publish_reviewed_studio_quiz("run-1")

    assert publisher.published_quiz(published.token) is not None
    with service.repository.database.session() as session:
        payload = session.get(StudioRunModel, "run-1")
        assert payload is not None
        assert payload.published_token == published.token


@pytest.mark.parametrize(
    "update",
    [
        {"choices": ["A"]},
        {"choices": ["A", "a"]},
        {"correct_index": 4},
        {"stem": " "},
    ],
)
def test_invalid_question_edits_are_rejected(tmp_path: Path, update: dict[str, object]) -> None:
    service = _service(tmp_path)
    service.store("run-1", (_draft("q1", generated=False),))

    with pytest.raises(ValueError):
        service.update_question("run-1", "q1", update)
