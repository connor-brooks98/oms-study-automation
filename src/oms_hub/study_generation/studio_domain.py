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


class StudioRunState(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    RETRYING = "retrying"
    COMPLETE = "complete"
    FAILED = "failed"


class StudioRunStage(StrEnum):
    VALIDATE = "validate"
    NOTEBOOK = "notebook"
    CHAT = "chat"
    COMPLETE = "complete"


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


@dataclass(frozen=True, slots=True)
class StudioRunSource:
    source_id: str
    remote_source_id: str
    title: str


@dataclass(frozen=True, slots=True)
class StudioRun:
    id: str
    subject: str
    subject_key: str
    exam_number: int
    destination_subject: str
    destination_subject_key: str
    destination_exam_number: int
    label: str
    prompt: str
    state: StudioRunState
    stage: StudioRunStage
    attempts: int
    next_attempt_at: str | None
    diagnostic_source: str | None
    error: str | None
    notebook_id: str | None
    raw_response: str | None
    supersedes_run_id: str | None
    sources: tuple[StudioRunSource, ...]


@dataclass(frozen=True, slots=True)
class StudioRunAttempt:
    attempt_number: int
    diagnostic_source: str
    raw_response: str | None
    error: str | None
    created_at: str
