from dataclasses import replace

from oms_hub.canvas.classifier import classify_attachment
from oms_hub.canvas.domain import CanvasAttachment, SourceKind


def attachment(filename: str, **changes: object) -> CanvasAttachment:
    base = CanvasAttachment(
        "751", "Hematology & Lymph", "LEC-DOSYS-751", "10", "Exam 1 Lectures",
        "20", "Lecture 4: Anemia I", "Page", "/courses/751/pages/anemia-i",
        "Lecture 4: Anemia I", "30", filename, "application/octet-stream", 1234,
        "2026-07-21T12:00:00Z", "https://lmunet.instructure.com/files/30/download", "",
    )
    return replace(base, **changes)


def test_lecture_powerpoint_wins_on_lecture_page() -> None:
    result = classify_attachment(attachment("2026 Student Anemia.pptx"))
    assert result.kind is SourceKind.LECTURE
    assert result.confidence >= 0.90


def test_duplicate_lecture_pdf_is_ignored() -> None:
    result = classify_attachment(attachment("2026 Student Anemia.pdf"))
    assert result.kind is SourceKind.IGNORE
    assert "lecture PDF" in result.reason


def test_page_wide_pq_text_does_not_reclassify_lecture_powerpoint() -> None:
    result = classify_attachment(
        attachment(
            "2026 Student Anemia.pptx",
            evidence_text="2026 Student Anemia.pdf Practice questions for anemia.docx",
        )
    )
    assert result.kind is SourceKind.LECTURE


def test_page_wide_pq_text_does_not_collect_duplicate_lecture_pdf() -> None:
    result = classify_attachment(
        attachment(
            "2026 Student Anemia.pdf",
            evidence_text="Practice questions for anemia.docx",
        )
    )
    assert result.kind is SourceKind.IGNORE


def test_positive_pq_docx_is_collected() -> None:
    result = classify_attachment(attachment("Practice questions for anemia.docx"))
    assert result.kind is SourceKind.PRACTICE_QUESTIONS


def test_url_encoded_pq_filename_is_collected() -> None:
    result = classify_attachment(
        attachment("2026+Practice+questions+for+general+CNS+pathology.docx")
    )
    assert result.kind is SourceKind.PRACTICE_QUESTIONS


def test_negative_reading_overrides_weak_question_word() -> None:
    result = classify_attachment(attachment("Reading assignment questions.pdf"))
    assert result.kind is SourceKind.IGNORE


def test_macro_enabled_office_file_requires_review() -> None:
    assert classify_attachment(attachment("Anemia lecture.pptm")).kind is SourceKind.REVIEW
