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


class LectureStepName(StrEnum):
    OUTLOOK_MATCHED = "outlook_matched"
    CANVAS_PPTX_FOUND = "canvas_pptx_found"
    PPTX_DOWNLOADED = "pptx_downloaded"
    PDF_FILED = "pdf_filed"
    GOODNOTES_DELIVERED = "goodnotes_delivered"
    PANOPTO_RECORDING_FOUND = "panopto_recording_found"
    TRANSCRIPT_DOWNLOADED = "transcript_downloaded"
    TRANSCRIPT_CLEANED = "transcript_cleaned"
    TRANSCRIPT_FILED = "transcript_filed"
    NOTEBOOK_EXISTS = "notebook_exists"
    PPTX_UPLOADED = "pptx_uploaded"
    TRANSCRIPT_UPLOADED = "transcript_uploaded"
    PRACTICE_QUESTIONS_UPLOADED = "practice_questions_uploaded"
    SOURCES_SELECTED = "sources_selected"
    SUMMARY_GENERATED = "summary_generated"
    SUMMARY_FILED = "summary_filed"
    QUIZ_PROMPT_COMPLETED = "quiz_prompt_completed"
    GEMINI_QUIZ_GENERATED = "gemini_quiz_generated"
    GEMINI_QUIZ_VERIFIED = "gemini_quiz_verified"
    SHARE_LINK_CAPTURED = "share_link_captured"
    GOOGLE_DOC_UPDATED = "google_doc_updated"


@dataclass(frozen=True, slots=True)
class LectureKey:
    subject: str
    exam_number: int
    lecture_number: int
    topic: str

