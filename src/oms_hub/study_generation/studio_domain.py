from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from oms_hub.study_generation.domain import NativeQuiz
from oms_hub.study_generation.practice_domain import (
    ImportSourceRole,
    QuizContentKind,
    QuizWorkflowKind,
    StudioSourcePurpose,
)


class StudioSourceType(StrEnum):
    FILE = "file"
    TEXT = "text"
    URL = "url"


class StudioSourceState(StrEnum):
    PENDING = "pending"
    ATTACHING = "attaching"
    ATTACHED = "attached"
    READY = "ready"
    FAILED = "failed"
    DELETING = "deleting"
    NEEDS_REVIEW = "needs_review"
    DELETED = "deleted"


class StudioRunState(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    RETRYING = "retrying"
    AWAITING_IMAGES = "awaiting_images"
    AWAITING_REVIEW = "awaiting_review"
    COMPLETE = "complete"
    FAILED = "failed"


class StudioRunStage(StrEnum):
    VALIDATE = "validate"
    NOTEBOOK = "notebook"
    CHAT = "chat"
    QUIZ_VALIDATE = "quiz_validate"
    IMAGE_REVIEW = "image_review"
    PUBLISH = "publish"
    COMPLETE = "complete"
    ACQUIRE = "acquire"
    PARSE = "parse"
    EXTRACT = "extract"
    PAIR = "pair"
    ANSWER_NOTEBOOK = "answer_notebook"
    ANSWER_FALLBACK = "answer_fallback"
    NORMALIZE = "normalize"
    REVIEW = "review"
    ACCURACY = "accuracy"


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
    purpose: StudioSourcePurpose = StudioSourcePurpose.NOTEBOOK
    snapshot_sha256: str | None = None
    media_type: str | None = None
    final_url: str | None = None
    import_role: ImportSourceRole | None = None
    import_attach_to_notebook: bool = False


@dataclass(frozen=True, slots=True)
class StudioSourceOperation:
    id: str
    source_id: str
    operation_kind: str
    state: str
    notebook_id: str | None
    remote_source_id: str | None
    baseline_remote_ids: frozenset[str]
    attempts: int
    diagnostic_source: str | None
    error: str | None


@dataclass(frozen=True, slots=True)
class StudioRunSource:
    source_id: str
    remote_source_id: str
    title: str


@dataclass(frozen=True, slots=True)
class StudioImportRunSource:
    source_id: str
    role: ImportSourceRole
    attach_to_notebook: bool
    remote_notebook_id: str | None
    remote_source_id: str | None
    position: int


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
    draft_payload_json: str | None
    published_token: str | None
    supersedes_run_id: str | None
    sources: tuple[StudioRunSource, ...]
    workflow_kind: QuizWorkflowKind = QuizWorkflowKind.NOTEBOOK_GENERATION
    content_kind: QuizContentKind = QuizContentKind.EXAM_REVIEW


@dataclass(frozen=True, slots=True)
class StudioRunAttempt:
    attempt_number: int
    diagnostic_source: str
    raw_response: str | None
    error: str | None
    created_at: str


@dataclass(frozen=True, slots=True)
class StudioRunArtifact:
    artifact_key: str
    signature_sha256: str
    payload_json: str
    provider: str | None
    model: str | None
    request_id: str | None


@dataclass(frozen=True, slots=True)
class StudioStoredImage:
    path: Path
    sha256: str
    media_type: str
    width: int
    height: int
    original_filename: str


@dataclass(frozen=True, slots=True)
class StudioQuizImageRequirement:
    image_key: str
    source_title: str
    locator: str
    description: str
    question_ids: tuple[str, ...]
    image: StudioStoredImage | None


@dataclass(frozen=True, slots=True)
class StudioQuizReview:
    run: StudioRun
    quiz: NativeQuiz
    requirements: tuple[StudioQuizImageRequirement, ...]
    overridden_question_ids: frozenset[str]

    @property
    def unresolved_keys(self) -> tuple[str, ...]:
        return tuple(
            requirement.image_key
            for requirement in self.requirements
            if requirement.image is None
            and any(
                question_id not in self.overridden_question_ids
                for question_id in requirement.question_ids
            )
        )

    @property
    def resolved(self) -> bool:
        return not self.unresolved_keys
