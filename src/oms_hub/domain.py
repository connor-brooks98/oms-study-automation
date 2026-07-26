from dataclasses import dataclass
from enum import StrEnum


class StepStatus(StrEnum):
    WAITING = "waiting"
    QUEUED = "queued"
    RUNNING = "running"
    NEEDS_REVIEW = "needs_review"
    COMPLETE = "complete"
    FAILED = "failed"
    SKIPPED = "skipped"


class V2StepName(StrEnum):
    SLIDES_RECEIVED = "slides_received"
    SLIDES_MATCHED = "slides_matched"
    SLIDES_VALIDATED = "slides_validated"
    PDF_CONVERTED = "pdf_converted"
    SLIDES_FILED = "slides_filed"
    ICLOUD_PDF_STAGED = "icloud_pdf_staged"
    TRANSCRIPT_RECEIVED = "transcript_received"
    TRANSCRIPT_MATCHED = "transcript_matched"
    TRANSCRIPT_VALIDATED = "transcript_validated"
    TRANSCRIPT_CLEANED = "transcript_cleaned"
    TRANSCRIPT_FILED = "transcript_filed"
    NOTEBOOK_CREATED = "notebook_created"
    SOURCES_UPLOADED = "sources_uploaded"
    SUMMARY_FILED = "summary_filed"
    QUIZ_PUBLISHED = "quiz_published"
    ANKI_SYNCED = "anki_synced"

    @classmethod
    def first_release(cls) -> tuple["V2StepName", ...]:
        return tuple(cls)[:11]


@dataclass(frozen=True, slots=True)
class LectureKey:
    subject: str
    exam_number: int
    lecture_number: int
    topic: str
