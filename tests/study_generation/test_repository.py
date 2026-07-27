from datetime import UTC, datetime

from oms_hub.db import Database
from oms_hub.repositories import CatalogRepository, LectureInput
from oms_hub.study_generation.domain import (
    GenerationKind,
    GenerationStage,
    GenerationState,
)
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
