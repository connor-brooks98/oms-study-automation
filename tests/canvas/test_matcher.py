from dataclasses import replace
from types import SimpleNamespace

from oms_hub.canvas.domain import CanvasAttachment
from oms_hub.canvas.matcher import match_attachment


def attachment(filename: str, **changes: object) -> CanvasAttachment:
    base = CanvasAttachment(
        "751", "Hematology & Lymph", "LEC-DOSYS-751", "10", "Exam 1 Lectures",
        "20", "Lecture 4: Anemia I", "Page", "/courses/751/pages/anemia-i",
        "Lecture 4: Anemia I", "30", filename, "application/octet-stream", 1234,
        "2026-07-21T12:00:00Z", "https://lmunet.instructure.com/files/30/download", "",
    )
    return replace(base, **changes)


def lecture(id: int, subject: str, exam: int, number: int, topic: str) -> SimpleNamespace:
    return SimpleNamespace(
        id=id, subject=subject, exam_number=exam, lecture_number=number, topic=topic
    )


def test_standard_match_requires_subject_exam_and_lecture_number() -> None:
    result = match_attachment(
        attachment("anemia.pptx"),
        "Heme/Lymph",
        [lecture(7, "Heme/Lymph", 1, 4, "Anemia I")],
    )
    assert (result.lecture_id, result.exam_number) == (7, 1)
    assert result.confidence >= 0.95


def test_epc_unique_topic_match_derives_exam_and_number() -> None:
    value = attachment(
        "Giving the Assessment and Plan.pptx",
        module_title="Giving the Assessment and Plan",
        item_title="Giving the Assessment and Plan Lecture",
        page_title="Giving the Assessment and Plan Lecture",
    )
    catalog = [
        lecture(8, "EPC", 1, 3, "The Difficult Patient"),
        lecture(9, "EPC", 1, 4, "Giving the Assessment and Plan"),
    ]
    result = match_attachment(value, "EPC", catalog)
    assert (result.lecture_id, result.exam_number) == (9, 1)
    assert result.confidence >= 0.88


def test_epc_competing_topic_matches_require_review() -> None:
    value = attachment(
        "Communication.pptx",
        module_title="Communication",
        item_title="Communication Lecture",
    )
    catalog = [
        lecture(10, "EPC", 1, 1, "Communication I"),
        lecture(11, "EPC", 2, 7, "Communication II"),
    ]
    assert match_attachment(value, "EPC", catalog).lecture_id is None
