from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum


class TranscriptAction(StrEnum):
    CLEAN = "clean"
    FILE = "file"


class TranscriptJobState(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    WAITING = "waiting"
    COMPLETE = "complete"
    FAILED = "failed"
    NEEDS_REVIEW = "needs_review"


@dataclass(frozen=True, slots=True)
class PanoptoSession:
    session_id: str
    name: str
    created_utc: datetime
    duration_seconds: float
    folder_name: str
    content_language: str | None
    caption_download_url: str | None


@dataclass(frozen=True, slots=True)
class RecordingMatch:
    lecture_id: int | None
    confidence: float
    evidence: tuple[str, ...]
    needs_review: bool


@dataclass(frozen=True, slots=True)
class RecordingDisposition:
    recording_id: int
    created: bool
    needs_review: bool

