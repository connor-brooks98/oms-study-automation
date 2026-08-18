import hashlib
import json
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
    CARD_BUILDING_LEDGER = "card_building_ledger"
    CARD_AUDITING_EVIDENCE = "card_auditing_evidence"
    CARD_SCOPING_TAGS = "card_scoping_tags"
    CARD_PREFILTERING = "card_prefiltering"
    CARD_FAST_CLASSIFYING = "card_fast_classifying"
    CARD_CLASSIFYING = "card_classifying"
    CARD_COVERAGE = "card_coverage"
    CARD_SWEEPING_RESIDUAL = "card_sweeping_residual"
    CARD_GENERATING_GAPS = "card_generating_gaps"
    CARD_DEDUPING = "card_deduping"
    CARD_SELECTING = "card_selecting"
    CARD_RECONCILING = "card_reconciling"
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
    V3_R0_PREFLIGHT = "v3_r0_preflight"
    V3_R1_SOURCE_INDEX = "v3_r1_source_index"
    V3_R2_FIDELITY = "v3_r2_fidelity"
    V3_R3_SCOPE = "v3_r3_scope"
    V3_R4_INDEX_VERIFICATION = "v3_r4_index_verification"
    V3_R5_RETRIEVAL = "v3_r5_retrieval"
    V3_R6_CALIBRATION = "v3_r6_calibration"
    V3_R7_CLASSIFICATION = "v3_r7_classification"
    V3_R8_GAP_CONFIRMATION = "v3_r8_gap_confirmation"
    V3_R9_GENERATION = "v3_r9_generation"
    V3_R10_DEDUPE = "v3_r10_dedupe"
    V3_R11_REVIEW = "v3_r11_review"
    V3_R12_APPLY = "v3_r12_apply"


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
    CARD_EVIDENCE_AUDIT = "card_evidence_audit"
    CARD_TAG_SCOPE = "card_tag_scope"
    CARD_PREFILTER = "card_prefilter"
    CARD_FAST_CLASSIFY = "card_fast_classify"
    CARD_CLASSIFY = "card_classify"
    CARD_COVERAGE = "card_coverage"
    CARD_RESIDUAL = "card_residual"
    CARD_GAP_FILL = "card_gap_fill"
    CARD_SELECTION = "card_selection"
    V3_R0_PREFLIGHT = "v3_r0_preflight"
    V3_R1_SOURCE_INDEX = "v3_r1_source_index"
    V3_R2_FIDELITY = "v3_r2_fidelity"
    V3_R3_SCOPE = "v3_r3_scope"
    V3_R4_INDEX_VERIFICATION = "v3_r4_index_verification"
    V3_R5_RETRIEVAL = "v3_r5_retrieval"
    V3_R6_CALIBRATION = "v3_r6_calibration"
    V3_R7_CLASSIFICATION = "v3_r7_classification"
    V3_R8_GAP_CONFIRMATION = "v3_r8_gap_confirmation"
    V3_R9_GENERATION = "v3_r9_generation"
    V3_R10_DEDUPE = "v3_r10_dedupe"
    V3_R11_REVIEW = "v3_r11_review"
    V3_R12_APPLY = "v3_r12_apply"


