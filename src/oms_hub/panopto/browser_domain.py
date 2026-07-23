from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum


class BrowserCommandKind(StrEnum):
    CONNECTION_CHECK = "connection_check"
    SCAN = "scan"
    ACCEPTANCE = "acceptance"


@dataclass(frozen=True, slots=True)
class BrowserCommand:
    id: str
    kind: BrowserCommandKind
    payload: dict[str, object]


@dataclass(frozen=True, slots=True)
class BrowserRecording:
    session_id: str
    name: str
    created_utc: datetime
    duration_seconds: float
    folder_name: str
    viewer_url: str


@dataclass(frozen=True, slots=True)
class BrowserDisposition:
    recording_id: int
    session_id: str
    action: str
    viewer_url: str | None
    reason: str


@dataclass(frozen=True, slots=True)
class TranscriptExtraction:
    command_id: str
    recording_id: int
    session_id: str
    viewer_url: str
    language: str
    line_count: int
    complete: bool
    text: str
