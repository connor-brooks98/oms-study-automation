from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any
from uuid import UUID


class CurationState(StrEnum):
    QUEUED = "queued"
    BUILDING_LCL = "building_lcl"
    RETRIEVING = "retrieving"
    JUDGING = "judging"
    DEDUPING = "deduping"
    PROPOSING_GAPS = "proposing_gaps"
    READY_FOR_REVIEW = "ready_for_review"
    ENVELOPE_PENDING = "envelope_pending"
    APPLYING = "applying"
    COMPLETE = "complete"
    FAILED = "failed"


class CurationStage(StrEnum):
    LCL = "lcl"
    RETRIEVAL = "retrieval"
    JUDGMENT = "judgment"
    DEDUPE = "dedupe"
    GAPS = "gaps"
    MEDIA = "media"
    ENVELOPE = "envelope"


class Verdict(StrEnum):
    INCLUDE = "include"
    UNCERTAIN = "uncertain"
    DROP = "drop"


class EnvelopeOperationType(StrEnum):
    STORE_MEDIA = "store_media"
    ADD_TAGS = "add_tags"
    ADD_NOTES = "add_notes"
    SYNC = "sync"
    VERIFY = "verify"


@dataclass(frozen=True, slots=True)
class CreateCurationJob:
    lecture_id: int
    amboss_input: str
    instruction_text: str
    target_deck: str
    target_tag: str
    index_snapshot_id: str
    lcl_prompt_version: str
    judgment_rubric_version: str
    gap_prompt_version: str


@dataclass(frozen=True, slots=True)
class CurationJob:
    id: UUID
    lecture_id: int
    state: CurationState
    attempts: int
    amboss_input: str
    amboss_sha256: str
    instruction_text: str
    instruction_sha256: str
    target_deck: str
    target_tag: str
    index_snapshot_id: str
    lcl_prompt_version: str
    judgment_rubric_version: str
    gap_prompt_version: str
    review_revision: int
    error: str | None
    created_at: str
    updated_at: str


@dataclass(frozen=True, slots=True)
class StageUsage:
    request_id: str
    input_tokens: int
    output_tokens: int
    cost_microusd: int


@dataclass(frozen=True, slots=True)
class JobStage:
    job_id: UUID
    stage: CurationStage
    state: str
    attempt_count: int
    provider: str | None
    model: str | None
    request_id: str | None
    input_tokens: int
    output_tokens: int
    cost_microusd: int
    cache_hits: int
    error: str | None


@dataclass(frozen=True, slots=True)
class Candidate:
    note_id: int
    content_hash: str
    best_concept_id: str
    provenance: dict[str, Any]
    scores: dict[str, float]
    predicted_band: str
    verdict: str
    confidence: float
    reason: str
    context_trap: bool
    recall_direction: str
    mnemonic_classification: str
    dedupe_disposition: str
    selected: bool


@dataclass(frozen=True, slots=True)
class GapCard:
    concept_id: str
    text: str
    extra: str
    revision: int = 1
    selected: bool = True
    image_state: str = "none"
    media_filename: str | None = None
    source_note_id: int | None = None
    generated_image: dict[str, Any] = field(default_factory=dict)
    validation_state: str = "valid"


@dataclass(frozen=True, slots=True)
class GapCardEdit:
    concept_id: str
    text: str
    extra: str
    selected: bool


@dataclass(frozen=True, slots=True)
class ReviewChangeSet:
    expected_revision: int
    candidate_selections: dict[int, bool] = field(default_factory=dict)
    gap_edits: tuple[GapCardEdit, ...] = ()


@dataclass(frozen=True, slots=True)
class SavedReview:
    job_id: UUID
    revision: int


@dataclass(frozen=True, slots=True)
class EnvelopeOperationDraft:
    operation_id: str
    operation_type: str
    payload: dict[str, Any]


@dataclass(frozen=True, slots=True)
class EnvelopeDraft:
    envelope_id: str
    snapshot_id: str
    payload: dict[str, Any]
    operations: tuple[EnvelopeOperationDraft, ...]


@dataclass(frozen=True, slots=True)
class StoredEnvelope:
    id: UUID
    job_id: UUID
    snapshot_id: str
    payload_sha256: str
    state: str
    receipt_summary: dict[str, Any] | None


@dataclass(frozen=True, slots=True)
class StoredEnvelopeOperation:
    id: UUID
    envelope_id: UUID
    operation_type: str
    content_hash: str
    payload: dict[str, Any]
    state: str
    attempts: int
    result: dict[str, Any] | None
    error: str | None
