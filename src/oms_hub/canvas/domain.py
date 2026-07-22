from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path


class ConnectionState(StrEnum):
    UNPAIRED = "unpaired"
    CONNECTED = "connected"
    SCANNING = "scanning"
    LOGIN_REQUIRED = "canvas_login_required"
    DISCONNECTED = "disconnected"
    ERROR = "error"


class SourceKind(StrEnum):
    LECTURE = "lecture"
    PRACTICE_QUESTIONS = "practice_questions"
    IGNORE = "ignore"
    REVIEW = "review"


class ReviewState(StrEnum):
    NONE = "none"
    NEEDS_REVIEW = "needs_review"
    RESOLVED = "resolved"


class RevisionState(StrEnum):
    DISCOVERED = "discovered"
    DOWNLOADED = "downloaded"
    PROPOSED = "proposed"
    CURRENT = "current"
    KEPT = "kept"
    FAILED = "failed"


class ArtifactRole(StrEnum):
    ORIGINAL = "original"
    STAGED_PDF = "staged_pdf"
    LOCAL_PPTX = "local_pptx"
    LOCAL_PDF = "local_pdf"
    ICLOUD_PDF = "icloud_pdf"


class ValidationState(StrEnum):
    PENDING = "pending"
    VALID = "valid"
    INVALID = "invalid"


class JobAction(StrEnum):
    INGEST = "ingest"
    CONVERT = "convert"
    PROMOTE = "promote"


class JobState(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETE = "complete"
    FAILED = "failed"
    NEEDS_REVIEW = "needs_review"


@dataclass(frozen=True, slots=True)
class CanvasAttachment:
    course_id: str
    course_name: str
    course_code: str
    module_id: str
    module_title: str
    item_id: str
    item_title: str
    item_type: str
    page_url: str
    page_title: str
    file_id: str
    filename: str
    content_type: str
    size: int
    modified_at: str
    download_url: str
    evidence_text: str = ""


@dataclass(frozen=True, slots=True)
class Classification:
    kind: SourceKind
    confidence: float
    reason: str


@dataclass(frozen=True, slots=True)
class CatalogMatch:
    lecture_id: int | None
    subject: str | None
    exam_number: int | None
    confidence: float
    reason: str


@dataclass(frozen=True, slots=True)
class DownloadDisposition:
    source_item_id: int
    action: str
    reason: str
    relative_filename: str | None = None


@dataclass(frozen=True, slots=True)
class CanonicalPaths:
    revision_original: Path
    revision_pdf: Path
    local_source: Path | None
    local_pdf: Path
    icloud_pdf: Path


@dataclass(frozen=True, slots=True)
class CourseMappingInput:
    course_id: str
    course_name: str
    course_code: str
    subject: str
    enabled: bool = True


@dataclass(frozen=True, slots=True)
class MetadataResult:
    source_item_id: int
    revision_id: int
    created: bool
    review_state: ReviewState


@dataclass(frozen=True, slots=True)
class DispositionContext:
    source_item_id: int
    revision_id: int
    kind: SourceKind
    lecture_id: int | None
    subject: str | None
    exam_number: int | None
    confidence: float
    has_current_artifact: bool


@dataclass(frozen=True, slots=True)
class FailedRevisionReview:
    revision_id: int
    filename: str
    subject: str | None
    error: str