class PipelineContractVersion(StrEnum):
    RETRIEVAL_V4 = "retrieval_v4"
    CARD_CENTRIC_V1 = "card_centric_v1"
    CARD_CENTRIC_V2 = "card_centric_v2"
    CARD_CENTRIC_V3 = "card_centric_v3"


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
class ResolvedClassifierExecution:
    """Frozen S4b/S4c/S6 execution settings exposed to the P1/I0 seam.

    ``None`` on the enclosing model configuration remains the compatibility
    representation for documents written before P2-C.  P1/I0 owns persistence
    in job canonical documents and preflight snapshots; P2-C only defines the
    typed settings and the stage-artifact hook.
    """

    fast_batch_size: int = 60
    fast_concurrency: int = 4
    thorough_batch_size: int = 30
    thorough_concurrency: int = 4
    thorough_retry_attempts: int = 2
    thinking_budget_tokens: int = 1024

    def __post_init__(self) -> None:
        if self.fast_batch_size != 60:
            raise ValueError("card-centric v2 fast batch size must be 60")
        if (
            self.fast_concurrency < 1
            or self.thorough_batch_size < 1
            or self.thorough_concurrency < 1
            or self.thorough_retry_attempts != 2
            or self.thinking_budget_tokens < 1024
        ):
            raise ValueError("classifier execution configuration is invalid")

    def canonical_document(self) -> dict[str, int]:
        return {
            "fast_batch_size": self.fast_batch_size,
            "fast_concurrency": self.fast_concurrency,
            "thorough_batch_size": self.thorough_batch_size,
            "thorough_concurrency": self.thorough_concurrency,
            "thorough_retry_attempts": self.thorough_retry_attempts,
            "thinking_budget_tokens": self.thinking_budget_tokens,
        }

    @classmethod
    def from_document(cls, value: dict[str, Any]) -> "ResolvedClassifierExecution":
        expected = {
            "fast_batch_size",
            "fast_concurrency",
            "thorough_batch_size",
            "thorough_concurrency",
            "thorough_retry_attempts",
            "thinking_budget_tokens",
        }
        if set(value) != expected or any(type(value[name]) is not int for name in expected):
            raise ValueError("classifier execution document is invalid")
        return cls(
            fast_batch_size=value["fast_batch_size"],
            fast_concurrency=value["fast_concurrency"],
            thorough_batch_size=value["thorough_batch_size"],
            thorough_concurrency=value["thorough_concurrency"],
            thorough_retry_attempts=value["thorough_retry_attempts"],
            thinking_budget_tokens=value["thinking_budget_tokens"],
        )

    def canonical_json(self) -> str:
        return json.dumps(self.canonical_document(), sort_keys=True, separators=(",", ":"))

    def generation_parameters_sha256(self) -> str:
        return hashlib.sha256(self.canonical_json().encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class ResolvedModelConfiguration:
    profile: str
    ledger_s2: ResolvedStageModel
    classify_s4: ResolvedStageModel
    residual_s6: ResolvedStageModel
    gap_fill_s7: ResolvedStageModel
    residual_unlocked: bool = False
    # None deliberately preserves old v1/legacy persisted canonical documents.
    fast_classify_s4b: ResolvedStageModel | None = None
    # None preserves legacy persisted canonical documents. New v2 jobs carry
    # an explicit execution document so batch/concurrency inputs participate in
    # the resolved-model hash and every downstream replay identity.
    classifier_execution: ResolvedClassifierExecution | None = None
    # These optional routes are the v3 tier identity only.  V3 remains
    # unexecutable until its later stage implementation is authorized.
    scope_r3: ResolvedStageModel | None = None
    cheap_classify_r7: ResolvedStageModel | None = None
    thorough_classify_r7: ResolvedStageModel | None = None
    generation_r9: ResolvedStageModel | None = None

    def __post_init__(self) -> None:
        if not self.profile.strip():
            raise ValueError("model configuration profile cannot be blank")
        if (self.classify_s4.provider, self.classify_s4.model) != (
            self.ledger_s2.provider,
            self.ledger_s2.model,
        ) and not self.classify_s4.fixture_validation_signature:
            raise ValueError("cheaper S4 model is not validated on the Lecture07 fixture")
        if not self.residual_unlocked and self.residual_s6 != self.classify_s4:
            raise ValueError("S6 must match S4 unless residual model is explicitly unlocked")

    @classmethod
    def legacy(cls, provider: str, model: str) -> "ResolvedModelConfiguration":
        stage = ResolvedStageModel(provider=provider, model=model)
        return cls("legacy_single_model", stage, stage, stage, stage)

    @classmethod
    def card_centric_default(
        cls,
        provider: str,
        model: str,
    ) -> "ResolvedModelConfiguration":
        standard = ResolvedStageModel(provider=provider, model=model)
        classifier = ResolvedStageModel(
            provider=provider,
            model=model,
            thinking_mode="disabled",
        )
        return cls(
            "card_centric_default",
            standard,
            classifier,
            classifier,
            standard,
        )

    @classmethod
    def card_centric_v2_default(cls, provider: str, model: str) -> "ResolvedModelConfiguration":
        base = cls.card_centric_default(provider, model)
        return cls(
            base.profile,
            base.ledger_s2,
            base.classify_s4,
            base.residual_s6,
            base.gap_fill_s7,
            base.residual_unlocked,
            ResolvedStageModel("openai", "gpt-4o-mini", thinking_mode="disabled"),
            ResolvedClassifierExecution(),
        )

    def resolved_classifier_execution(self) -> ResolvedClassifierExecution:
        """Return compatible defaults without rewriting legacy configuration."""
        return self.classifier_execution or ResolvedClassifierExecution(
            fast_batch_size=60,
            fast_concurrency=4,
            thorough_batch_size=30,
            thorough_concurrency=4,
            thorough_retry_attempts=2,
            thinking_budget_tokens=1024,
        )

    def canonical_document(self) -> dict[str, Any]:
        def stage(value: ResolvedStageModel) -> dict[str, Any]:
            return {
                "provider": value.provider,
                "model": value.model,
                "thinking_mode": value.thinking_mode,
                "fixture_validation_signature": value.fixture_validation_signature,
            }

        document = {
            "profile": self.profile,
            "ledger_s2": stage(self.ledger_s2),
            "classify_s4": stage(self.classify_s4),
            "residual_s6": stage(self.residual_s6),
            "gap_fill_s7": stage(self.gap_fill_s7),
            "residual_unlocked": self.residual_unlocked,
        }
        if self.fast_classify_s4b is not None:
            document["fast_classify_s4b"] = stage(self.fast_classify_s4b)
        if self.classifier_execution is not None:
            document["classifier_execution"] = self.classifier_execution.canonical_document()
        for name, value in (
            ("scope_r3", self.scope_r3),
            ("cheap_classify_r7", self.cheap_classify_r7),
            ("thorough_classify_r7", self.thorough_classify_r7),
            ("generation_r9", self.generation_r9),
        ):
            if value is not None:
                document[name] = stage(value)
        return document

    def require_card_centric_v2_fast_classifier(self) -> None:
        """Enforce the approved persisted S4b route for v2 jobs only."""
        stage = self.fast_classify_s4b
        if stage is None:
            raise ValueError("card-centric v2 requires a fast S4b model")
        if (
            stage.provider not in {"openai", "anthropic", "gemini"}
            or not stage.model.strip()
            or stage.thinking_mode != "disabled"
        ):
            raise ValueError(
                "card-centric v2 S4b requires an approved provider, a nonblank model, "
                "and disabled thinking"
            )

    def has_v3_tier_routes(self) -> bool:
        return any(
            route is not None
            for route in (
                self.scope_r3,
                self.cheap_classify_r7,
                self.thorough_classify_r7,
                self.generation_r9,
            )
        )


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
    policy_sha256: str | None = None
    rate_table_document: dict[str, object] | None = None
    offline_replay_only: bool = False

    def __post_init__(self) -> None:
        v3_only = (
            self.policy_sha256 is not None
            or self.rate_table_document is not None
            or self.offline_replay_only
            or (
                self.resolved_model_config is not None
                and self.resolved_model_config.has_v3_tier_routes()
            )
        )
        if (
            v3_only
            and self.pipeline_contract_version is not PipelineContractVersion.CARD_CENTRIC_V3
        ):
            raise ValueError("v3-only policy and model-tier fields require card_centric_v3")


# Frozen names only.  Phase H wires these into runtime replay identity.
V3_REPLAY_IDENTITY_FIELDS = (
    "policy_sha256",
    "policy_revision",
    "style_fidelity_sha256",
    "scope_sha256",
    "lexical_generation",
    "semantic_generation",
    "retrieval_calibration_sha256",
    "evidence_bundle_sha256",
    "model_tier_escalation_identity",
    "cost_policy_rate_table_sha256",
)


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
    policy_sha256: str | None = None
    rate_table_document: dict[str, object] | None = None
    rate_table_sha256: str | None = None
    offline_replay_only: bool = False


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
