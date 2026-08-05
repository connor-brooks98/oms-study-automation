from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any
from uuid import UUID


class CurationState(StrEnum):
    QUEUED = "queued"
    PREFLIGHT = "preflight"
    SNAPSHOTTING_EMBEDDINGS = "snapshotting_embeddings"
    BUILDING_COMPANION_INDEX = "building_companion_index"
    BUILDING_SOURCE_INDEX = "building_source_index"
    BUILDING_LCL = "building_lcl"
    RETRIEVING_PASS_1 = "retrieving_pass_1"
    JUDGING_PASS_1 = "judging_pass_1"
    LOCALIZING_MISSED_CONCEPTS = "localizing_missed_concepts"
    RETRIEVING_PASS_2 = "retrieving_pass_2"
    JUDGING_PASS_2 = "judging_pass_2"
    CONVERGING_PASS_3 = "converging_pass_3"
    CONVERGING_PASS_4 = "converging_pass_4"
    CONVERGING_PASS_5 = "converging_pass_5"
    AUDITING_CANDIDATES = "auditing_candidates"
    RECOMPUTING_COVERAGE = "recomputing_coverage"
    DEDUPING = "deduping"
    GENERATING_GAPS = "generating_gaps"
    RECONCILING = "reconciling"
    READY_FOR_REVIEW = "ready_for_review"
    ENVELOPE_PENDING = "envelope_pending"
    APPLYING_LOCAL = "applying_local"
    SYNCING = "syncing"
    VERIFYING = "verifying"
    COMPLETE = "complete"
    FAILED = "failed"
    CANCELED = "canceled"
    REMOVED = "removed"


class CurationStage(StrEnum):
    PREFLIGHT = "preflight"
    SEMANTIC_SNAPSHOT = "semantic_snapshot"
    COMPANION_INDEX = "companion_index"
    SOURCE_INDEX = "source_index"
    LCL = "lcl"
    RETRIEVAL_PASS_1 = "retrieval_pass_1"
    JUDGMENT_PASS_1 = "judgment_pass_1"
    RESCUE = "rescue"
    RETRIEVAL_PASS_2 = "retrieval_pass_2"
    JUDGMENT_PASS_2 = "judgment_pass_2"
    CONVERGENCE_PASS_3 = "convergence_pass_3"
    CONVERGENCE_PASS_4 = "convergence_pass_4"
    CONVERGENCE_PASS_5 = "convergence_pass_5"
    CARD_AUDIT = "card_audit"
    COVERAGE_RECOMPUTE = "coverage_recompute"
    DEDUPE = "dedupe"
    GAPS = "gaps"
    RECONCILIATION = "reconciliation"
    ENVELOPE = "envelope"
    APPLY = "apply"
    SYNC = "sync"
    VERIFY = "verify"
    CARD_LEDGER = "card_ledger"
    CARD_CLASSIFY = "card_classify"
    CARD_RESIDUAL = "card_residual"
    CARD_GAP_FILL = "card_gap_fill"


class PipelineContractVersion(StrEnum):
    RETRIEVAL_V4 = "retrieval_v4"
    CARD_CENTRIC_V1 = "card_centric_v1"


@dataclass(frozen=True, slots=True)
class ResolvedStageModel:
    provider: str
    model: str
    thinking_mode: str = "default"
    fixture_validation_signature: str | None = None

    def __post_init__(self) -> None:
        if not self.provider.strip() or not self.model.strip() or not self.thinking_mode.strip():
            raise ValueError("resolved stage model values cannot be blank")
        if self.provider not in {"openai", "gemini", "anthropic", "openrouter"}:
            raise ValueError("resolved stage model provider is unsupported")
        if self.thinking_mode not in {"default", "enabled", "disabled"}:
            raise ValueError("resolved stage model thinking mode is unsupported")


@dataclass(frozen=True, slots=True)
class ResolvedModelConfiguration:
    profile: str
    ledger_s2: ResolvedStageModel
    classify_s4: ResolvedStageModel
    residual_s6: ResolvedStageModel
    gap_fill_s7: ResolvedStageModel
    residual_unlocked: bool = False

    def __post_init__(self) -> None:
        if not self.profile.strip():
            raise ValueError("model configuration profile cannot be blank")

    @classmethod
    def legacy(cls, provider: str, model: str) -> "ResolvedModelConfiguration":
        stage = ResolvedStageModel(provider=provider, model=model)
        return cls("legacy_single_model", stage, stage, stage, stage)

    def canonical_document(self) -> dict[str, Any]:
        def stage(value: ResolvedStageModel) -> dict[str, Any]:
            return {
                "provider": value.provider,
                "model": value.model,
                "thinking_mode": value.thinking_mode,
                "fixture_validation_signature": value.fixture_validation_signature,
            }

        return {
            "profile": self.profile,
            "ledger_s2": stage(self.ledger_s2),
            "classify_s4": stage(self.classify_s4),
            "residual_s6": stage(self.residual_s6),
            "gap_fill_s7": stage(self.gap_fill_s7),
            "residual_unlocked": self.residual_unlocked,
        }


class RetrievalPass(StrEnum):
    PASS_1 = "pass_1"
    PASS_2_RESCUE = "pass_2_rescue"
    CONVERGENCE = "convergence"


class EvidenceSupport(StrEnum):
    SUPPORTED = "supported"
    PARTIAL = "partial"
    UNSUPPORTED = "unsupported"


