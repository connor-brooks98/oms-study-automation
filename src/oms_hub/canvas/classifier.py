from pathlib import Path

from oms_hub.canvas.domain import CanvasAttachment, Classification, SourceKind

PQ_TERMS = (
    "practice question",
    "practice qs",
    "question set",
    "review question",
    "case questions",
)
NEGATIVE_TERMS = (
    "reading",
    "objective",
    "rubric",
    "article",
    "lab instruction",
    "expectation",
    "assignment",
    "lockdown browser",
)
AUTO_PQ_SUFFIXES = {".pdf", ".doc", ".docx", ".ppt", ".pptx"}


def classify_attachment(value: CanvasAttachment) -> Classification:
    suffix = Path(value.filename).suffix.casefold()
    context = " ".join(
        (
            value.module_title,
            value.item_title,
            value.page_title,
            value.filename,
            value.evidence_text,
        )
    ).casefold()
    filename = value.filename.casefold()
    if suffix in {".pptm", ".docm", ".xlsm"}:
        return Classification(SourceKind.REVIEW, 1.0, "macro-enabled Office file")
    filename_has_pq = any(term in filename for term in PQ_TERMS)
    has_pq = any(term in context for term in PQ_TERMS)
    has_negative = any(term in context for term in NEGATIVE_TERMS)
    if has_negative and not filename_has_pq:
        return Classification(SourceKind.IGNORE, 0.95, "negative content category")
    if has_pq:
        if suffix in AUTO_PQ_SUFFIXES:
            return Classification(
                SourceKind.PRACTICE_QUESTIONS,
                0.95,
                "positive practice-question evidence",
            )
        return Classification(
            SourceKind.REVIEW,
            0.85,
            "practice questions use an unsupported file type",
        )
    lecture_context = "lecture" in f"{value.item_title} {value.page_title}".casefold()
    if lecture_context and suffix in {".ppt", ".pptx"}:
        return Classification(SourceKind.LECTURE, 0.95, "PowerPoint on a lecture page")
    if lecture_context and suffix == ".pdf":
        return Classification(SourceKind.IGNORE, 0.99, "duplicate professor lecture PDF")
    return Classification(
        SourceKind.IGNORE,
        0.70,
        "not a lecture PowerPoint or professor practice questions",
    )
