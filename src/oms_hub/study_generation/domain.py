from dataclasses import dataclass
from enum import StrEnum


class GenerationKind(StrEnum):
    OUTLINE = "outline"
    QUIZ = "quiz"


class GenerationState(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    PAUSED = "paused"
    COMPLETE = "complete"
    FAILED = "failed"


class GenerationStage(StrEnum):
    VALIDATE = "validate"
    NOTEBOOK = "notebook"
    SOURCES = "sources"
    NOTEBOOK_PROMPT = "notebook_prompt"
    PDF = "pdf"
    GEMINI = "gemini"
    SHARE = "share"
    DOCS = "docs"
    COMPLETE = "complete"


class SourceKind(StrEnum):
    LECTURE_PDF = "lecture_pdf"
    CLEANED_TRANSCRIPT = "cleaned_transcript"


class PromptKind(StrEnum):
    OUTLINE = "outline"
    QUIZ = "quiz"


@dataclass(frozen=True, slots=True)
class GenerationJob:
    id: str
    lecture_id: int
    kind: GenerationKind
    state: GenerationState
    stage: GenerationStage
    attempts: int
    error: str | None = None
    notebook_id: str | None = None
    pdf_source_id: str | None = None
    transcript_source_id: str | None = None
    notebook_answer: str | None = None
    gemini_quiz_id: str | None = None
    quiz_url: str | None = None
