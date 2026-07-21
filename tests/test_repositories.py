from oms_hub.domain import LectureStepName, StepStatus
from oms_hub.repositories import CatalogRepository, LectureInput


def test_upsert_lecture_initializes_every_checklist_step(database):
    repo = CatalogRepository(database)
    lecture_id = repo.upsert_lecture(
        LectureInput(
            subject="Heme/Lymph",
            exam_number=1,
            lecture_number=4,
            topic="Anemia I",
            lecturer="Jun Wang, MD, PhD",
            exam_date="2026-07-03",
        )
    )

    lecture = repo.get_lecture(lecture_id)

    assert lecture is not None
    assert lecture.subject == "Heme/Lymph"
    assert len(lecture.steps) == len(LectureStepName)
    assert {step.status for step in lecture.steps} == {StepStatus.WAITING.value}


def test_upsert_is_idempotent_for_subject_exam_and_lecture_number(database):
    repo = CatalogRepository(database)
    item = LectureInput(
        "Neuro",
        1,
        1,
        "General CNS Pathology",
        "Teresa Campbell, MD",
        "2026-07-03",
    )

    first = repo.upsert_lecture(item)
    second = repo.upsert_lecture(item)

    assert first == second
    assert len(repo.list_lectures()) == 1
