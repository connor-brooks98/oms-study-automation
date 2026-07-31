from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path


class StudioSourceType(StrEnum):
    FILE = "file"
    TEXT = "text"
    URL = "url"


class StudioSourceState(StrEnum):
    PENDING = "pending"
    ATTACHING = "attaching"
    ATTACHED = "attached"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class StudioSource:
    id: str
    subject: str
    subject_key: str
    exam_number: int
    source_type: StudioSourceType
    title: str
    original_filename: str | None
    payload_path: Path | None
    source_url: str | None
    state: StudioSourceState
    attempts: int
    next_attempt_at: str | None
    diagnostic_source: str | None
    error: str | None
    remote_notebook_id: str | None
    remote_source_id: str | None
    converted_from_pptx: bool
