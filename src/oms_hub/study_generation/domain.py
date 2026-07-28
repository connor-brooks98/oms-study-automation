from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path


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
    QUIZ_VALIDATE = "quiz_validate"
    PUBLISH = "publish"
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
    prompt_path: str | None = None
    prompt_sha256: str | None = None
    pdf_revision_id: int | None = None
    transcript_revision_id: int | None = None
    notebook_id: str | None = None
    pdf_source_id: str | None = None
    transcript_source_id: str | None = None
    notebook_answer: str | None = None
    gemini_quiz_id: str | None = None
    quiz_url: str | None = None


@dataclass(frozen=True, slots=True)
class PromptSnapshot:
    path: Path
    content: str
    sha256: str
    modified_at: str


class SourceIsolationError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class NotebookRef:
    id: str
    title: str


@dataclass(frozen=True, slots=True)
class RevisionSource:
    lecture_id: int
    revision_id: int
    path: Path
    sha256: str
    kind: SourceKind


@dataclass(frozen=True, slots=True)
class RemoteSource:
    remote_id: str
    lecture_id: int
    revision_id: int
    sha256: str
    kind: SourceKind
    ready: bool


@dataclass(frozen=True, slots=True)
class LectureSourceSet:
    lecture_id: int
    pdf: RemoteSource
    transcript: RemoteSource

    def __post_init__(self) -> None:
        if (
            self.pdf.kind is not SourceKind.LECTURE_PDF
            or self.transcript.kind is not SourceKind.CLEANED_TRANSCRIPT
        ):
            raise SourceIsolationError("lecture source kinds are invalid")
        if (
            self.pdf.lecture_id != self.lecture_id
            or self.transcript.lecture_id != self.lecture_id
        ):
            raise SourceIsolationError("lecture sources belong to different lectures")
        if not self.pdf.ready or not self.transcript.ready:
            raise SourceIsolationError("lecture sources are not ready")
        if self.pdf.remote_id == self.transcript.remote_id:
            raise SourceIsolationError("lecture sources must be distinct")

    @property
    def remote_ids(self) -> list[str]:
        return [self.pdf.remote_id, self.transcript.remote_id]


@dataclass(frozen=True, slots=True)
class NotebookAnswer:
    text: str


@dataclass(frozen=True, slots=True)
class OutlineRecord:
    id: int
    lecture_id: int
    job_id: str
    path: Path
    sha256: str
    current: bool


@dataclass(frozen=True, slots=True)
class QuizRecord:
    id: int
    lecture_id: int
    job_id: str
    url: str
    docs_synced: bool
    current: bool


@dataclass(frozen=True, slots=True)
class QuizChoice:
    id: str
    text: str


@dataclass(frozen=True, slots=True)
class QuizQuestion:
    id: str
    stem: str
    choices: tuple[QuizChoice, ...]
    correct_choice_id: str
    rationale: str


@dataclass(frozen=True, slots=True)
class NativeQuiz:
    title: str
    questions: tuple[QuizQuestion, ...]


@dataclass(frozen=True, slots=True)
class QuizFeedback:
    correct: bool
    correct_choice_id: str
    rationale: str


@dataclass(frozen=True, slots=True)
class PublishedQuizRecord:
    token: str
    lecture_id: int
    job_id: str
    title: str
    quiz: NativeQuiz
    version: int