class ApplyState(StrEnum):
    PENDING = "pending"
    FAILED_BEFORE_APPLY = "failed_before_apply"
    COMPLETE = "complete"
    APPLIED_LOCAL_SYNC_RETRYABLE = "applied_local_sync_retryable"
    APPLIED_LOCAL_SYNC_BLOCKED = "applied_local_sync_blocked"
    APPLY_PARTIAL = "apply_partial"


class SourceKind(StrEnum):
    SLIDE = "slide"
    SPEAKER_NOTES = "speaker_notes"
    TRANSCRIPT = "transcript"
    VISION = "vision"
    SUMMARY = "summary"


class AgentCommandType(StrEnum):
    FULL_SNAPSHOT = "full_snapshot"
    DELTA_SNAPSHOT = "delta_snapshot"
    FETCH_MEDIA = "fetch_media"
    APPLY_ENVELOPE = "apply_envelope"


@dataclass(frozen=True, slots=True)
class CreateCurationJob:
    lecture_id: int
    block_id: str | None
    source_revision_ids: tuple[int, ...]
    deck_allowlist: tuple[str, ...]
    tag_allowlist: tuple[str, ...]
    instruction_text: str
    target_deck: str
    target_tag: str
    index_snapshot_id: str
    lcl_prompt_version: str
    judgment_rubric_version: str
    gap_prompt_version: str
    provider: str
    model: str
    pipeline_contract_version: PipelineContractVersion = PipelineContractVersion.RETRIEVAL_V4
    resolved_model_config: ResolvedModelConfiguration | None = None
    source_revision_hashes: dict[int, str] = field(default_factory=dict)
    semantic_generation: str | None = None
    companion_generation: str | None = None
    summary_outline_id: int | None = None
    summary_outline_sha256: str | None = None


@dataclass(frozen=True, slots=True)
class CurationJob:
    id: UUID
    lecture_id: int
    state: CurationState
    attempts: int
    block_id: str | None
    source_revision_ids: tuple[int, ...]
    source_revision_hashes: dict[int, str]
    deck_allowlist: tuple[str, ...]
    tag_allowlist: tuple[str, ...]
    provider: str
    model: str
    pipeline_contract_version: PipelineContractVersion
    resolved_model_config: ResolvedModelConfiguration
    model_config_sha256: str
    instruction_text: str
    instruction_sha256: str
    target_deck: str
    target_tag: str
    index_snapshot_id: str
    lcl_prompt_version: str
    judgment_rubric_version: str
    gap_prompt_version: str
    semantic_generation: str | None
    companion_generation: str | None
    source_index_generation: str | None
    configuration_sha256: str
    apply_state: ApplyState
    review_revision: int
    error: str | None
    lease_owner: str | None
    lease_expires_at: str | None
    available_at: str | None
    created_at: str
    updated_at: str
    summary_outline_id: int | None = None
    summary_outline_sha256: str | None = None


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
    retrieval_pass: RetrievalPass = RetrievalPass.PASS_1


@dataclass(frozen=True, slots=True)
class SourceReference:
    source_kind: SourceKind
    revision_id: int
    locator: str
    content_hash: str


@dataclass(frozen=True, slots=True)
class SourceEvidence:
    evidence_id: str
    concept_id: str
    support: EvidenceSupport
    statement: str
    source_refs: tuple[SourceReference, ...]
    content_hash: str


@dataclass(frozen=True, slots=True)
class StageArtifact:
    artifact_id: str
    stage: CurationStage
    kind: str
    relative_path: str
    input_sha256: str
    content_sha256: str
    pipeline_contract_version: PipelineContractVersion = PipelineContractVersion.RETRIEVAL_V4
    model_config_sha256: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


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
    source_refs: tuple[SourceReference, ...] = ()
    evidence_ids: tuple[str, ...] = ()
    provenance: dict[str, Any] = field(default_factory=dict)
    initial_tags: tuple[str, ...] = ()
    content_hash: str = ""
    card_id: str = ""


@dataclass(frozen=True, slots=True)
class GapCardEdit:
    concept_id: str
    text: str
    extra: str
    selected: bool
    card_id: str = ""


@dataclass(frozen=True, slots=True)
class TagPatch:
    note_id: int
    before: tuple[str, ...]
    after: tuple[str, ...]
    add_tags: tuple[str, ...]
    remove_tags: tuple[str, ...]
    expected_tag_hash: str
    tag_policy_version: str


@dataclass(frozen=True, slots=True)
class ReviewChangeSet:
    expected_revision: int
    reviewer: str = "local-user"
    candidate_selections: dict[int, bool] = field(default_factory=dict)
    gap_edits: tuple[GapCardEdit, ...] = ()
    tag_patches: tuple[TagPatch, ...] = ()


@dataclass(frozen=True, slots=True)
class SavedReview:
    job_id: UUID
    revision: int


@dataclass(frozen=True, slots=True)
class StoredReviewChange:
    job_id: UUID
    revision: int
    prior_revision: int
    reviewer: str
    payload: dict[str, Any]
    created_at: str


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
class AgentState:
    agent_id: str | None
    heartbeat_at: str | None
    versions: dict[str, Any]
    active_snapshot_id: str | None
    health: dict[str, Any]


@dataclass(frozen=True, slots=True)
class StoredAgentCommand:
    id: UUID
    command_type: AgentCommandType
    state: str
    payload: dict[str, Any]
    payload_sha256: str
    owner_agent_id: str | None
    created_at: str
