import json
from datetime import UTC, datetime

from oms_hub.db import Database
from oms_hub.repositories import CatalogRepository, LectureInput
from oms_hub.study_generation.domain import (
    GenerationKind,
    GenerationStage,
    GenerationState,
    SourceKind,
)
from oms_hub.study_generation.native_quiz import parse_native_quiz
from oms_hub.study_generation.repository import GenerationRepository


def prepared_repository(tmp_path):
    database = Database(f"sqlite:///{tmp_path / 'hub.db'}")
    database.migrate()
    lecture_id = CatalogRepository(database).upsert_lecture(
        LectureInput("Neuro", 1, 1, "Seizures", "Dr Test", None)
    )
    return GenerationRepository(database), lecture_id


def test_queue_reuses_active_job_but_separates_generation_kinds(tmp_path):
    repository, lecture_id = prepared_repository(tmp_path)

    first = repository.queue(lecture_id, GenerationKind.OUTLINE)
    second = repository.queue(lecture_id, GenerationKind.OUTLINE)
    quiz = repository.queue(lecture_id, GenerationKind.QUIZ)

    assert second.id == first.id
    assert quiz.id != first.id
    assert first.state is GenerationState.QUEUED
    assert first.stage is GenerationStage.VALIDATE


def test_claim_and_recovery_preserve_recorded_stage(tmp_path):
    repository, lecture_id = prepared_repository(tmp_path)
    queued = repository.queue(lecture_id, GenerationKind.QUIZ)

    claimed = repository.claim_next(datetime.now(UTC))
    assert claimed is not None
    assert claimed.id == queued.id
    repository.advance(claimed.id, GenerationStage.GEMINI)

    assert repository.recover_interrupted() == 1
    recovered = repository.get(claimed.id)
    assert recovered.state is GenerationState.QUEUED
    assert recovered.stage is GenerationStage.GEMINI


def _quiz(title="Seizures"):
    return parse_native_quiz(
        json.dumps(
            {
                "title": title,
                "questions": [
                    {
                        "stem": "Which choice is correct?",
                        "choices": ["First", "Second"],
                        "correct_index": 0,
                        "rationale": "The first choice is correct.",
                    }
                ],
            }
        )
    )


def test_publish_keeps_token_and_increments_version_for_new_job(tmp_path):
    repository, lecture_id = prepared_repository(tmp_path)
    try:
        first_job = repository.queue(lecture_id, GenerationKind.QUIZ)

        first = repository.publish_quiz(lecture_id, first_job.id, _quiz())
        retried = repository.publish_quiz(lecture_id, first_job.id, _quiz())
        repository.complete(first_job.id)
        second_job = repository.queue(lecture_id, GenerationKind.QUIZ)
        regenerated = repository.publish_quiz(
            lecture_id,
            second_job.id,
            _quiz("Seizures Review"),
        )

        assert len(first.token) == 64
        assert retried.token == first.token
        assert retried.version == 1
        assert regenerated.token == first.token
        assert regenerated.version == 2
        assert regenerated.title == "Seizures Review"
        assert repository.published_quiz(first.token) == regenerated
    finally:
        repository.database.engine.dispose()


def test_unknown_public_quiz_token_returns_none(tmp_path):
    repository, _ = prepared_repository(tmp_path)

    try:
        assert repository.published_quiz("f" * 64) is None
    finally:
        repository.database.engine.dispose()


def test_binding_new_revision_supersedes_prior_ready_source(tmp_path):
    repository, lecture_id = prepared_repository(tmp_path)
    notebook = repository.save_notebook_mapping(
        "Neuro",
        "neuro",
        1,
        "nb-1",
        "Neuro · Exam 1",
    )

    first = repository.bind_source(
        notebook.id,
        lecture_id,
        revision_id=10,
        source_kind=SourceKind.LECTURE_PDF,
        source_sha256="a" * 64,
        remote_source_id="remote-old",
        display_title="Lecture 01 - Seizures",
    )
    second = repository.bind_source(
        notebook.id,
        lecture_id,
        revision_id=11,
        source_kind=SourceKind.LECTURE_PDF,
        source_sha256="b" * 64,
        remote_source_id="remote-new",
        display_title="Lecture 01 - Seizures",
    )

    assert first.remote_source_id == "remote-old"
    assert repository.source_binding(
        notebook.id,
        lecture_id,
        SourceKind.LECTURE_PDF,
    ) == second


def test_notebook_mapping_is_upserted_by_course_and_exam(tmp_path):
    repository, _ = prepared_repository(tmp_path)

    first = repository.save_notebook_mapping(
        "Neuro",
        "neuro",
        1,
        "nb-old",
        "Neuro · Exam 1",
    )
    second = repository.save_notebook_mapping(
        "Neuro",
        "neuro",
        1,
        "nb-new",
        "Neuro · Exam 1",
    )

    assert second.id == first.id
    assert second.remote_notebook_id == "nb-new"
    assert repository.notebook_mapping("neuro", 1) == second
