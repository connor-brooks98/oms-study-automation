import pytest

from oms_hub.checklist import ChecklistService, InvalidTransition
from oms_hub.domain import LectureStepName, StepStatus
from oms_hub.repositories import CatalogRepository, LectureInput


def make_lecture(repository: CatalogRepository) -> int:
    return repository.upsert_lecture(
        LectureInput(
            "Neuro",
            1,
            1,
            "General CNS Pathology",
            "T. Campbell",
            "2026-07-03",
        )
    )


def test_pdf_cannot_complete_before_pptx_is_downloaded(database):
    repository = CatalogRepository(database)
    lecture_id = make_lecture(repository)
    service = ChecklistService(repository)

    with pytest.raises(InvalidTransition, match="pptx_downloaded"):
        service.transition(
            lecture_id,
            LectureStepName.PDF_FILED,
            StepStatus.COMPLETE,
        )


def test_review_can_be_approved_back_to_queue(database):
    repository = CatalogRepository(database)
    lecture_id = make_lecture(repository)
    service = ChecklistService(repository)
    service.transition(
        lecture_id,
        LectureStepName.OUTLOOK_MATCHED,
        StepStatus.NEEDS_REVIEW,
        "ambiguous",
    )

    service.transition(
        lecture_id,
        LectureStepName.OUTLOOK_MATCHED,
        StepStatus.QUEUED,
        "approved by user",
    )

    lecture = repository.get_lecture(lecture_id)
    assert lecture is not None
    step = next(
        item for item in lecture.steps if item.name == "outlook_matched"
    )
    assert step.status == "queued"
    assert step.detail == "approved by user"


def test_next_actionable_steps_excludes_blocked_steps(database):
    repository = CatalogRepository(database)
    lecture_id = make_lecture(repository)
    service = ChecklistService(repository)

    actionable = service.next_actionable_steps(lecture_id)

    assert LectureStepName.CANVAS_PPTX_FOUND in actionable
    assert LectureStepName.PPTX_DOWNLOADED not in actionable
    assert LectureStepName.PDF_FILED not in actionable


def test_skipped_prerequisite_unblocks_dependent_step(database):
    repository = CatalogRepository(database)
    lecture_id = make_lecture(repository)
    service = ChecklistService(repository)
    service.transition(
        lecture_id,
        LectureStepName.PPTX_DOWNLOADED,
        StepStatus.SKIPPED,
        "not provided",
    )

    service.transition(
        lecture_id,
        LectureStepName.PDF_FILED,
        StepStatus.COMPLETE,
    )

    lecture = repository.get_lecture(lecture_id)
    assert lecture is not None
    step = next(item for item in lecture.steps if item.name == "pdf_filed")
    assert step.status == "complete"
