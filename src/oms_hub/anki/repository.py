import hashlib
import json
import re
from collections.abc import Sequence
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any, cast
from uuid import UUID, uuid4

from sqlalchemy import delete, or_, select, text, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from oms_hub.anki.apply import ApplyOperationRecord
from oms_hub.anki.audit import AuditCacheRecord
from oms_hub.anki.card_centric import (
    CardCentricLedgerAttempt,
    _redacted_invalid_response,
    resolve_card_centric_scope,
    validate_s2_generation_parameters,
)
from oms_hub.anki.card_centric_review import (
    OverflowAcknowledgement,
    V3ReviewReconciliation,
    V3ReviewSnapshot,
    issue_acknowledgement,
    reconcile_v3,
    selection_digest,
    verify_acknowledgement,
)
from oms_hub.anki.contracts import (
    ActionEnvelopeDocument,
    ActionEnvelopeV2,
    canonical_payload_sha256,
    parse_action_envelope,
)
from oms_hub.anki.correction_contracts import (
    A11HistoryEntry,
    A11HistorySnapshot,
    CanonicalJsonObject,
    PinnedLectureMetadata,
    _sha,
)
from oms_hub.anki.cost_estimator import FrozenRateTable
from oms_hub.anki.course_policy import CourseCurationPolicy
from oms_hub.anki.domain import (
    AgentCommandType,
    AgentState,
    ApplyState,
    Candidate,
    CreateCurationJob,
    CurationJob,
    CurationStage,
    CurationState,
    EnvelopeDraft,
    EvidenceSupport,
    GapCard,
    JobStage,
    PipelineContractVersion,
    ResolvedClassifierExecution,
    ResolvedModelConfiguration,
    ResolvedStageModel,
    RetrievalPass,
    ReviewChangeSet,
    SavedReview,
    SourceEvidence,
    SourceKind,
    SourceReference,
    StageArtifact,
    StageUsage,
    StoredAgentCommand,
    StoredEnvelope,
    StoredReviewChange,
    TagPatch,
)
from oms_hub.anki.judgment import JudgmentCacheRecord
from oms_hub.anki.models import (
    AnkiAgentCommandModel,
    AnkiAgentStateModel,
    AnkiCandidateModel,
    AnkiCardAuditCacheModel,
    AnkiCardLedgerAttemptModel,
    AnkiCoverageJudgmentCacheModel,
    AnkiCurationJobModel,
    AnkiEnvelopeModel,
    AnkiEnvelopeOperationModel,
    AnkiGapCardModel,
    AnkiJobStageModel,
    AnkiProviderAttemptEventModel,
    AnkiReviewChangeSetModel,
    AnkiReviewedReconciliationModel,
    AnkiSourceEvidenceModel,
    AnkiStageArtifactModel,
    AnkiStageReplayInputModel,
    AnkiStageSettingModel,
    AnkiTagPatchModel,
    CourseCurationPolicyModel,
)
from oms_hub.anki.provider_attempts import (
    ProviderAttemptIndeterminate,
    ProviderEventEvidence,
)
from oms_hub.anki.reconciliation import (
    CardCentricReconciliationInput,
    reconcile_card_centric,
    selected_card_centric_coverage,
)
from oms_hub.anki.replay_inputs import PreparedStageReplayInputs, canonical_json
from oms_hub.db import Database
from oms_hub.llm.domain import DiagnosticSource, ProviderName
from oms_hub.models import LectureModel, utc_now

ALLOWED_TRANSITIONS: dict[CurationState, set[CurationState]] = {
    CurationState.QUEUED: {
        CurationState.PREFLIGHT,
        CurationState.V3_R0_PREFLIGHT,
        CurationState.FAILED,
    },
    CurationState.V3_R0_PREFLIGHT: {CurationState.V3_R1_SOURCE_INDEX, CurationState.FAILED},
    CurationState.V3_R1_SOURCE_INDEX: {CurationState.V3_R2_FIDELITY, CurationState.FAILED},
    CurationState.V3_R2_FIDELITY: {CurationState.V3_R3_SCOPE, CurationState.FAILED},
    CurationState.V3_R3_SCOPE: {CurationState.V3_R4_INDEX_VERIFICATION, CurationState.FAILED},
    CurationState.V3_R4_INDEX_VERIFICATION: {CurationState.V3_R5_RETRIEVAL, CurationState.FAILED},
    CurationState.V3_R5_RETRIEVAL: {CurationState.V3_R6_CALIBRATION, CurationState.FAILED},
    CurationState.V3_R6_CALIBRATION: {CurationState.V3_R7_CLASSIFICATION, CurationState.FAILED},
    CurationState.V3_R7_CLASSIFICATION: {
        CurationState.V3_R8_GAP_CONFIRMATION,
        CurationState.FAILED,
    },
    CurationState.V3_R8_GAP_CONFIRMATION: {CurationState.V3_R9_GENERATION, CurationState.FAILED},
    CurationState.V3_R9_GENERATION: {CurationState.V3_R10_DEDUPE, CurationState.FAILED},
    CurationState.V3_R10_DEDUPE: {CurationState.V3_R11_REVIEW, CurationState.FAILED},
    CurationState.V3_R11_REVIEW: {CurationState.READY_FOR_REVIEW, CurationState.FAILED},
    CurationState.PREFLIGHT: {
        CurationState.SNAPSHOTTING_EMBEDDINGS,
        CurationState.BUILDING_COMPANION_INDEX,
        CurationState.BUILDING_SOURCE_INDEX,
        CurationState.BUILDING_LCL,
        CurationState.FAILED,
    },
    CurationState.SNAPSHOTTING_EMBEDDINGS: {
        CurationState.BUILDING_COMPANION_INDEX,
        CurationState.FAILED,
    },
    CurationState.BUILDING_COMPANION_INDEX: {
        CurationState.BUILDING_SOURCE_INDEX,
        CurationState.FAILED,
    },
    CurationState.BUILDING_SOURCE_INDEX: {
        CurationState.BUILDING_LCL,
        CurationState.CARD_BUILDING_LEDGER,
        CurationState.FAILED,
    },
    CurationState.CARD_BUILDING_LEDGER: {
        CurationState.CARD_SCOPING_TAGS,
        CurationState.CARD_AUDITING_EVIDENCE,
        CurationState.FAILED,
    },
    CurationState.CARD_AUDITING_EVIDENCE: {
        CurationState.CARD_SCOPING_TAGS,
        CurationState.FAILED,
    },
    CurationState.CARD_SCOPING_TAGS: {
        CurationState.CARD_CLASSIFYING,
        CurationState.CARD_PREFILTERING,
        CurationState.FAILED,
    },
    CurationState.CARD_PREFILTERING: {
        CurationState.CARD_FAST_CLASSIFYING,
        CurationState.FAILED,
    },
    CurationState.CARD_FAST_CLASSIFYING: {
        CurationState.CARD_CLASSIFYING,
        CurationState.FAILED,
    },
    CurationState.CARD_CLASSIFYING: {
        CurationState.CARD_COVERAGE,
        CurationState.FAILED,
    },
    CurationState.CARD_COVERAGE: {CurationState.CARD_SWEEPING_RESIDUAL, CurationState.FAILED},
    CurationState.CARD_SWEEPING_RESIDUAL: {
        CurationState.CARD_GENERATING_GAPS,
        CurationState.FAILED,
    },
    CurationState.CARD_GENERATING_GAPS: {CurationState.CARD_DEDUPING, CurationState.FAILED},
    CurationState.CARD_DEDUPING: {
        CurationState.CARD_SELECTING,
        CurationState.READY_FOR_REVIEW,
        CurationState.FAILED,
    },
    CurationState.CARD_SELECTING: {CurationState.CARD_RECONCILING, CurationState.FAILED},
    CurationState.CARD_RECONCILING: {CurationState.READY_FOR_REVIEW, CurationState.FAILED},
    CurationState.BUILDING_LCL: {
        CurationState.RETRIEVING_PASS_1,
        CurationState.FAILED,
    },
    CurationState.RETRIEVING_PASS_1: {
        CurationState.JUDGING_PASS_1,
        CurationState.FAILED,
    },
    CurationState.JUDGING_PASS_1: {
        CurationState.LOCALIZING_MISSED_CONCEPTS,
        CurationState.DEDUPING,
        CurationState.FAILED,
    },
    CurationState.LOCALIZING_MISSED_CONCEPTS: {
        CurationState.RETRIEVING_PASS_2,
        CurationState.FAILED,
    },
    CurationState.RETRIEVING_PASS_2: {
        CurationState.JUDGING_PASS_2,
        CurationState.FAILED,
    },
    CurationState.JUDGING_PASS_2: {
        CurationState.CONVERGING_PASS_3,
        CurationState.AUDITING_CANDIDATES,
        CurationState.DEDUPING,
        CurationState.FAILED,
    },
    CurationState.CONVERGING_PASS_3: {
        CurationState.CONVERGING_PASS_4,
        CurationState.FAILED,
    },
    CurationState.CONVERGING_PASS_4: {
        CurationState.CONVERGING_PASS_5,
        CurationState.FAILED,
    },
    CurationState.CONVERGING_PASS_5: {
        CurationState.AUDITING_CANDIDATES,
        CurationState.FAILED,
    },
    CurationState.AUDITING_CANDIDATES: {
        CurationState.RECOMPUTING_COVERAGE,
        CurationState.FAILED,
    },
    CurationState.RECOMPUTING_COVERAGE: {
        CurationState.DEDUPING,
        CurationState.FAILED,
    },
    CurationState.DEDUPING: {
        CurationState.GENERATING_GAPS,
        CurationState.FAILED,
    },
    CurationState.GENERATING_GAPS: {
        CurationState.RECONCILING,
        CurationState.FAILED,
    },
    CurationState.RECONCILING: {
        CurationState.READY_FOR_REVIEW,
        CurationState.FAILED,
    },
    CurationState.READY_FOR_REVIEW: {
        CurationState.CARD_DEDUPING,
        CurationState.ENVELOPE_PENDING,
    },
    CurationState.ENVELOPE_PENDING: {
        CurationState.APPLYING_LOCAL,
        CurationState.FAILED,
    },
    CurationState.APPLYING_LOCAL: {
        CurationState.SYNCING,
        CurationState.FAILED,
    },
    CurationState.SYNCING: {
        CurationState.VERIFYING,
        CurationState.FAILED,
    },
    CurationState.VERIFYING: {
        CurationState.COMPLETE,
        CurationState.FAILED,
    },
    CurationState.FAILED: {
        CurationState.PREFLIGHT,
        CurationState.BUILDING_SOURCE_INDEX,
        CurationState.BUILDING_LCL,
        CurationState.CARD_BUILDING_LEDGER,
        CurationState.CARD_AUDITING_EVIDENCE,
        CurationState.CARD_SCOPING_TAGS,
        CurationState.CARD_PREFILTERING,
        CurationState.CARD_FAST_CLASSIFYING,
        CurationState.CARD_CLASSIFYING,
        CurationState.CARD_COVERAGE,
        CurationState.CARD_SWEEPING_RESIDUAL,
        CurationState.CARD_GENERATING_GAPS,
        CurationState.CARD_DEDUPING,
        CurationState.CARD_SELECTING,
        CurationState.CARD_RECONCILING,
        CurationState.RETRIEVING_PASS_1,
        CurationState.JUDGING_PASS_1,
        CurationState.LOCALIZING_MISSED_CONCEPTS,
        CurationState.RETRIEVING_PASS_2,
        CurationState.JUDGING_PASS_2,
        CurationState.CONVERGING_PASS_3,
        CurationState.CONVERGING_PASS_4,
        CurationState.CONVERGING_PASS_5,
        CurationState.AUDITING_CANDIDATES,
        CurationState.RECOMPUTING_COVERAGE,
        CurationState.DEDUPING,
        CurationState.GENERATING_GAPS,
        CurationState.RECONCILING,
        CurationState.V3_R0_PREFLIGHT,
        CurationState.V3_R1_SOURCE_INDEX,
        CurationState.V3_R2_FIDELITY,
        CurationState.V3_R3_SCOPE,
        CurationState.V3_R4_INDEX_VERIFICATION,
        CurationState.V3_R5_RETRIEVAL,
        CurationState.V3_R6_CALIBRATION,
        CurationState.V3_R7_CLASSIFICATION,
        CurationState.V3_R8_GAP_CONFIRMATION,
        CurationState.V3_R9_GENERATION,
        CurationState.V3_R10_DEDUPE,
        CurationState.V3_R11_REVIEW,
        CurationState.REMOVED,
    },
}

_INTERRUPTED_PRE_REVIEW_STATES = {
    CurationState.PREFLIGHT,
    CurationState.SNAPSHOTTING_EMBEDDINGS,
    CurationState.BUILDING_COMPANION_INDEX,
    CurationState.BUILDING_SOURCE_INDEX,
    CurationState.BUILDING_LCL,
    CurationState.CARD_BUILDING_LEDGER,
    CurationState.CARD_AUDITING_EVIDENCE,
    CurationState.CARD_SCOPING_TAGS,
    CurationState.CARD_PREFILTERING,
    CurationState.CARD_FAST_CLASSIFYING,
    CurationState.CARD_CLASSIFYING,
    CurationState.CARD_COVERAGE,
    CurationState.CARD_SWEEPING_RESIDUAL,
    CurationState.CARD_GENERATING_GAPS,
    CurationState.CARD_DEDUPING,
    CurationState.CARD_SELECTING,
    CurationState.CARD_RECONCILING,
    CurationState.RETRIEVING_PASS_1,
    CurationState.JUDGING_PASS_1,
    CurationState.LOCALIZING_MISSED_CONCEPTS,
    CurationState.RETRIEVING_PASS_2,
    CurationState.JUDGING_PASS_2,
    CurationState.CONVERGING_PASS_3,
    CurationState.CONVERGING_PASS_4,
    CurationState.CONVERGING_PASS_5,
    CurationState.AUDITING_CANDIDATES,
    CurationState.RECOMPUTING_COVERAGE,
    CurationState.DEDUPING,
    CurationState.GENERATING_GAPS,
    CurationState.RECONCILING,
    CurationState.V3_R0_PREFLIGHT,
    CurationState.V3_R1_SOURCE_INDEX,
    CurationState.V3_R2_FIDELITY,
    CurationState.V3_R3_SCOPE,
    CurationState.V3_R4_INDEX_VERIFICATION,
    CurationState.V3_R5_RETRIEVAL,
    CurationState.V3_R6_CALIBRATION,
    CurationState.V3_R7_CLASSIFICATION,
    CurationState.V3_R8_GAP_CONFIRMATION,
    CurationState.V3_R9_GENERATION,
    CurationState.V3_R10_DEDUPE,
    CurationState.V3_R11_REVIEW,
}

_CLAIMABLE_STATES = {
    CurationState.QUEUED,
    CurationState.PREFLIGHT,
    CurationState.BUILDING_SOURCE_INDEX,
    CurationState.BUILDING_LCL,
    CurationState.CARD_BUILDING_LEDGER,
    CurationState.CARD_AUDITING_EVIDENCE,
    CurationState.CARD_SCOPING_TAGS,
    CurationState.CARD_PREFILTERING,
    CurationState.CARD_FAST_CLASSIFYING,
    CurationState.CARD_CLASSIFYING,
    CurationState.CARD_COVERAGE,
    CurationState.CARD_SWEEPING_RESIDUAL,
    CurationState.CARD_GENERATING_GAPS,
    CurationState.CARD_DEDUPING,
    CurationState.CARD_SELECTING,
    CurationState.CARD_RECONCILING,
    CurationState.RETRIEVING_PASS_1,
    CurationState.JUDGING_PASS_1,
    CurationState.LOCALIZING_MISSED_CONCEPTS,
    CurationState.RETRIEVING_PASS_2,
    CurationState.JUDGING_PASS_2,
    CurationState.CONVERGING_PASS_3,
    CurationState.CONVERGING_PASS_4,
    CurationState.CONVERGING_PASS_5,
    CurationState.AUDITING_CANDIDATES,
    CurationState.RECOMPUTING_COVERAGE,
    CurationState.DEDUPING,
    CurationState.GENERATING_GAPS,
    CurationState.RECONCILING,
    CurationState.V3_R0_PREFLIGHT,
    CurationState.V3_R1_SOURCE_INDEX,
    CurationState.V3_R2_FIDELITY,
    CurationState.V3_R3_SCOPE,
    CurationState.V3_R4_INDEX_VERIFICATION,
    CurationState.V3_R5_RETRIEVAL,
    CurationState.V3_R6_CALIBRATION,
    CurationState.V3_R7_CLASSIFICATION,
    CurationState.V3_R8_GAP_CONFIRMATION,
    CurationState.V3_R9_GENERATION,
    CurationState.V3_R10_DEDUPE,
    CurationState.V3_R11_REVIEW,
}

_RETRY_STATE_BY_STAGE = {
    CurationStage.PREFLIGHT: CurationState.PREFLIGHT,
    CurationStage.SOURCE_INDEX: CurationState.BUILDING_SOURCE_INDEX,
    CurationStage.LCL: CurationState.BUILDING_LCL,
    CurationStage.CARD_LEDGER: CurationState.CARD_BUILDING_LEDGER,
    CurationStage.CARD_EVIDENCE_AUDIT: CurationState.CARD_AUDITING_EVIDENCE,
    CurationStage.CARD_TAG_SCOPE: CurationState.CARD_SCOPING_TAGS,
    CurationStage.CARD_PREFILTER: CurationState.CARD_PREFILTERING,
    CurationStage.CARD_FAST_CLASSIFY: CurationState.CARD_FAST_CLASSIFYING,
    CurationStage.CARD_CLASSIFY: CurationState.CARD_CLASSIFYING,
    CurationStage.CARD_COVERAGE: CurationState.CARD_COVERAGE,
    CurationStage.CARD_RESIDUAL: CurationState.CARD_SWEEPING_RESIDUAL,
    CurationStage.CARD_GAP_FILL: CurationState.CARD_GENERATING_GAPS,
    CurationStage.CARD_SELECTION: CurationState.CARD_SELECTING,
    CurationStage.RETRIEVAL_PASS_1: CurationState.RETRIEVING_PASS_1,
    CurationStage.JUDGMENT_PASS_1: CurationState.JUDGING_PASS_1,
    CurationStage.RESCUE: CurationState.LOCALIZING_MISSED_CONCEPTS,
    CurationStage.RETRIEVAL_PASS_2: CurationState.RETRIEVING_PASS_2,
    CurationStage.JUDGMENT_PASS_2: CurationState.JUDGING_PASS_2,
    CurationStage.CONVERGENCE_PASS_3: CurationState.CONVERGING_PASS_3,
    CurationStage.CONVERGENCE_PASS_4: CurationState.CONVERGING_PASS_4,
    CurationStage.CONVERGENCE_PASS_5: CurationState.CONVERGING_PASS_5,
    CurationStage.CARD_AUDIT: CurationState.AUDITING_CANDIDATES,
    CurationStage.COVERAGE_RECOMPUTE: CurationState.RECOMPUTING_COVERAGE,
    CurationStage.DEDUPE: CurationState.DEDUPING,
    CurationStage.GAPS: CurationState.GENERATING_GAPS,
    CurationStage.RECONCILIATION: CurationState.RECONCILING,
    CurationStage.V3_R0_PREFLIGHT: CurationState.V3_R0_PREFLIGHT,
    CurationStage.V3_R1_SOURCE_INDEX: CurationState.V3_R1_SOURCE_INDEX,
    CurationStage.V3_R2_FIDELITY: CurationState.V3_R2_FIDELITY,
    CurationStage.V3_R3_SCOPE: CurationState.V3_R3_SCOPE,
    CurationStage.V3_R4_INDEX_VERIFICATION: CurationState.V3_R4_INDEX_VERIFICATION,
    CurationStage.V3_R5_RETRIEVAL: CurationState.V3_R5_RETRIEVAL,
    CurationStage.V3_R6_CALIBRATION: CurationState.V3_R6_CALIBRATION,
    CurationStage.V3_R7_CLASSIFICATION: CurationState.V3_R7_CLASSIFICATION,
    CurationStage.V3_R8_GAP_CONFIRMATION: CurationState.V3_R8_GAP_CONFIRMATION,
    CurationStage.V3_R9_GENERATION: CurationState.V3_R9_GENERATION,
    CurationStage.V3_R10_DEDUPE: CurationState.V3_R10_DEDUPE,
    CurationStage.V3_R11_REVIEW: CurationState.V3_R11_REVIEW,
}


_CARD_CENTRIC_REWIND_STAGES = frozenset(
    {
        CurationStage.SOURCE_INDEX,
        CurationStage.CARD_LEDGER,
        CurationStage.CARD_EVIDENCE_AUDIT,
        CurationStage.CARD_TAG_SCOPE,
        CurationStage.CARD_PREFILTER,
        CurationStage.CARD_FAST_CLASSIFY,
        CurationStage.CARD_CLASSIFY,
        CurationStage.CARD_COVERAGE,
        CurationStage.CARD_RESIDUAL,
        CurationStage.CARD_GAP_FILL,
        CurationStage.DEDUPE,
        CurationStage.CARD_SELECTION,
        CurationStage.RECONCILIATION,
    }
)
_BLANK_CARD_CENTRIC_SCOPE_ERROR = "tag scope has no resolved tokens"
_SEMANTIC_DEDUPE_RETRY_HOLD_PREFIX = "Semantic dedupe retry required: "


class InvalidCurationTransition(ValueError):
    """A curation job did not match the required state transition."""


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _validate_provider_event_append(prior: list[str], event: str) -> None:
    """Validate the durable lifecycle for one provider call identity."""
    allowed: dict[tuple[str, ...], set[str]] = {
        (): {"begun"},
        ("begun",): {"dispatched"},
        ("begun", "dispatched"): {"response_received", "transport_failed"},
        ("begun", "dispatched", "response_received"): {
            "accepted",
            "validation_failed",
            "contract_failed",
        },
        # Schema-valid structured output can still fail a stage-level partition
        # contract. Preserve both facts in append-only order.
        ("begun", "dispatched", "response_received", "accepted"): {"contract_failed"},
    }
    if event not in allowed.get(tuple(prior), set()):
        raise ValueError("provider attempt event lifecycle is invalid")


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _configuration_document(
    *,
    block_id: str | None,
    source_revision_ids: Sequence[int],
    source_revision_hashes: dict[int, str],
    summary_outline_id: int | None,
    summary_outline_sha256: str | None,
    deck_allowlist: Sequence[str],
    tag_allowlist: Sequence[str],
    target_deck: str,
    target_tag: str,
    index_snapshot_id: str,
    lcl_prompt_version: str,
    judgment_rubric_version: str,
    gap_prompt_version: str,
    provider: str,
    model: str,
    pipeline_contract_version: PipelineContractVersion,
    model_config_sha256: str,
    semantic_generation: str | None,
    companion_generation: str | None,
    policy_sha256: str | None = None,
    rate_table_sha256: str | None = None,
) -> dict[str, Any]:
    """Return the complete canonical job configuration used for provenance."""
    document = {
        "block_id": block_id,
        "source_revision_ids": tuple(source_revision_ids),
        "source_revision_hashes": source_revision_hashes,
        "summary_outline_id": summary_outline_id,
        "summary_outline_sha256": summary_outline_sha256,
        "deck_allowlist": tuple(deck_allowlist),
        "tag_allowlist": tuple(tag_allowlist),
        "target_deck": target_deck,
        "target_tag": target_tag,
        "index_snapshot_id": index_snapshot_id,
        "lcl_prompt_version": lcl_prompt_version,
        "judgment_rubric_version": judgment_rubric_version,
        "gap_prompt_version": gap_prompt_version,
        "provider": provider,
        "model": model,
        "pipeline_contract_version": pipeline_contract_version.value,
        "model_config_sha256": model_config_sha256,
        "semantic_generation": semantic_generation,
        "companion_generation": companion_generation,
    }
    if pipeline_contract_version is PipelineContractVersion.CARD_CENTRIC_V3:
        document["policy_sha256"] = policy_sha256
        document["rate_table_sha256"] = rate_table_sha256
    return document


def _configuration_sha256(configuration: dict[str, Any]) -> str:
    return _sha256_text(_canonical_json(configuration))


def _aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("job timestamps must be timezone-aware")
    return value.astimezone(UTC)


def _same_unique_identity_set(
    provided: Sequence[object],
    frozen: Sequence[object],
) -> bool:
    """Accept storage/request ordering changes, never identity duplication or drift."""
    return (
        len(provided) == len(frozen)
        and len(provided) == len(set(provided))
        and len(frozen) == len(set(frozen))
        and set(provided) == set(frozen)
    )


def _is_sha256(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(char in "0123456789abcdef" for char in value)
    )


def is_semantic_dedupe_retry_hold(job: CurationJob) -> bool:
    """Whether a ready job is an S8 outage hold, not a reviewable result."""
    return job.state is CurationState.READY_FOR_REVIEW and _is_semantic_dedupe_retry_hold(job.error)


def _is_semantic_dedupe_retry_hold(error: str | None) -> bool:
    return bool(error and error.startswith(_SEMANTIC_DEDUPE_RETRY_HOLD_PREFIX))


class AnkiCurationRepository:
    def __init__(
        self,
        database: Database,
        *,
        supported_envelope_versions: frozenset[int] | None = None,
    ) -> None:
        self.database = database
        self.supported_envelope_versions = supported_envelope_versions

    def create_job(self, request: CreateCurationJob) -> CurationJob:
        if request.pipeline_contract_version is PipelineContractVersion.CARD_CENTRIC_V3:
            if not request.offline_replay_only and not self.allows_v3_live_capture():
                raise ValueError("card_centric_v3 is offline-replay-only")
            if not _is_sha256(request.policy_sha256):
                raise ValueError("card_centric_v3 requires an exact policy pin")
            assert request.policy_sha256 is not None
            if set(request.source_revision_hashes) != set(request.source_revision_ids) or not all(
                _is_sha256(value) for value in request.source_revision_hashes.values()
            ):
                raise ValueError("card_centric_v3 requires exact source revision hash pins")
            if not request.companion_generation or not request.semantic_generation:
                raise ValueError("card_centric_v3 requires companion and semantic generation pins")
            self.get_policy_by_sha256(request.policy_sha256)
            if request.resolved_model_config is None or not all(
                (
                    request.resolved_model_config.scope_r3,
                    request.resolved_model_config.cheap_classify_r7,
                    request.resolved_model_config.thorough_classify_r7,
                    request.resolved_model_config.generation_r9,
                )
            ):
                raise ValueError("card_centric_v3 requires complete pinned model routes")
            try:
                table = FrozenRateTable.from_document(
                    cast(dict[str, object], request.rate_table_document)
                )
            except (TypeError, ValueError) as exc:
                raise ValueError("card_centric_v3 requires a complete frozen rate table") from exc
        else:
            table = None
        if (
            request.pipeline_contract_version
            in {PipelineContractVersion.CARD_CENTRIC_V1, PipelineContractVersion.CARD_CENTRIC_V2}
            and not request.tag_allowlist
        ):
            with self.database.session() as session:
                lecture = session.get(LectureModel, request.lecture_id)
                if lecture is None:
                    raise KeyError(request.lecture_id)
                request = replace(
                    request,
                    tag_allowlist=resolve_card_centric_scope(
                        tag_allowlist=(), subject=lecture.subject, topic=lecture.topic
                    ),
                )
        model_config = request.resolved_model_config or (
            ResolvedModelConfiguration.card_centric_default(
                request.provider,
                request.model,
            )
            if request.pipeline_contract_version is PipelineContractVersion.CARD_CENTRIC_V1
            else ResolvedModelConfiguration.card_centric_v2_default(request.provider, request.model)
            if request.pipeline_contract_version is PipelineContractVersion.CARD_CENTRIC_V2
            else ResolvedModelConfiguration.legacy(request.provider, request.model)
        )
        if request.pipeline_contract_version in {
            PipelineContractVersion.CARD_CENTRIC_V1,
            PipelineContractVersion.CARD_CENTRIC_V2,
        } and (
            model_config.classify_s4.thinking_mode != "disabled"
            or model_config.residual_s6.thinking_mode != "disabled"
        ):
            raise ValueError("card-centric S4/S6 thinking must be disabled")
        if request.pipeline_contract_version is PipelineContractVersion.CARD_CENTRIC_V2:
            model_config.require_card_centric_v2_fast_classifier()
            if model_config.classifier_execution is None:
                model_config = replace(
                    model_config,
                    classifier_execution=model_config.resolved_classifier_execution(),
                )
        model_config_json = _canonical_json(model_config.canonical_document())
        model_config_sha256 = _sha256_text(model_config_json)
        configuration = _configuration_document(
            block_id=request.block_id,
            source_revision_ids=request.source_revision_ids,
            source_revision_hashes=request.source_revision_hashes,
            summary_outline_id=request.summary_outline_id,
            summary_outline_sha256=request.summary_outline_sha256,
            deck_allowlist=request.deck_allowlist,
            tag_allowlist=request.tag_allowlist,
            target_deck=request.target_deck,
            target_tag=request.target_tag,
            index_snapshot_id=request.index_snapshot_id,
            lcl_prompt_version=request.lcl_prompt_version,
            judgment_rubric_version=request.judgment_rubric_version,
            gap_prompt_version=request.gap_prompt_version,
            provider=request.provider,
            model=request.model,
            pipeline_contract_version=request.pipeline_contract_version,
            model_config_sha256=model_config_sha256,
            semantic_generation=request.semantic_generation,
            companion_generation=request.companion_generation,
            policy_sha256=request.policy_sha256,
            rate_table_sha256=None if table is None else table.rate_table_sha256,
        )
        with self.database.session() as session:
            lecture = session.get(LectureModel, request.lecture_id)
            if lecture is None:
                raise KeyError(request.lecture_id)
            pinned_lecture = (
                self._pinned_lecture_metadata(lecture)
                if request.pipeline_contract_version is PipelineContractVersion.CARD_CENTRIC_V2
                else None
            )
            stored = AnkiCurationJobModel(
                id=str(uuid4()),
                lecture_id=request.lecture_id,
                lecture_title_snapshot=(None if pinned_lecture is None else pinned_lecture.title),
                lecture_metadata_json=(
                    None if pinned_lecture is None else pinned_lecture.metadata.canonical_json
                ),
                lecture_metadata_sha256=(
                    None if pinned_lecture is None else pinned_lecture.metadata_sha256
                ),
                state=CurationState.QUEUED.value,
                target_deck=request.target_deck,
                target_tag=request.target_tag,
                index_snapshot_id=request.index_snapshot_id,
                _legacy_amboss_input="",
                _legacy_amboss_sha256=_sha256_text(""),
                block_id=request.block_id,
                source_revision_ids_json=_canonical_json(request.source_revision_ids),
                source_revision_hashes_json=_canonical_json(request.source_revision_hashes),
                summary_outline_id=request.summary_outline_id,
                summary_outline_sha256=request.summary_outline_sha256,
                deck_allowlist_json=_canonical_json(request.deck_allowlist),
                tag_allowlist_json=_canonical_json(request.tag_allowlist),
                provider=request.provider,
                model=request.model,
                pipeline_contract_version=request.pipeline_contract_version.value,
                resolved_model_config_json=model_config_json,
                model_config_sha256=model_config_sha256,
                semantic_generation=request.semantic_generation,
                companion_generation=request.companion_generation,
                configuration_sha256=_configuration_sha256(configuration),
                policy_sha256=request.policy_sha256,
                v3_rate_table_json=None if table is None else _canonical_json(table.document()),
                v3_rate_table_sha256=None if table is None else table.rate_table_sha256,
                offline_replay_only=request.offline_replay_only,
                apply_state=ApplyState.PENDING.value,
                instruction_text=request.instruction_text,
                instruction_sha256=_sha256_text(request.instruction_text),
                lcl_prompt_version=request.lcl_prompt_version,
                judgment_rubric_version=request.judgment_rubric_version,
                gap_prompt_version=request.gap_prompt_version,
            )
            session.add(stored)
            session.flush()
            return self._job(stored)

    def create_policy_revision(self, policy: CourseCurationPolicy) -> CourseCurationPolicy:
        """Append a policy revision, allowing only an exact idempotent retry."""
        payload_json = _canonical_json(policy.canonical_payload())
        with self.database.session() as session:
            existing = session.scalar(
                select(CourseCurationPolicyModel).where(
                    CourseCurationPolicyModel.policy_id == policy.policy_id,
                    CourseCurationPolicyModel.revision == policy.revision,
                )
            )
            if existing is not None:
                if (
                    existing.policy_sha256 == policy.policy_sha256
                    and existing.payload_json == payload_json
                ):
                    return self._course_policy(existing)
                raise ValueError(
                    "course policy revision identity already exists with different payload"
                )
            session.add(
                CourseCurationPolicyModel(
                    policy_id=policy.policy_id,
                    revision=policy.revision,
                    payload_json=payload_json,
                    policy_sha256=policy.policy_sha256,
                )
            )
            session.flush()
            return policy

    def get_policy_revision(self, policy_id: str, revision: int) -> CourseCurationPolicy:
        with self.database.session() as session:
            stored = session.scalar(
                select(CourseCurationPolicyModel).where(
                    CourseCurationPolicyModel.policy_id == policy_id,
                    CourseCurationPolicyModel.revision == revision,
                )
            )
            if stored is None:
                raise KeyError((policy_id, revision))
            return self._course_policy(stored)

    def allows_v3_live_capture(self) -> bool:
        """Ordinary repositories never authorize live v3 execution."""
        return False

    def get_policy_by_sha256(self, policy_sha256: str) -> CourseCurationPolicy:
        """Return the one immutable policy revision pinned by a v3 job."""
        if not _is_sha256(policy_sha256):
            raise ValueError("policy SHA-256 is invalid")
        with self.database.session() as session:
            rows = list(
                session.scalars(
                    select(CourseCurationPolicyModel).where(
                        CourseCurationPolicyModel.policy_sha256 == policy_sha256
                    )
                )
            )
            if len(rows) != 1:
                raise KeyError("pinned course policy is unavailable or ambiguous")
            return self._course_policy(rows[0])

    def list_policy_revisions(self, policy_id: str) -> tuple[CourseCurationPolicy, ...]:
        with self.database.session() as session:
            rows = session.scalars(
                select(CourseCurationPolicyModel)
                .where(CourseCurationPolicyModel.policy_id == policy_id)
                .order_by(CourseCurationPolicyModel.revision)
            ).all()
            return tuple(self._course_policy(row) for row in rows)

    def card_centric_profile(self) -> ResolvedModelConfiguration | None:
        """The local Study Hub has one signed-in operator; this is that user's default."""
        with self.database.session() as session:
            stored = session.get(AnkiStageSettingModel, "card_centric_profile")
            if stored is None:
                return None
            try:
                return self._resolved_model_config(
                    stored.options_json,
                    stored.provider,
                    stored.model,
                )
            except (TypeError, ValueError):
                return None

    def save_card_centric_profile(self, value: ResolvedModelConfiguration) -> None:
        document = _canonical_json(value.canonical_document())
        with self.database.session() as session:
            stored = session.get(AnkiStageSettingModel, "card_centric_profile")
            if stored is None:
                stored = AnkiStageSettingModel(
                    stage="card_centric_profile",
                    provider=value.ledger_s2.provider,
                    model=value.ledger_s2.model,
                    enabled=True,
                    options_json=document,
                )
                session.add(stored)
            else:
                stored.provider = value.ledger_s2.provider
                stored.model = value.ledger_s2.model
                stored.enabled = True
                stored.options_json = document

    def save_fixture_validation(self, provider: str, model: str, record: dict[str, Any]) -> None:
        key = f"card_centric_fixture:{provider}:{model}"
        with self.database.session() as session:
            stored = session.get(AnkiStageSettingModel, key)
            if stored is None:
                session.add(
                    AnkiStageSettingModel(
                        stage=key,
                        provider=provider,
                        model=model,
                        enabled=True,
                        options_json=_canonical_json(record),
                    )
                )
            else:
                stored.enabled = True
                stored.options_json = _canonical_json(record)

    def fixture_validation(self, provider: str, model: str) -> dict[str, Any] | None:
        with self.database.session() as session:
            stored = session.get(AnkiStageSettingModel, f"card_centric_fixture:{provider}:{model}")
            if stored is None or not stored.enabled:
                return None
            try:
                return cast(dict[str, Any], json.loads(stored.options_json))
            except (TypeError, ValueError):
                return None

    def issue_card_centric_overflow_acknowledgement(
        self,
        job_id: UUID,
        *,
        review_revision: int,
        selected_note_ids: tuple[int, ...],
        selected_generated_ids: tuple[str, ...],
        mandatory_note_ids: tuple[int, ...],
        mandatory_generated_ids: tuple[str, ...],
        cap: int,
    ) -> dict[str, Any]:
        """Persist an HMAC acknowledgement bound to the frozen full selection."""
        with self.database.session() as session:
            job = self._require_job_model(session, job_id)
            if job.review_revision != review_revision:
                raise ValueError("review revision is stale")
            selection_order: tuple[str, ...] = ()
            mandatory_count = len(selected_note_ids) + len(selected_generated_ids)
            if job.pipeline_contract_version == PipelineContractVersion.CARD_CENTRIC_V2.value:
                (
                    frozen_notes,
                    frozen_generated,
                    selection_order,
                    frozen_cap,
                    overflow_notes,
                    overflow_generated,
                ) = self._v2_overflow_selection_proof(session, job_id, review_revision)
                if (
                    not _same_unique_identity_set(selected_note_ids, frozen_notes)
                    or not _same_unique_identity_set(selected_generated_ids, frozen_generated)
                    or not _same_unique_identity_set(mandatory_note_ids, overflow_notes)
                    or not _same_unique_identity_set(mandatory_generated_ids, overflow_generated)
                    or cap != frozen_cap
                ):
                    raise ValueError(
                        "overflow acknowledgement must bind the frozen full selection "
                        "and overflow slice"
                    )
                mandatory_count = len(overflow_notes) + len(overflow_generated)
            elif set(selected_note_ids) != set(mandatory_note_ids):
                raise ValueError(
                    "overflow acknowledgement must bind the exact mandatory overflow set"
                )
            secret_setting = session.get(AnkiStageSettingModel, "card_centric_review_secret")
            if secret_setting is None:
                secret_setting = AnkiStageSettingModel(
                    stage="card_centric_review_secret",
                    provider="local",
                    model="hmac-sha256",
                    enabled=True,
                    options_json=_canonical_json({"secret": uuid4().hex + uuid4().hex}),
                )
                session.add(secret_setting)
                session.flush()
            secret = str(cast(dict[str, Any], json.loads(secret_setting.options_json))["secret"])
            acknowledgement = issue_acknowledgement(
                secret,
                job_id=job_id,
                review_revision=review_revision,
                selected_note_ids=selected_note_ids,
                selected_generated_ids=selected_generated_ids,
                mandatory_count=mandatory_count,
                cap=cap,
                pipeline_contract_version=job.pipeline_contract_version,
                model_config_sha256=job.model_config_sha256,
                selection_order=selection_order,
            )
            document = acknowledgement.document()
            session.add(
                AnkiStageSettingModel(
                    stage=f"card_centric_ack:{acknowledgement.token}",
                    provider="local",
                    model="hmac-sha256",
                    enabled=True,
                    options_json=_canonical_json(document),
                )
            )
            return document

    def validate_card_centric_overflow_acknowledgement(
        self,
        job_id: UUID,
        *,
        review_revision: int,
        selected_note_ids: tuple[int, ...],
        selected_generated_ids: tuple[str, ...],
        cap: int,
        document: dict[str, Any],
    ) -> bool:
        """Accept only the exact server-issued document for this frozen revision."""
        token = str(document.get("token", ""))
        if not token:
            return False
        with self.database.session() as session:
            job = self._require_job_model(session, job_id)
            stored = session.get(AnkiStageSettingModel, f"card_centric_ack:{token}")
            secret_setting = session.get(AnkiStageSettingModel, "card_centric_review_secret")
            if stored is None or secret_setting is None:
                return False
            try:
                persisted = cast(dict[str, Any], json.loads(stored.options_json))
                acknowledgement = OverflowAcknowledgement(
                    token=str(persisted["token"]),
                    job_id=UUID(str(persisted["job_id"])),
                    review_revision=int(persisted["review_revision"]),
                    selection_digest=str(persisted["selection_digest"]),
                    mandatory_count=int(persisted["mandatory_count"]),
                    cap=int(persisted["cap"]),
                    pipeline_contract_version=str(persisted["pipeline_contract_version"]),
                    model_config_sha256=str(persisted["model_config_sha256"]),
                    signature=str(persisted["signature"]),
                )
                secret = str(
                    cast(dict[str, Any], json.loads(secret_setting.options_json))["secret"]
                )
            except (KeyError, TypeError, ValueError):
                return False
            selection_order: tuple[str, ...] = ()
            mandatory_count = len(selected_note_ids) + len(selected_generated_ids)
            if job.pipeline_contract_version == PipelineContractVersion.CARD_CENTRIC_V2.value:
                try:
                    (
                        frozen_notes,
                        frozen_generated,
                        selection_order,
                        frozen_cap,
                        overflow_notes,
                        overflow_generated,
                    ) = self._v2_overflow_selection_proof(session, job_id, review_revision)
                except (KeyError, TypeError, ValueError):
                    return False
                if (
                    not _same_unique_identity_set(selected_note_ids, frozen_notes)
                    or not _same_unique_identity_set(selected_generated_ids, frozen_generated)
                    or cap != frozen_cap
                ):
                    return False
                mandatory_count = len(overflow_notes) + len(overflow_generated)
            return (
                persisted == document
                and verify_acknowledgement(secret, acknowledgement)
                and acknowledgement.job_id == job_id
                and acknowledgement.review_revision == review_revision
                and acknowledgement.selection_digest
                == selection_digest(
                    selected_note_ids,
                    selected_generated_ids,
                    selection_order=selection_order,
                )
                and acknowledgement.mandatory_count == mandatory_count
                and acknowledgement.cap == cap
                and acknowledgement.pipeline_contract_version == job.pipeline_contract_version
                and acknowledgement.model_config_sha256 == job.model_config_sha256
            )

    def _v2_overflow_selection_proof(
        self,
        session: Session,
        job_id: UUID,
        review_revision: int,
    ) -> tuple[
        tuple[int, ...],
        tuple[str, ...],
        tuple[str, ...],
        int,
        tuple[int, ...],
        tuple[str, ...],
    ]:
        """Derive the only V2 acknowledgement scope from persisted S9 metadata."""
        stored = session.scalar(
            select(AnkiReviewedReconciliationModel).where(
                AnkiReviewedReconciliationModel.job_id == str(job_id),
                AnkiReviewedReconciliationModel.review_revision == review_revision,
            )
        )
        if stored is None:
            raise ValueError("current reviewed reconciliation is unavailable")
        payload = cast(dict[str, Any], json.loads(stored.payload_json))
        snapshot = CardCentricReconciliationInput.model_validate(payload["snapshot"])
        metadata = tuple(
            sorted(snapshot.selection_metadata, key=lambda item: item.selected_position)
        )
        total = len(snapshot.selected_nids) + len(snapshot.selected_generated_card_ids)
        expected_identities = {
            *(f"existing:{note_id}" for note_id in snapshot.selected_nids),
            *(f"generated:{card_id}" for card_id in snapshot.selected_generated_card_ids),
        }
        if (
            total <= snapshot.cap
            or len(metadata) != total
            or [item.selected_position for item in metadata] != list(range(1, total + 1))
            or {item.identity for item in metadata} != expected_identities
            or tuple(item.identity for item in metadata) != snapshot.selection_order
        ):
            raise ValueError("persisted V2 selection metadata cannot prove overflow scope")
        overflow = tuple(item for item in metadata if item.selected_position > snapshot.cap)
        if len(overflow) != total - snapshot.cap or any(
            not item.mandatory
            or not item.overflow_reason
            or not item.overflow_reason.strip()
            or not item.manual_acknowledgement_required
            for item in overflow
        ):
            raise ValueError("persisted V2 overflow slice is not mandatory and review-ready")
        overflow_notes = tuple(
            int(item.identity.removeprefix("existing:"))
            for item in overflow
            if item.identity.startswith("existing:")
        )
        overflow_generated = tuple(
            item.identity.removeprefix("generated:")
            for item in overflow
            if item.identity.startswith("generated:")
        )
        return (
            snapshot.selected_nids,
            snapshot.selected_generated_card_ids,
            snapshot.selection_order,
            snapshot.cap,
            overflow_notes,
            overflow_generated,
        )

    def reviewed_reconciliation(self, job_id: UUID, review_revision: int) -> dict[str, Any] | None:
        with self.database.session() as session:
            stored = session.scalar(
                select(AnkiReviewedReconciliationModel).where(
                    AnkiReviewedReconciliationModel.job_id == str(job_id),
                    AnkiReviewedReconciliationModel.review_revision == review_revision,
                )
            )
            return None if stored is None else cast(dict[str, Any], json.loads(stored.payload_json))

    def prepare_stage_replay_inputs(
        self, job_id: UUID, stage: CurationStage
    ) -> PreparedStageReplayInputs:
        """Atomically freeze and return the exact replay document for one stage.

        The unique ``(job_id, stage)`` row is the first-write-wins boundary:
        reviews or lecture edits committed after this method returns can never
        modify the returned document or a later reload of it.
        """
        with self.database.session() as session:
            job = self._require_job_model(session, job_id)
            existing = session.scalar(
                select(AnkiStageReplayInputModel).where(
                    AnkiStageReplayInputModel.job_id == str(job_id),
                    AnkiStageReplayInputModel.stage == stage.value,
                )
            )
            if existing is not None:
                return self._prepared_stage_replay_inputs(existing)

            document = self._stage_replay_document(session, job, job_id, stage)
            serialized = canonical_json(document)
            prepared = AnkiStageReplayInputModel(
                job_id=str(job_id),
                stage=stage.value,
                canonical_json=serialized,
                sha256=_sha256_text(serialized),
            )
            try:
                # A savepoint lets a competing first writer resolve to its
                # already-committed immutable input rather than leaking a
                # uniqueness exception to a retrying worker.
                with session.begin_nested():
                    session.add(prepared)
                    session.flush()
            except IntegrityError:
                existing = session.scalar(
                    select(AnkiStageReplayInputModel).where(
                        AnkiStageReplayInputModel.job_id == str(job_id),
                        AnkiStageReplayInputModel.stage == stage.value,
                    )
                )
                if existing is None:  # pragma: no cover - database isolation-specific retry path
                    raise
                return self._prepared_stage_replay_inputs(existing)
            return self._prepared_stage_replay_inputs(prepared)

    def card_centric_yes_rate_history(self, job_id: UUID, *, limit: int = 12) -> tuple[float, ...]:
        """Return one latest valid reviewed revision per prior v2 job, newest first.

        This compatibility reader is deliberately distinct-job based.  Replay
        code must call :meth:`prepare_stage_replay_inputs` and use the frozen
        reconciliation document instead of this live convenience reader.
        """
        if limit < 1:
            raise ValueError("history limit must be positive")
        with self.database.session() as session:
            job = self._require_job_model(session, job_id)
            history = self._a11_history_snapshot(session, job_id, job.lecture_id, limit=limit)
            return tuple(entry.yes_rate for entry in history.entries)

    def persist_card_centric_overflow_acknowledgement(
        self, job_id: UUID, *, review_revision: int, document: dict[str, Any]
    ) -> None:
        """Attach a server-issued acknowledgement to its exact reviewed S9 row."""
        with self.database.session() as session:
            job = self._require_job_model(session, job_id)
            if job.review_revision != review_revision:
                raise ValueError("review revision is stale")
            stored = session.scalar(
                select(AnkiReviewedReconciliationModel).where(
                    AnkiReviewedReconciliationModel.job_id == str(job_id),
                    AnkiReviewedReconciliationModel.review_revision == review_revision,
                )
            )
            if stored is None:
                raise ValueError("current reviewed reconciliation is unavailable")
            payload = cast(dict[str, Any], json.loads(stored.payload_json))
            snapshot = CardCentricReconciliationInput.model_validate(payload["snapshot"])
            if not self.validate_card_centric_overflow_acknowledgement(
                job_id,
                review_revision=review_revision,
                selected_note_ids=snapshot.selected_nids,
                selected_generated_ids=snapshot.selected_generated_card_ids,
                cap=snapshot.cap,
                document=document,
            ):
                raise ValueError("selection overflow acknowledgement is missing, stale, or forged")
            reviewed = snapshot.model_copy(update={"overflow_acknowledgement": document})
            report = reconcile_card_centric(reviewed)
            payload.update(report.model_dump(mode="json"))
            payload["snapshot"] = reviewed.model_dump(mode="json")
            payload["selection"] = {
                **cast(dict[str, Any], payload.get("selection", {})),
                "overflow_acknowledgement": document,
            }
            stored.payload_json = _canonical_json(payload)

    def validate_card_centric_envelope_acknowledgement(self, envelope_id: UUID) -> bool:
        """Run before the coordinator syncs or mutates Anki."""
        with self.database.session() as session:
            row = session.get(AnkiEnvelopeModel, str(envelope_id))
            if row is None:
                return False
            try:
                envelope = parse_action_envelope(row.payload_json)
            except ValueError:
                return False
            if not isinstance(envelope, ActionEnvelopeV2):
                return True
            job = self._require_job_model(session, UUID(row.job_id))
            if job.pipeline_contract_version == PipelineContractVersion.CARD_CENTRIC_V3.value:
                # Phase G persists a revision-bound approval seam only; R12
                # must stop here, before the coordinator can contact Anki.
                return False
            if envelope.review_revision != job.review_revision:
                return False
            reviewed = session.scalar(
                select(AnkiReviewedReconciliationModel).where(
                    AnkiReviewedReconciliationModel.job_id == row.job_id,
                    AnkiReviewedReconciliationModel.review_revision == envelope.review_revision,
                )
            )
            if reviewed is None:
                return False
            try:
                snapshot = CardCentricReconciliationInput.model_validate(
                    json.loads(reviewed.payload_json)["snapshot"]
                )
            except (KeyError, TypeError, ValueError):
                return False
            total = len(snapshot.selected_nids) + len(snapshot.selected_generated_card_ids)
            if total <= snapshot.cap:
                return envelope.overflow_acknowledgement_provenance == {"required": False}
            document = snapshot.overflow_acknowledgement
            return bool(
                document
                and document == envelope.overflow_acknowledgement_provenance
                and self.validate_card_centric_overflow_acknowledgement(
                    UUID(row.job_id),
                    review_revision=envelope.review_revision,
                    selected_note_ids=snapshot.selected_nids,
                    selected_generated_ids=snapshot.selected_generated_card_ids,
                    cap=snapshot.cap,
                    document=document,
                )
            )

    @staticmethod
    def _valid_v3_envelope(
        session: Session, job: AnkiCurationJobModel, envelope: ActionEnvelopeV2
    ) -> bool:
        if envelope.pipeline_contract_version != PipelineContractVersion.CARD_CENTRIC_V3.value:
            return False
        reviewed = session.scalar(
            select(AnkiReviewedReconciliationModel).where(
                AnkiReviewedReconciliationModel.job_id == job.id,
                AnkiReviewedReconciliationModel.review_revision == envelope.review_revision,
            )
        )
        if reviewed is None:
            return False
        try:
            payload = cast(dict[str, Any], json.loads(reviewed.payload_json))
            reconciliation = V3ReviewReconciliation.model_validate(
                {
                    key: payload[key]
                    for key in (
                        "snapshot",
                        "can_render_envelope",
                        "findings",
                        "reconciliation_sha256",
                    )
                }
            )
        except (KeyError, TypeError, ValueError):
            return False
        snapshot = reconciliation.snapshot
        return bool(
            reconciliation.can_render_envelope
            and envelope.policy_sha256 == snapshot.policy_sha256
            and envelope.scope_sha256 == snapshot.scope_sha256
            and envelope.r11_snapshot_sha256 == snapshot.snapshot_sha256
            and envelope.r11_artifact_sha256 == payload.get("r11_artifact_sha256")
            and envelope.rate_table_sha256 == snapshot.rate_table_sha256
            and envelope.cost_ledger_sha256 == payload.get("cost_ledger_sha256")
            and envelope.review_revision == job.review_revision
        )

    def require_job(self, job_id: UUID) -> CurationJob:
        with self.database.session() as session:
            stored = session.get(AnkiCurationJobModel, str(job_id))
            if stored is None:
                raise KeyError(str(job_id))
            return self._job(stored)

    def lecture_title(self, lecture_id: int) -> str:
        with self.database.session() as session:
            lecture = session.get(LectureModel, lecture_id)
            if lecture is None:
                raise KeyError(lecture_id)
            return (
                f"{lecture.subject} Exam {lecture.exam_number} "
                f"Lecture {lecture.lecture_number}: {lecture.topic}"
            )

    def list_jobs(self, *, limit: int = 100) -> list[CurationJob]:
        if not 1 <= limit <= 500:
            raise ValueError("job list limit must be between 1 and 500")
        with self.database.session() as session:
            stored = session.scalars(
                select(AnkiCurationJobModel)
                .where(AnkiCurationJobModel.state != CurationState.REMOVED.value)
                .order_by(
                    AnkiCurationJobModel.created_at.desc(),
                    AnkiCurationJobModel.id.desc(),
                )
                .limit(limit)
            ).all()
            return [self._job(job) for job in stored]

    def claim_next_job(
        self,
        now: datetime,
        *,
        worker_id: str = "legacy-worker",
        lease_seconds: int = 60,
    ) -> CurationJob | None:
        worker_id = worker_id.strip()
        if not worker_id or len(worker_id) > 100:
            raise ValueError("worker ID is invalid")
        if lease_seconds < 1:
            raise ValueError("lease duration must be positive")
        now = _aware_utc(now)
        now_text = now.isoformat()
        lease_expires_at = (now + timedelta(seconds=lease_seconds)).isoformat()
        with self.database.session() as session:
            stored = session.scalar(
                select(AnkiCurationJobModel)
                .where(
                    AnkiCurationJobModel.state.in_([state.value for state in _CLAIMABLE_STATES]),
                    or_(
                        AnkiCurationJobModel.available_at.is_(None),
                        AnkiCurationJobModel.available_at <= now_text,
                    ),
                    or_(
                        AnkiCurationJobModel.lease_expires_at.is_(None),
                        AnkiCurationJobModel.lease_expires_at <= now_text,
                    ),
                )
                .order_by(AnkiCurationJobModel.created_at, AnkiCurationJobModel.id)
                .limit(1)
            )
            if stored is None:
                return None
            queued = stored.state == CurationState.QUEUED.value
            claimed = session.execute(
                update(AnkiCurationJobModel)
                .where(
                    AnkiCurationJobModel.id == stored.id,
                    AnkiCurationJobModel.state == stored.state,
                    or_(
                        AnkiCurationJobModel.lease_expires_at.is_(None),
                        AnkiCurationJobModel.lease_expires_at <= now_text,
                    ),
                )
                .values(
                    state=(
                        CurationState.V3_R0_PREFLIGHT.value
                        if queued
                        and stored.pipeline_contract_version
                        == PipelineContractVersion.CARD_CENTRIC_V3.value
                        else CurationState.PREFLIGHT.value
                        if queued
                        else stored.state
                    ),
                    attempts=(
                        AnkiCurationJobModel.attempts + 1
                        if queued
                        else AnkiCurationJobModel.attempts
                    ),
                    started_at=now_text if queued else stored.started_at,
                    error=None,
                    lease_owner=worker_id,
                    lease_expires_at=lease_expires_at,
                    available_at=None,
                )
            )
            if cast(CursorResult[Any], claimed).rowcount != 1:
                return None
            session.flush()
            session.refresh(stored)
            return self._job(stored)

    def renew_lease(
        self,
        job_id: UUID,
        worker_id: str,
        now: datetime,
        *,
        lease_seconds: int,
    ) -> bool:
        if lease_seconds < 1:
            raise ValueError("lease duration must be positive")
        expires = (_aware_utc(now) + timedelta(seconds=lease_seconds)).isoformat()
        now_text = _aware_utc(now).isoformat()
        with self.database.session() as session:
            changed = session.execute(
                update(AnkiCurationJobModel)
                .where(
                    AnkiCurationJobModel.id == str(job_id),
                    AnkiCurationJobModel.lease_owner == worker_id,
                    AnkiCurationJobModel.lease_expires_at.is_not(None),
                    AnkiCurationJobModel.lease_expires_at > now_text,
                    AnkiCurationJobModel.state.in_([state.value for state in _CLAIMABLE_STATES]),
                )
                .values(lease_expires_at=expires)
            )
            return cast(CursorResult[Any], changed).rowcount == 1

    def release_lease(self, job_id: UUID, worker_id: str) -> bool:
        with self.database.session() as session:
            changed = session.execute(
                update(AnkiCurationJobModel)
                .where(
                    AnkiCurationJobModel.id == str(job_id),
                    AnkiCurationJobModel.lease_owner == worker_id,
                )
                .values(
                    lease_owner=None,
                    lease_expires_at=None,
                )
            )
            return cast(CursorResult[Any], changed).rowcount == 1

    def defer_job(
        self,
        job_id: UUID,
        worker_id: str,
        safe_error: str,
        *,
        expected_state: CurationState,
        available_at: datetime,
        now: datetime,
    ) -> CurationJob:
        if expected_state not in _CLAIMABLE_STATES:
            raise ValueError("deferred job state must be claimable")
        now_text = _aware_utc(now).isoformat()
        with self.database.session() as session:
            changed = session.execute(
                update(AnkiCurationJobModel)
                .where(
                    AnkiCurationJobModel.id == str(job_id),
                    AnkiCurationJobModel.lease_owner == worker_id,
                    AnkiCurationJobModel.lease_expires_at.is_not(None),
                    AnkiCurationJobModel.lease_expires_at > now_text,
                    AnkiCurationJobModel.state == expected_state.value,
                )
                .values(
                    error=safe_error[:1_000],
                    available_at=_aware_utc(available_at).isoformat(),
                    lease_owner=None,
                    lease_expires_at=None,
                )
            )
            if cast(CursorResult[Any], changed).rowcount != 1:
                stored = self._require_job_model(session, job_id)
                if stored.state != expected_state.value:
                    raise InvalidCurationTransition(
                        f"job {job_id} is not in {expected_state.value}"
                    )
                self._require_active_stage_lease(stored, job_id, worker_id, now=now)
                raise InvalidCurationTransition(f"job {job_id} is not claimable")
            stored = self._require_job_model(session, job_id)
            session.refresh(stored)
            return self._job(stored)

    def fail_job(
        self,
        job_id: UUID,
        worker_id: str,
        safe_error: str,
        *,
        expected_state: CurationState,
        now: datetime,
    ) -> CurationJob:
        if expected_state not in _CLAIMABLE_STATES:
            raise ValueError("failed job state must be claimable")
        now_text = _aware_utc(now).isoformat()
        with self.database.session() as session:
            changed = session.execute(
                update(AnkiCurationJobModel)
                .where(
                    AnkiCurationJobModel.id == str(job_id),
                    AnkiCurationJobModel.lease_owner == worker_id,
                    AnkiCurationJobModel.lease_expires_at.is_not(None),
                    AnkiCurationJobModel.lease_expires_at > now_text,
                    AnkiCurationJobModel.state == expected_state.value,
                )
                .values(
                    state=CurationState.FAILED.value,
                    error=safe_error[:1_000],
                    available_at=None,
                    lease_owner=None,
                    lease_expires_at=None,
                )
            )
            if cast(CursorResult[Any], changed).rowcount != 1:
                stored = self._require_job_model(session, job_id)
                if stored.state != expected_state.value:
                    raise InvalidCurationTransition(
                        f"job {job_id} is not in {expected_state.value}"
                    )
                self._require_active_stage_lease(stored, job_id, worker_id, now=now)
                raise InvalidCurationTransition(f"job {job_id} is not claimable")
            stored = self._require_job_model(session, job_id)
            session.refresh(stored)
            return self._job(stored)

    def hold_semantic_dedupe_for_review(
        self,
        job_id: UUID,
        worker_id: str,
        safe_error: str,
        *,
        now: datetime,
    ) -> CurationJob:
        """Fence an exhausted S8 semantic outage into an explicit review hold.

        A semantic-deduplication provider outage must never be represented as a
        completed selection.  The terminal review state keeps the fault detail
        while releasing the active lease, so an operator can decide whether to
        retry rather than the worker silently proceeding or marking the job
        irrecoverably failed.
        """
        now_text = _aware_utc(now).isoformat()
        with self.database.session() as session:
            changed = session.execute(
                update(AnkiCurationJobModel)
                .where(
                    AnkiCurationJobModel.id == str(job_id),
                    AnkiCurationJobModel.lease_owner == worker_id,
                    AnkiCurationJobModel.lease_expires_at.is_not(None),
                    AnkiCurationJobModel.lease_expires_at > now_text,
                    AnkiCurationJobModel.state == CurationState.CARD_DEDUPING.value,
                )
                .values(
                    state=CurationState.READY_FOR_REVIEW.value,
                    error=(_SEMANTIC_DEDUPE_RETRY_HOLD_PREFIX + safe_error)[:1_000],
                    ready_at=now_text,
                    available_at=None,
                    lease_owner=None,
                    lease_expires_at=None,
                )
            )
            if cast(CursorResult[Any], changed).rowcount != 1:
                stored = self._require_job_model(session, job_id)
                if stored.state != CurationState.CARD_DEDUPING.value:
                    raise InvalidCurationTransition(
                        f"job {job_id} is not in {CurationState.CARD_DEDUPING.value}"
                    )
                self._require_active_stage_lease(stored, job_id, worker_id, now=now)
                raise InvalidCurationTransition(f"job {job_id} is not claimable")
            stored = self._require_job_model(session, job_id)
            session.refresh(stored)
            return self._job(stored)

    def retry_job(self, job_id: UUID) -> CurationJob:
        with self.database.session() as session:
            stored = self._require_job_model(session, job_id)
            if (
                stored.state == CurationState.READY_FOR_REVIEW.value
                and _is_semantic_dedupe_retry_hold(stored.error)
            ):
                if (
                    CurationState.CARD_DEDUPING
                    not in ALLOWED_TRANSITIONS[CurationState.READY_FOR_REVIEW]
                ):
                    raise InvalidCurationTransition(
                        "transition ready_for_review -> card_deduping is not allowed"
                    )
                stored.state = CurationState.CARD_DEDUPING.value
                stored.error = None
                stored.ready_at = None
                stored.available_at = None
                stored.lease_owner = None
                stored.lease_expires_at = None
                session.flush()
                return self._job(stored)
            if stored.state != CurationState.FAILED.value:
                raise ValueError("only a failed curation job can be retried")
            failed_stage = session.scalar(
                select(AnkiJobStageModel)
                .where(
                    AnkiJobStageModel.job_id == str(job_id),
                    AnkiJobStageModel.state == "failed",
                )
                .order_by(AnkiJobStageModel.started_at.desc())
            )
            if failed_stage is None:
                raise ValueError("failed curation job has no resumable stage")
            stage = CurationStage(failed_stage.stage)
            card = stored.pipeline_contract_version in {
                PipelineContractVersion.CARD_CENTRIC_V1.value,
                PipelineContractVersion.CARD_CENTRIC_V2.value,
            }
            if self._is_legacy_blank_card_scope_failure(stored, failed_stage):
                lecture = session.get(LectureModel, stored.lecture_id)
                if lecture is None:
                    raise KeyError(stored.lecture_id)
                resolved_scope = resolve_card_centric_scope(
                    tag_allowlist=(),
                    subject=lecture.subject,
                    topic=lecture.topic,
                )
                self._rewind_legacy_blank_card_scope_failure(
                    session,
                    job_id,
                    stored,
                    resolved_scope,
                )
                session.flush()
                return self._job(stored)
            target_state = (
                CurationState.CARD_DEDUPING
                if card and stage is CurationStage.DEDUPE
                else CurationState.CARD_RECONCILING
                if card and stage is CurationStage.RECONCILIATION
                else _RETRY_STATE_BY_STAGE.get(stage)
            )
            if target_state is None:
                raise ValueError("failed curation stage cannot be retried")
            if target_state not in ALLOWED_TRANSITIONS[CurationState.FAILED]:
                raise InvalidCurationTransition(
                    f"transition failed -> {target_state.value} is not allowed"
                )
            stored.state = target_state.value
            stored.error = None
            stored.available_at = None
            stored.lease_owner = None
            stored.lease_expires_at = None
            session.flush()
            return self._job(stored)

    @staticmethod
    def _is_legacy_blank_card_scope_failure(
        job: AnkiCurationJobModel,
        failed_stage: AnkiJobStageModel,
    ) -> bool:
        try:
            tag_allowlist = json.loads(job.tag_allowlist_json)
        except (TypeError, ValueError):
            return False
        return (
            job.pipeline_contract_version
            in {
                PipelineContractVersion.CARD_CENTRIC_V1.value,
                PipelineContractVersion.CARD_CENTRIC_V2.value,
            }
            and failed_stage.stage == CurationStage.CARD_TAG_SCOPE.value
            and failed_stage.error == _BLANK_CARD_CENTRIC_SCOPE_ERROR
            and isinstance(tag_allowlist, list)
            and not any(str(value).strip() for value in tag_allowlist)
        )

    @staticmethod
    def _rewind_legacy_blank_card_scope_failure(
        session: Session,
        job_id: UUID,
        job: AnkiCurationJobModel,
        resolved_scope: tuple[str, ...],
    ) -> None:
        """Reset only artifacts derived from a legacy empty card-centric scope.

        Scope resolution occurs before this helper mutates anything, so an ambiguous
        or unmatched lecture remains a failed job with all of its diagnostics intact.
        The surrounding database session makes the scope pin and cleanup atomic.
        """
        job_key = str(job_id)
        stages = [stage.value for stage in _CARD_CENTRIC_REWIND_STAGES]
        session.execute(
            delete(AnkiStageArtifactModel).where(
                AnkiStageArtifactModel.job_id == job_key,
                AnkiStageArtifactModel.stage.in_(stages),
            )
        )
        session.execute(
            delete(AnkiJobStageModel).where(
                AnkiJobStageModel.job_id == job_key,
                AnkiJobStageModel.stage.in_(stages),
            )
        )
        for model in (
            AnkiCandidateModel,
            AnkiGapCardModel,
            AnkiSourceEvidenceModel,
            AnkiReviewedReconciliationModel,
            AnkiReviewChangeSetModel,
            AnkiTagPatchModel,
        ):
            session.execute(delete(model).where(model.job_id == job_key))
        job.tag_allowlist_json = _canonical_json(resolved_scope)
        job.configuration_sha256 = _configuration_sha256(
            _configuration_document(
                block_id=job.block_id,
                source_revision_ids=tuple(json.loads(job.source_revision_ids_json)),
                source_revision_hashes={
                    int(key): str(value)
                    for key, value in cast(
                        dict[str, Any], json.loads(job.source_revision_hashes_json)
                    ).items()
                },
                summary_outline_id=job.summary_outline_id,
                summary_outline_sha256=job.summary_outline_sha256,
                deck_allowlist=tuple(json.loads(job.deck_allowlist_json)),
                tag_allowlist=resolved_scope,
                target_deck=job.target_deck,
                target_tag=job.target_tag,
                index_snapshot_id=job.index_snapshot_id,
                lcl_prompt_version=job.lcl_prompt_version,
                judgment_rubric_version=job.judgment_rubric_version,
                gap_prompt_version=job.gap_prompt_version,
                provider=job.provider,
                model=job.model,
                pipeline_contract_version=PipelineContractVersion(job.pipeline_contract_version),
                model_config_sha256=job.model_config_sha256,
                semantic_generation=job.semantic_generation,
                companion_generation=job.companion_generation,
                policy_sha256=job.policy_sha256,
            )
        )
        job.source_index_generation = None
        job.counts_json = "{}"
        job.review_revision = 0
        job.ready_at = None
        job.state = CurationState.BUILDING_SOURCE_INDEX.value
        job.error = None
        job.available_at = None
        job.lease_owner = None
        job.lease_expires_at = None

    def remove_failed_job(self, job_id: UUID) -> CurationJob:
        with self.database.session() as session:
            stored = self._require_job_model(session, job_id)
            if stored.state != CurationState.FAILED.value:
                raise ValueError("only a failed curation job can be removed")
            if CurationState.REMOVED not in ALLOWED_TRANSITIONS[CurationState.FAILED]:
                raise InvalidCurationTransition("transition failed -> removed is not allowed")
            stored.state = CurationState.REMOVED.value
            stored.available_at = None
            stored.lease_owner = None
            stored.lease_expires_at = None
            session.flush()
            return self._job(stored)

    def cancel_job(self, job_id: UUID) -> CurationJob:
        with self.database.session() as session:
            stored = self._require_job_model(session, job_id)
            state = CurationState(stored.state)
            if state not in _CLAIMABLE_STATES:
                raise ValueError(f"job in {state.value} cannot be canceled")
            stored.state = CurationState.CANCELED.value
            stored.error = "Canceled by user"
            stored.available_at = None
            stored.lease_owner = None
            stored.lease_expires_at = None
            session.flush()
            return self._job(stored)

    def transition(
        self,
        job_id: UUID,
        expected_state: CurationState,
        target_state: CurationState,
        detail: str | None = None,
    ) -> CurationJob:
        if target_state not in ALLOWED_TRANSITIONS.get(expected_state, set()):
            raise InvalidCurationTransition(
                f"transition {expected_state.value} -> {target_state.value} is not allowed"
            )
        values: dict[str, Any] = {
            "state": target_state.value,
            "error": detail if target_state is CurationState.FAILED else None,
        }
        if target_state is CurationState.READY_FOR_REVIEW:
            values["ready_at"] = utc_now()
        if target_state is CurationState.COMPLETE:
            values["completed_at"] = utc_now()
        with self.database.session() as session:
            changed = session.execute(
                update(AnkiCurationJobModel)
                .where(
                    AnkiCurationJobModel.id == str(job_id),
                    AnkiCurationJobModel.state == expected_state.value,
                )
                .values(**values)
            )
            if cast(CursorResult[Any], changed).rowcount != 1:
                raise InvalidCurationTransition(f"job {job_id} is not in {expected_state.value}")
            stored = session.get(AnkiCurationJobModel, str(job_id))
            assert stored is not None
            session.refresh(stored)
            return self._job(stored)

    def recover_interrupted_jobs(self) -> int:
        with self.database.session() as session:
            stored = session.scalars(
                select(AnkiCurationJobModel).where(
                    AnkiCurationJobModel.state.in_(
                        [state.value for state in _INTERRUPTED_PRE_REVIEW_STATES]
                    )
                )
            ).all()
            for job in stored:
                job.lease_owner = None
                job.lease_expires_at = None
                job.error = "resumable after an interrupted Hub process"
            return len(stored)

    def start_stage(
        self,
        job_id: UUID,
        stage: CurationStage,
        provider: str | None = None,
        model: str | None = None,
        *,
        expected_state: CurationState | None = None,
        lease_owner: str | None = None,
        now: datetime | None = None,
    ) -> JobStage:
        with self.database.session() as session:
            job = self._require_job_model(session, job_id)
            if expected_state is not None and job.state != expected_state.value:
                raise InvalidCurationTransition(f"job {job_id} is not in {expected_state.value}")
            self._require_active_stage_lease(job, job_id, lease_owner, now=now)
            stored = session.scalar(
                select(AnkiJobStageModel).where(
                    AnkiJobStageModel.job_id == str(job_id),
                    AnkiJobStageModel.stage == stage.value,
                )
            )
            if stored is None:
                stored = AnkiJobStageModel(
                    job_id=str(job_id),
                    stage=stage.value,
                    attempt_count=0,
                )
                session.add(stored)
            stored.state = "running"
            stored.attempt_count += 1
            stored.provider = provider
            stored.model = model
            stored.request_id = None
            stored.input_tokens = 0
            stored.output_tokens = 0
            stored.cost_microusd = 0
            stored.cache_hits = 0
            stored.started_at = utc_now()
            stored.finished_at = None
            stored.error = None
            session.flush()
            return self._stage(stored)

    def finish_stage(
        self,
        job_id: UUID,
        stage: CurationStage,
        usage: StageUsage | None = None,
        cache_hits: int = 0,
    ) -> JobStage:
        with self.database.session() as session:
            stored = self._require_stage(session, job_id, stage)
            stored.state = "complete"
            stored.finished_at = utc_now()
            stored.cache_hits = cache_hits
            if usage is not None:
                stored.request_id = usage.request_id
                stored.input_tokens = usage.input_tokens
                stored.output_tokens = usage.output_tokens
                stored.cost_microusd = usage.cost_microusd
            session.flush()
            return self._stage(stored)

    def fail_stage(
        self,
        job_id: UUID,
        stage: CurationStage,
        safe_error: str,
        *,
        expected_state: CurationState,
        lease_owner: str | None,
        now: datetime | None = None,
    ) -> JobStage:
        with self.database.session() as session:
            job = self._require_job_model(session, job_id)
            if job.state != expected_state.value:
                raise InvalidCurationTransition(f"job {job_id} is not in {expected_state.value}")
            self._require_active_stage_lease(job, job_id, lease_owner, now=now)
            stored = self._require_stage(session, job_id, stage)
            if stored.state != "running":
                raise InvalidCurationTransition(f"stage {stage.value} is not running")
            stored.state = "failed"
            stored.finished_at = utc_now()
            stored.error = safe_error
            session.flush()
            return self._stage(stored)

    def get_stage(
        self,
        job_id: UUID,
        stage: CurationStage,
    ) -> JobStage | None:
        with self.database.session() as session:
            stored = session.scalar(
                select(AnkiJobStageModel).where(
                    AnkiJobStageModel.job_id == str(job_id),
                    AnkiJobStageModel.stage == stage.value,
                )
            )
            return None if stored is None else self._stage(stored)

    def record_card_ledger_attempt(
        self,
        job_id: UUID,
        attempt: CardCentricLedgerAttempt,
        *,
        expected_stage_attempt: int,
        lease_owner: str | None,
        now: datetime | None = None,
    ) -> None:
        """Append one S2 provider invocation without rewriting prior evidence."""
        parameters_json = _canonical_json(attempt.generation_parameters)
        _validate_card_ledger_attempt_for_write(
            attempt,
            parameters_json,
            allow_hash_only_validation_failure=self._allow_hash_only_card_ledger_failure(),
        )
        with self.database.session() as session:
            # In WAL mode, deferred reads do not fence a subsequent writer: a
            # stale worker can otherwise validate its lease and stage attempt,
            # then append after a successor has reclaimed the job.  Acquire a
            # SQLite write transaction *before* every lease/stage/primary/
            # idempotency check.  This critical section contains only durable
            # evidence bookkeeping; provider calls have already completed.
            #
            # Other supported databases use the job/stage row locks below.
            # Keeping the fence local to this append avoids a process-global
            # lock and preserves the normal session lifecycle.
            is_sqlite = session.bind is not None and session.bind.dialect.name == "sqlite"
            if is_sqlite:
                session.execute(text("BEGIN IMMEDIATE"))
            job_query = select(AnkiCurationJobModel).where(AnkiCurationJobModel.id == str(job_id))
            if not is_sqlite:
                job_query = job_query.with_for_update()
            job = session.scalar(job_query)
            if job is None:
                raise KeyError(str(job_id))
            self._require_active_stage_lease(job, job_id, lease_owner, now=now)
            stage_query = select(AnkiJobStageModel).where(
                AnkiJobStageModel.job_id == str(job_id),
                AnkiJobStageModel.stage == CurationStage.CARD_LEDGER.value,
            )
            if not is_sqlite:
                stage_query = stage_query.with_for_update()
            stage = session.scalar(stage_query)
            if stage is None:
                raise KeyError(f"{job_id}:{CurationStage.CARD_LEDGER.value}")
            if (
                stage.state != "running"
                or stage.attempt_count < 1
                or stage.attempt_count != expected_stage_attempt
            ):
                raise InvalidCurationTransition("card-ledger attempt is not running")
            if (stage.provider is not None and stage.provider != attempt.provider.value) or (
                stage.model is not None and stage.model != attempt.model
            ):
                raise ValueError("card-ledger attempt does not match the stage transport")
            if attempt.call_index == 2:
                primary = session.scalar(
                    select(AnkiCardLedgerAttemptModel).where(
                        AnkiCardLedgerAttemptModel.job_id == str(job_id),
                        AnkiCardLedgerAttemptModel.stage == CurationStage.CARD_LEDGER.value,
                        AnkiCardLedgerAttemptModel.stage_attempt == stage.attempt_count,
                        AnkiCardLedgerAttemptModel.call_index == 1,
                    )
                )
                if primary is None:
                    raise ValueError("card-ledger repair requires a persisted primary call")
                if primary.outcome != "validation_failed":
                    raise ValueError("card-ledger repair requires a validation-failed primary call")
                if (
                    primary.provider != attempt.provider.value
                    or primary.model != attempt.model
                    or primary.generation_parameters_json != parameters_json
                    or primary.generation_parameters_sha256 != attempt.generation_parameters_sha256
                ):
                    raise ValueError("card-ledger repair must match the primary transport identity")
            existing = session.scalar(
                select(AnkiCardLedgerAttemptModel).where(
                    AnkiCardLedgerAttemptModel.job_id == str(job_id),
                    AnkiCardLedgerAttemptModel.stage == CurationStage.CARD_LEDGER.value,
                    AnkiCardLedgerAttemptModel.stage_attempt == stage.attempt_count,
                    AnkiCardLedgerAttemptModel.call_index == attempt.call_index,
                )
            )
            values = {
                "kind": attempt.kind,
                "outcome": attempt.outcome,
                "provider": attempt.provider.value,
                "model": attempt.model,
                "instruction_sha256": attempt.instruction_sha256,
                "generation_parameters_json": parameters_json,
                "generation_parameters_sha256": attempt.generation_parameters_sha256,
                "request_id": attempt.request_id or None,
                "input_tokens": attempt.input_tokens,
                "output_tokens": attempt.output_tokens,
                "cost_microusd": attempt.cost_microusd,
                "validation_error": attempt.validation_error,
                "invalid_response_sha256": attempt.invalid_response_sha256,
                "invalid_response": attempt.invalid_response,
                "diagnostic_source": attempt.diagnostic_source,
                "http_status": attempt.http_status,
            }
            if existing is not None:
                if any(getattr(existing, key) != value for key, value in values.items()):
                    raise ValueError(
                        "card-ledger attempt identity was reused with different evidence"
                    )
                return
            session.add(
                AnkiCardLedgerAttemptModel(
                    job_id=str(job_id),
                    stage=CurationStage.CARD_LEDGER.value,
                    stage_attempt=stage.attempt_count,
                    call_index=attempt.call_index,
                    **values,
                )
            )
            # Surface a uniqueness violation inside the fenced transaction,
            # rather than deferring it until the session context commits.
            session.flush()

    def _allow_hash_only_card_ledger_failure(self) -> bool:
        """Capture may retain a validation-failure digest without its raw payload."""
        return False

    def record_provider_attempt_event(
        self,
        evidence: ProviderEventEvidence,
        *,
        lease_owner: str | None,
        now: datetime | None = None,
    ) -> None:
        """Append one fenced provider event; exact duplicates are idempotent."""
        event = evidence.event
        identity = event.identity
        if identity.job_id.int == 0:
            raise ValueError("provider attempt job identity is invalid")
        values: dict[str, object] = {
            "subcall_ordinal": identity.subcall_ordinal,
            "batch_index": identity.batch_index,
            "batch_note_ids_json": _canonical_json(list(identity.batch_note_ids)),
            "batch_note_ids_sha256": identity.batch_note_ids_sha256,
            "kind": identity.kind,
            "provider": evidence.provider,
            "model": evidence.model,
            "instruction_sha256": evidence.instruction_sha256,
            "input_sha256": evidence.input_sha256,
            "output_schema_sha256": evidence.output_schema_sha256,
            "generation_parameters_json": _canonical_json(evidence.generation_parameters),
            "generation_parameters_sha256": evidence.generation_parameters_sha256,
            "cache_prefix_sha256": evidence.cache_prefix_sha256,
            "request_sha256": event.request_sha256,
            "request_id": evidence.request_id,
            "input_tokens": evidence.input_tokens,
            "output_tokens": evidence.output_tokens,
            "cost_microusd": evidence.cost_microusd,
            "cache_creation_input_tokens": evidence.cache_creation_input_tokens,
            "cache_read_input_tokens": evidence.cache_read_input_tokens,
            "response_sha256": event.response_sha256,
            "response_text": evidence.response_text,
            "validation_error": event.error,
            "missing_note_ids_json": _canonical_json(list(event.missing_note_ids)),
            "extra_note_ids_json": _canonical_json(list(event.extra_note_ids)),
            "duplicate_note_ids_json": _canonical_json(list(event.duplicate_note_ids)),
            "diagnostic_source": evidence.diagnostic_source,
            "http_status": evidence.http_status,
            "cost_reservation_json": (
                None
                if evidence.cost_reservation is None
                else _canonical_json(evidence.cost_reservation)
            ),
            "cost_reservation_sha256": evidence.cost_reservation_sha256,
        }
        with self.database.session() as session:
            is_sqlite = session.bind is not None and session.bind.dialect.name == "sqlite"
            if is_sqlite:
                session.execute(text("BEGIN IMMEDIATE"))
            job_query = select(AnkiCurationJobModel).where(
                AnkiCurationJobModel.id == str(identity.job_id)
            )
            if not is_sqlite:
                job_query = job_query.with_for_update()
            job = session.scalar(job_query)
            if job is None:
                raise KeyError(str(identity.job_id))
            self._require_active_stage_lease(job, identity.job_id, lease_owner, now=now)
            stage_query = select(AnkiJobStageModel).where(
                AnkiJobStageModel.job_id == str(identity.job_id),
                AnkiJobStageModel.stage == identity.stage.value,
            )
            if not is_sqlite:
                stage_query = stage_query.with_for_update()
            stage = session.scalar(stage_query)
            if stage is None:
                raise KeyError(f"{identity.job_id}:{identity.stage.value}")
            if stage.state != "running" or stage.attempt_count != identity.stage_attempt:
                raise InvalidCurationTransition("provider attempt stage is not running")
            existing = session.scalar(
                select(AnkiProviderAttemptEventModel).where(
                    AnkiProviderAttemptEventModel.job_id == str(identity.job_id),
                    AnkiProviderAttemptEventModel.stage == identity.stage.value,
                    AnkiProviderAttemptEventModel.stage_attempt == identity.stage_attempt,
                    AnkiProviderAttemptEventModel.mode == identity.mode,
                    AnkiProviderAttemptEventModel.call_index == identity.call_index,
                    AnkiProviderAttemptEventModel.subcall_ordinal == identity.subcall_ordinal,
                    AnkiProviderAttemptEventModel.event == event.event,
                )
            )
            if existing is not None:
                if any(getattr(existing, key) != value for key, value in values.items()):
                    raise ValueError("provider attempt event identity was reused")
                return
            prior = list(
                session.scalars(
                    select(AnkiProviderAttemptEventModel)
                    .where(
                        AnkiProviderAttemptEventModel.job_id == str(identity.job_id),
                        AnkiProviderAttemptEventModel.stage == identity.stage.value,
                        AnkiProviderAttemptEventModel.stage_attempt == identity.stage_attempt,
                        AnkiProviderAttemptEventModel.mode == identity.mode,
                        AnkiProviderAttemptEventModel.call_index == identity.call_index,
                        AnkiProviderAttemptEventModel.subcall_ordinal == identity.subcall_ordinal,
                    )
                    .order_by(AnkiProviderAttemptEventModel.id)
                )
            )
            _validate_provider_event_append([row.event for row in prior], event.event)
            session.add(
                AnkiProviderAttemptEventModel(
                    job_id=str(identity.job_id),
                    stage=identity.stage.value,
                    stage_attempt=identity.stage_attempt,
                    mode=identity.mode,
                    call_index=identity.call_index,
                    event=event.event,
                    **values,
                )
            )
            session.flush()

    def list_provider_attempt_events(self, job_id: UUID) -> list[dict[str, object]]:
        with self.database.session() as session:
            rows = list(
                session.scalars(
                    select(AnkiProviderAttemptEventModel)
                    .where(AnkiProviderAttemptEventModel.job_id == str(job_id))
                    .order_by(
                        AnkiProviderAttemptEventModel.stage_attempt,
                        AnkiProviderAttemptEventModel.call_index,
                        AnkiProviderAttemptEventModel.id,
                    )
                )
            )
        return [
            {
                "id": row.id,
                "stage": row.stage,
                "stage_attempt": row.stage_attempt,
                "mode": row.mode,
                "call_index": row.call_index,
                "subcall_ordinal": row.subcall_ordinal,
                "batch_index": row.batch_index,
                "batch_note_ids": json.loads(row.batch_note_ids_json),
                "batch_note_ids_sha256": row.batch_note_ids_sha256,
                "kind": row.kind,
                "event": row.event,
                "provider": row.provider,
                "model": row.model,
                "instruction_sha256": row.instruction_sha256,
                "input_sha256": row.input_sha256,
                "output_schema_sha256": row.output_schema_sha256,
                "generation_parameters": json.loads(row.generation_parameters_json),
                "generation_parameters_sha256": row.generation_parameters_sha256,
                "cache_prefix_sha256": row.cache_prefix_sha256,
                "request_sha256": row.request_sha256,
                "request_id": row.request_id,
                "input_tokens": row.input_tokens,
                "output_tokens": row.output_tokens,
                "cost_microusd": row.cost_microusd,
                "cache_creation_input_tokens": row.cache_creation_input_tokens,
                "cache_read_input_tokens": row.cache_read_input_tokens,
                "response_sha256": row.response_sha256,
                "response_text": row.response_text,
                "validation_error": row.validation_error,
                "missing_note_ids": json.loads(row.missing_note_ids_json),
                "extra_note_ids": json.loads(row.extra_note_ids_json),
                "duplicate_note_ids": json.loads(row.duplicate_note_ids_json),
                "diagnostic_source": row.diagnostic_source,
                "http_status": row.http_status,
                "cost_reservation": (
                    None
                    if row.cost_reservation_json is None
                    else json.loads(row.cost_reservation_json)
                ),
                "cost_reservation_sha256": row.cost_reservation_sha256,
                "created_at": row.created_at,
            }
            for row in rows
        ]

    def require_no_indeterminate_provider_attempt(
        self,
        job_id: UUID,
        stage: CurationStage,
    ) -> None:
        with self.database.session() as session:
            rows = list(
                session.scalars(
                    select(AnkiProviderAttemptEventModel)
                    .where(
                        AnkiProviderAttemptEventModel.job_id == str(job_id),
                        AnkiProviderAttemptEventModel.stage == stage.value,
                    )
                    .order_by(AnkiProviderAttemptEventModel.id)
                )
            )
        events_by_call: dict[tuple[int, str, int, int], list[AnkiProviderAttemptEventModel]] = {}
        for row in rows:
            events_by_call.setdefault(
                (row.stage_attempt, row.mode, row.call_index, row.subcall_ordinal), []
            ).append(row)
        terminal = {"accepted", "validation_failed", "transport_failed", "contract_failed"}
        for attempt_rows in events_by_call.values():
            events = {row.event for row in attempt_rows}
            if "dispatched" in events and not terminal.intersection(events):
                raise ProviderAttemptIndeterminate(
                    "provider call lacks a durable terminal outcome; ordinary response evidence "
                    "is redacted and cannot authorize replay"
                )

    def list_card_ledger_attempts(self, job_id: UUID) -> list[dict[str, object]]:
        """Return immutable S2 provider-call diagnostics in call order."""
        with self.database.session() as session:
            rows = session.scalars(
                select(AnkiCardLedgerAttemptModel)
                .where(AnkiCardLedgerAttemptModel.job_id == str(job_id))
                .order_by(
                    AnkiCardLedgerAttemptModel.stage_attempt,
                    AnkiCardLedgerAttemptModel.call_index,
                )
            ).all()
        return [
            {
                "stage": row.stage,
                "stage_attempt": row.stage_attempt,
                "call_index": row.call_index,
                "kind": row.kind,
                "outcome": row.outcome,
                "provider": row.provider,
                "model": row.model,
                "instruction_sha256": row.instruction_sha256,
                "generation_parameters": json.loads(row.generation_parameters_json),
                "generation_parameters_sha256": row.generation_parameters_sha256,
                "request_id": row.request_id,
                "input_tokens": row.input_tokens,
                "output_tokens": row.output_tokens,
                "cost_microusd": row.cost_microusd,
                "validation_error": row.validation_error,
                "invalid_response_sha256": row.invalid_response_sha256,
                "invalid_response": row.invalid_response,
                "diagnostic_source": row.diagnostic_source,
                "http_status": row.http_status,
            }
            for row in rows
        ]

    def commit_stage(
        self,
        job_id: UUID,
        *,
        expected_state: CurationState,
        target_state: CurationState,
        stage: CurationStage,
        artifact: StageArtifact,
        usage: StageUsage | None = None,
        cache_hits: int = 0,
        lease_owner: str | None = None,
        candidates: Sequence[Candidate] | None = None,
        source_evidence: Sequence[SourceEvidence] | None = None,
        gap_cards: Sequence[GapCard] | None = None,
        job_pins: dict[str, str] | None = None,
        failure_detail: str | None = None,
        now: datetime | None = None,
    ) -> CurationJob:
        if target_state not in ALLOWED_TRANSITIONS.get(
            expected_state,
            set(),
        ):
            raise InvalidCurationTransition(
                f"transition {expected_state.value} -> {target_state.value} is not allowed"
            )
        if artifact.stage is not stage:
            raise ValueError("stage artifact does not match committed stage")
        if (target_state is CurationState.FAILED) != (failure_detail is not None):
            raise ValueError("failed stage commits require failure detail")
        with self.database.session() as session:
            job = self._require_job_model(session, job_id)
            self._validate_stage_artifact_for_commit(job, job_id, stage, artifact)
            if job.state != expected_state.value:
                raise InvalidCurationTransition(f"job {job_id} is not in {expected_state.value}")
            self._require_active_stage_lease(job, job_id, lease_owner, now=now)
            stored_stage = self._require_stage(session, job_id, stage)
            if stored_stage.state != "running":
                raise InvalidCurationTransition(f"stage {stage.value} is not running")
            existing = session.scalar(
                select(AnkiStageArtifactModel).where(
                    AnkiStageArtifactModel.job_id == str(job_id),
                    AnkiStageArtifactModel.artifact_id == artifact.artifact_id,
                )
            )
            if existing is None:
                session.add(
                    AnkiStageArtifactModel(
                        job_id=str(job_id),
                        artifact_id=artifact.artifact_id,
                        stage=artifact.stage.value,
                        kind=artifact.kind,
                        relative_path=artifact.relative_path,
                        input_sha256=artifact.input_sha256,
                        content_sha256=artifact.content_sha256,
                        metadata_json=_canonical_json(artifact.metadata),
                        pipeline_contract_version=artifact.pipeline_contract_version.value,
                        model_config_sha256=artifact.model_config_sha256 or job.model_config_sha256,
                    )
                )
            elif (
                existing.stage != artifact.stage.value
                or existing.kind != artifact.kind
                or existing.relative_path != artifact.relative_path
                or existing.input_sha256 != artifact.input_sha256
                or existing.content_sha256 != artifact.content_sha256
                or existing.pipeline_contract_version != artifact.pipeline_contract_version.value
                or existing.model_config_sha256 != artifact.model_config_sha256
                or cast(
                    dict[str, Any],
                    json.loads(existing.metadata_json),
                )
                != artifact.metadata
            ):
                raise ValueError("artifact identity was reused with different content")
            stored_stage.state = "failed" if target_state is CurationState.FAILED else "complete"
            stored_stage.finished_at = utc_now()
            stored_stage.cache_hits = cache_hits
            stored_stage.error = failure_detail
            if usage is not None:
                stored_stage.request_id = usage.request_id
                stored_stage.input_tokens = usage.input_tokens
                stored_stage.output_tokens = usage.output_tokens
                stored_stage.cost_microusd = usage.cost_microusd
            if candidates is not None:
                self._replace_candidate_models(
                    session,
                    job_id,
                    candidates,
                )
            if source_evidence is not None:
                self._replace_source_evidence_models(
                    session,
                    job_id,
                    source_evidence,
                )
            if gap_cards is not None:
                self._replace_gap_card_models(
                    session,
                    job_id,
                    gap_cards,
                )
            for name, value in (job_pins or {}).items():
                if name not in {
                    "semantic_generation",
                    "companion_generation",
                    "source_index_generation",
                }:
                    raise ValueError(f"unsupported job pin {name}")
                if not value.strip():
                    raise ValueError(f"job pin {name} cannot be blank")
                current = cast(str | None, getattr(job, name))
                if current is not None and current != value:
                    raise ValueError(f"job pin {name} cannot be changed")
                setattr(job, name, value)
            job.state = target_state.value
            job.error = failure_detail
            if target_state is CurationState.READY_FOR_REVIEW:
                job.ready_at = utc_now()
            session.flush()
            return self._job(job)

    @staticmethod
    def _require_active_stage_lease(
        job: AnkiCurationJobModel,
        job_id: UUID,
        lease_owner: str | None,
        *,
        now: datetime | None,
    ) -> None:
        if job.lease_owner != lease_owner:
            raise InvalidCurationTransition(f"worker no longer owns job {job_id}")
        if lease_owner is None:
            return
        if job.lease_expires_at is None:
            raise InvalidCurationTransition(f"worker lease is unavailable for job {job_id}")
        current = _aware_utc(now or datetime.now(UTC))
        expires_at = _aware_utc(datetime.fromisoformat(job.lease_expires_at))
        if expires_at <= current:
            raise InvalidCurationTransition(f"worker lease expired for job {job_id}")

    def replace_source_evidence(
        self,
        job_id: UUID,
        evidence: Sequence[SourceEvidence],
    ) -> None:
        with self.database.session() as session:
            self._require_job_model(session, job_id)
            self._replace_source_evidence_models(
                session,
                job_id,
                evidence,
            )

    def list_source_evidence(self, job_id: UUID) -> list[SourceEvidence]:
        with self.database.session() as session:
            stored = session.scalars(
                select(AnkiSourceEvidenceModel)
                .where(AnkiSourceEvidenceModel.job_id == str(job_id))
                .order_by(AnkiSourceEvidenceModel.id)
            ).all()
            return [self._source_evidence(item) for item in stored]

    def save_stage_artifact(
        self,
        job_id: UUID,
        artifact: StageArtifact,
    ) -> None:
        with self.database.session() as session:
            job = self._require_job_model(session, job_id)
            self._validate_stage_artifact_for_commit(job, job_id, artifact.stage, artifact)
            existing = session.scalar(
                select(AnkiStageArtifactModel).where(
                    AnkiStageArtifactModel.job_id == str(job_id),
                    AnkiStageArtifactModel.artifact_id == artifact.artifact_id,
                )
            )
            if existing is not None:
                if (
                    existing.content_sha256 != artifact.content_sha256
                    or existing.input_sha256 != artifact.input_sha256
                    or existing.stage != artifact.stage.value
                    or existing.kind != artifact.kind
                    or existing.relative_path != artifact.relative_path
                    or existing.pipeline_contract_version
                    != artifact.pipeline_contract_version.value
                    or existing.model_config_sha256 != artifact.model_config_sha256
                ):
                    raise ValueError("artifact identity was reused with different content")
                return
            session.add(
                AnkiStageArtifactModel(
                    job_id=str(job_id),
                    artifact_id=artifact.artifact_id,
                    stage=artifact.stage.value,
                    kind=artifact.kind,
                    relative_path=artifact.relative_path,
                    input_sha256=artifact.input_sha256,
                    content_sha256=artifact.content_sha256,
                    metadata_json=_canonical_json(artifact.metadata),
                    pipeline_contract_version=artifact.pipeline_contract_version.value,
                    model_config_sha256=(artifact.model_config_sha256 or job.model_config_sha256),
                )
            )

    def list_stage_artifacts(self, job_id: UUID) -> list[StageArtifact]:
        with self.database.session() as session:
            stored = session.scalars(
                select(AnkiStageArtifactModel)
                .where(AnkiStageArtifactModel.job_id == str(job_id))
                .order_by(AnkiStageArtifactModel.id)
            ).all()
            return [self._stage_artifact(item) for item in stored]

    def replace_candidates(
        self,
        job_id: UUID,
        candidates: Sequence[Candidate],
    ) -> None:
        with self.database.session() as session:
            self._require_job_model(session, job_id)
            self._replace_candidate_models(
                session,
                job_id,
                candidates,
            )

    def list_candidates(self, job_id: UUID) -> list[Candidate]:
        with self.database.session() as session:
            models = session.scalars(
                select(AnkiCandidateModel)
                .where(AnkiCandidateModel.job_id == str(job_id))
                .order_by(AnkiCandidateModel.id)
            ).all()
            return [self._candidate(stored) for stored in models]

    def save_gap_cards(self, job_id: UUID, cards: Sequence[GapCard]) -> None:
        with self.database.session() as session:
            self._require_job_model(session, job_id)
            self._replace_gap_card_models(session, job_id, cards)

    def list_gap_cards(self, job_id: UUID) -> list[GapCard]:
        with self.database.session() as session:
            models = session.scalars(
                select(AnkiGapCardModel)
                .where(AnkiGapCardModel.job_id == str(job_id))
                .order_by(AnkiGapCardModel.id)
            ).all()
            return [self._gap_card(stored) for stored in models]

    def save_review(
        self,
        job_id: UUID,
        change_set: ReviewChangeSet,
        *,
        card_centric_snapshot: dict[str, Any] | None = None,
        v3_review_artifact_sha256: str | None = None,
        v3_cost_ledger_sha256: str | None = None,
    ) -> SavedReview:
        with self.database.session() as session:
            job = self._require_job_model(session, job_id)
            if job.review_revision != change_set.expected_revision:
                raise ValueError("review revision is stale")
            reviewer = change_set.reviewer.strip()
            if not reviewer or len(reviewer) > 200:
                raise ValueError("reviewer is invalid")
            documented_t6_nids: set[int] = set()
            v3_snapshot: V3ReviewSnapshot | None = None
            is_v3 = job.pipeline_contract_version == PipelineContractVersion.CARD_CENTRIC_V3.value
            if is_v3:
                if card_centric_snapshot is None:
                    raise ValueError("v3 review requires a committed R11 snapshot")
                try:
                    v3_snapshot = V3ReviewSnapshot.model_validate(card_centric_snapshot)
                except (TypeError, ValueError) as exc:
                    raise ValueError("v3 review requires a valid R11 snapshot") from exc
                if (
                    card_centric_snapshot.get("snapshot_sha256") != v3_snapshot.snapshot_sha256
                    or not _is_sha256(v3_review_artifact_sha256)
                    or not _is_sha256(v3_cost_ledger_sha256)
                ):
                    raise ValueError("v3 review requires exact R11 snapshot and identity")
                visible_notes = {
                    int(item["note_id"])
                    for item in v3_snapshot.existing_candidates
                    if isinstance(item.get("note_id"), int)
                }
                visible_cards = {
                    str(item["card_id"])
                    for item in v3_snapshot.generated_cards
                    if isinstance(item.get("card_id"), str)
                }
                if not set(change_set.candidate_selections) <= visible_notes:
                    raise ValueError("v3 review cannot select an invisible candidate")
                canonical_notes = {
                    int(item["note_id"])
                    for item in v3_snapshot.existing_candidates
                    if item.get("disposition") == "keep" and isinstance(item.get("note_id"), int)
                }
                redundant_notes = {
                    int(item["note_id"])
                    for item in v3_snapshot.existing_candidates
                    if item.get("disposition") == "redundant"
                    and isinstance(item.get("note_id"), int)
                } - canonical_notes
                if any(
                    selected and note_id in redundant_notes
                    for note_id, selected in change_set.candidate_selections.items()
                ):
                    raise ValueError("v3 review cannot select a redundant representative")
                if any(
                    edit.card_id and edit.card_id not in visible_cards
                    for edit in change_set.gap_edits
                ):
                    raise ValueError("v3 review cannot edit an invisible generated card")
            elif card_centric_snapshot is not None:
                documented_t6_nids = set(
                    CardCentricReconciliationInput.model_validate(
                        card_centric_snapshot
                    ).t6_selected_nids
                )
            if len({patch.note_id for patch in change_set.tag_patches}) != len(
                change_set.tag_patches
            ):
                raise ValueError("a review cannot patch one note more than once")
            for note_id, selected in change_set.candidate_selections.items():
                candidate = session.scalar(
                    select(AnkiCandidateModel).where(
                        AnkiCandidateModel.job_id == str(job_id),
                        AnkiCandidateModel.note_id == note_id,
                    )
                )
                if candidate is None:
                    raise KeyError(note_id)
                provenance = cast(dict[str, Any], json.loads(candidate.provenance_json))
                card_centric = provenance.get("card_centric_v2", provenance.get("card_centric", {}))
                if (
                    selected
                    and isinstance(card_centric, dict)
                    and (
                        "selection_eligible" in card_centric
                        and not bool(card_centric["selection_eligible"])
                    )
                    and note_id not in documented_t6_nids
                ):
                    raise ValueError("review cannot select an undocumented ineligible card")
                candidate.selected = selected
            for edit in change_set.gap_edits:
                conditions = [AnkiGapCardModel.job_id == str(job_id)]
                if edit.card_id:
                    conditions.append(AnkiGapCardModel.id == edit.card_id)
                    gap = session.scalar(select(AnkiGapCardModel).where(*conditions))
                else:
                    conditions.append(AnkiGapCardModel.concept_id == edit.concept_id)
                    matches = session.scalars(select(AnkiGapCardModel).where(*conditions)).all()
                    if len(matches) > 1:
                        raise ValueError(
                            "gap card edit requires card_id when a concept has multiple cards"
                        )
                    gap = matches[0] if matches else None
                if gap is None:
                    raise KeyError(edit.card_id or edit.concept_id)
                if gap.concept_id != edit.concept_id:
                    raise ValueError("generated card identity conflicts with concept")
                gap.text = edit.text
                gap.extra = edit.extra
                gap.selected = edit.selected
                gap.revision += 1
            next_revision = job.review_revision + 1
            session.add(
                AnkiReviewChangeSetModel(
                    job_id=str(job_id),
                    revision=next_revision,
                    prior_revision=job.review_revision,
                    reviewer=reviewer,
                    payload_json=_canonical_json(
                        {
                            "candidate_selections": (change_set.candidate_selections),
                            "gap_edits": [
                                {
                                    "card_id": edit.card_id,
                                    "concept_id": edit.concept_id,
                                    "text": edit.text,
                                    "extra": edit.extra,
                                    "selected": edit.selected,
                                }
                                for edit in change_set.gap_edits
                            ],
                            "tag_patches": [
                                {
                                    "note_id": patch.note_id,
                                    "before": patch.before,
                                    "after": patch.after,
                                    "add_tags": patch.add_tags,
                                    "remove_tags": patch.remove_tags,
                                    "expected_tag_hash": (patch.expected_tag_hash),
                                    "tag_policy_version": (patch.tag_policy_version),
                                }
                                for patch in change_set.tag_patches
                            ],
                        }
                    ),
                )
            )
            for patch in change_set.tag_patches:
                session.add(
                    AnkiTagPatchModel(
                        job_id=str(job_id),
                        note_id=patch.note_id,
                        revision=next_revision,
                        before_json=_canonical_json(patch.before),
                        after_json=_canonical_json(patch.after),
                        add_tags_json=_canonical_json(patch.add_tags),
                        remove_tags_json=_canonical_json(patch.remove_tags),
                        expected_tag_hash=patch.expected_tag_hash,
                        policy_version=patch.tag_policy_version,
                    )
                )
            job.review_revision += 1
            if is_v3:
                self._persist_reviewed_v3_snapshot(
                    session,
                    job_id,
                    revision=job.review_revision,
                    snapshot_payload=cast(dict[str, Any], card_centric_snapshot),
                    r11_artifact_sha256=cast(str, v3_review_artifact_sha256),
                    cost_ledger_sha256=cast(str, v3_cost_ledger_sha256),
                )
            elif card_centric_snapshot is not None:
                self._persist_reviewed_card_centric_snapshot(
                    session,
                    job_id,
                    revision=job.review_revision,
                    snapshot_payload=card_centric_snapshot,
                )
            session.flush()
            return SavedReview(job_id=job_id, revision=job.review_revision)

    def _persist_reviewed_v3_snapshot(
        self,
        session: Session,
        job_id: UUID,
        *,
        revision: int,
        snapshot_payload: dict[str, Any],
        r11_artifact_sha256: str,
        cost_ledger_sha256: str,
    ) -> None:
        """Reconcile v3 persisted rows in the review transaction, without dispatch."""
        snapshot = V3ReviewSnapshot.model_validate(snapshot_payload)
        candidates = session.scalars(
            select(AnkiCandidateModel).where(AnkiCandidateModel.job_id == str(job_id))
        ).all()
        cards = session.scalars(
            select(AnkiGapCardModel).where(AnkiGapCardModel.job_id == str(job_id))
        ).all()
        candidate_rows = {item.note_id: item for item in candidates}
        card_rows = {item.id: item for item in cards}
        existing: list[dict[str, Any]] = []
        for item in snapshot.existing_candidates:
            value = dict(item)
            note_id = value.get("note_id")
            if not isinstance(note_id, int) or note_id not in candidate_rows:
                raise ValueError("v3 review candidate is not a persisted row")
            value["selected"] = candidate_rows[note_id].selected
            existing.append(value)
        generated: list[dict[str, Any]] = []
        for item in snapshot.generated_cards:
            value = dict(item)
            card_id = value.get("card_id")
            if not isinstance(card_id, str):
                if value.get("status") == "unresolved":
                    generated.append(value)
                    continue
                raise ValueError("v3 review generated card is not a persisted row")
            if card_id not in card_rows:
                raise ValueError("v3 review generated card is not a persisted row")
            stored = card_rows[card_id]
            value.update(text=stored.text, extra=stored.extra, selected=stored.selected)
            generated.append(value)
        visible_note_ids = {int(item["note_id"]) for item in snapshot.existing_candidates}
        visible_card_ids = {str(item["card_id"]) for item in snapshot.generated_cards}
        reviewed = V3ReviewSnapshot.model_validate(
            {
                **snapshot.canonical_payload(),
                "existing_candidates": existing,
                "generated_cards": generated,
                "selected_existing_note_ids": sorted(
                    item.note_id
                    for item in candidates
                    if item.selected and item.note_id in visible_note_ids
                ),
                "selected_generated_card_ids": sorted(
                    item.id for item in cards if item.selected and item.id in visible_card_ids
                ),
                "snapshot_sha256": "",
            }
        )
        reconciliation = reconcile_v3(reviewed)
        session.add(
            AnkiReviewedReconciliationModel(
                job_id=str(job_id),
                review_revision=revision,
                payload_json=_canonical_json(
                    {
                        "contract_version": "card_centric_v3_r11",
                        "r11_artifact_sha256": r11_artifact_sha256,
                        "r11_snapshot_sha256": reviewed.snapshot_sha256,
                        "cost_ledger_sha256": cost_ledger_sha256,
                        **reconciliation.model_dump(mode="json"),
                    }
                ),
            )
        )

    def _persist_reviewed_card_centric_snapshot(
        self,
        session: Session,
        job_id: UUID,
        *,
        revision: int,
        snapshot_payload: dict[str, Any],
    ) -> None:
        """Materialize selected-only S9 in the review transaction."""
        snapshot = CardCentricReconciliationInput.model_validate(snapshot_payload)
        candidates = session.scalars(
            select(AnkiCandidateModel).where(AnkiCandidateModel.job_id == str(job_id))
        ).all()
        cards = session.scalars(
            select(AnkiGapCardModel).where(AnkiGapCardModel.job_id == str(job_id))
        ).all()
        selected_nids = tuple(item.note_id for item in candidates if item.selected)
        selected_cards = tuple(item.id for item in cards if item.selected)
        canonical_generated = snapshot.canonical_generated_cards or snapshot.generated_cards
        canonical_by_card_id = {item.card_id: item for item in canonical_generated}
        unknown_selected_cards = set(selected_cards) - set(canonical_by_card_id)
        if unknown_selected_cards:
            raise ValueError("selected generated card is not in the pinned S9 output")
        stored_by_card_id = {item.id: item for item in cards}
        reviewed_generated_cards = tuple(
            canonical_by_card_id[card_id].model_copy(
                update={
                    "text": stored_by_card_id[card_id].text,
                    "extra": stored_by_card_id[card_id].extra,
                }
            )
            for card_id in selected_cards
        )
        reviewed = snapshot.model_copy(
            update={
                "selected_nids": selected_nids,
                "selected_generated_card_ids": selected_cards,
                # Current review text is validated, while fact/split identity
                # remains pinned to the original S7/S8 card resolution.
                "generated_cards": reviewed_generated_cards,
                "overflow_acknowledgement": None,
            }
        )
        reviewed = reviewed.model_copy(
            update={"coverage": selected_card_centric_coverage(reviewed)}
        )
        report = reconcile_card_centric(reviewed)
        payload = {
            "contract_version": "card_centric_s9_v1",
            **report.model_dump(mode="json"),
            "selection": {
                "selected_existing_note_ids": list(selected_nids),
                "selected_generated_card_ids": list(selected_cards),
                "cap": reviewed.cap,
                "target": reviewed.target,
                "mandatory_note_ids": list(reviewed.mandatory_nids),
                "mandatory_generated_card_ids": list(reviewed.mandatory_generated_card_ids),
                "overflow_acknowledgement": None,
            },
            "snapshot": reviewed.model_dump(mode="json"),
        }
        session.add(
            AnkiReviewedReconciliationModel(
                job_id=str(job_id), review_revision=revision, payload_json=_canonical_json(payload)
            )
        )

    def list_tag_patches(self, job_id: UUID) -> list[TagPatch]:
        with self.database.session() as session:
            stored = session.scalars(
                select(AnkiTagPatchModel)
                .where(AnkiTagPatchModel.job_id == str(job_id))
                .order_by(
                    AnkiTagPatchModel.revision,
                    AnkiTagPatchModel.id,
                )
            ).all()
            return [
                TagPatch(
                    note_id=patch.note_id,
                    before=tuple(json.loads(patch.before_json)),
                    after=tuple(json.loads(patch.after_json)),
                    add_tags=tuple(json.loads(patch.add_tags_json)),
                    remove_tags=tuple(json.loads(patch.remove_tags_json)),
                    expected_tag_hash=patch.expected_tag_hash,
                    tag_policy_version=patch.policy_version,
                )
                for patch in stored
            ]

    def list_review_changes(
        self,
        job_id: UUID,
    ) -> list[StoredReviewChange]:
        with self.database.session() as session:
            stored = session.scalars(
                select(AnkiReviewChangeSetModel)
                .where(AnkiReviewChangeSetModel.job_id == str(job_id))
                .order_by(AnkiReviewChangeSetModel.revision)
            ).all()
            return [
                StoredReviewChange(
                    job_id=UUID(change.job_id),
                    revision=change.revision,
                    prior_revision=change.prior_revision,
                    reviewer=change.reviewer,
                    payload=cast(
                        dict[str, Any],
                        json.loads(change.payload_json),
                    ),
                    created_at=change.created_at,
                )
                for change in stored
            ]

    def get_judgment_cache(
        self,
        cache_key: str,
    ) -> JudgmentCacheRecord | None:
        with self.database.session() as session:
            stored = session.get(
                AnkiCoverageJudgmentCacheModel,
                cache_key,
            )
            return None if stored is None else self._judgment_cache_record(stored)

    def save_judgment_cache(
        self,
        record: JudgmentCacheRecord,
    ) -> None:
        with self.database.session() as session:
            stored = session.get(
                AnkiCoverageJudgmentCacheModel,
                record.cache_key,
            )
            if stored is not None:
                if self._judgment_cache_record(stored) != record:
                    raise ValueError("judgment cache key has conflicting content")
                return
            session.add(
                AnkiCoverageJudgmentCacheModel(
                    cache_key=record.cache_key,
                    concept_content_hash=record.concept_content_hash,
                    candidate_digest=record.candidate_digest,
                    prompt_version=record.prompt_version,
                    provider=record.provider.value,
                    model=record.model,
                    result_json=_canonical_json(record.result),
                    input_tokens=record.input_tokens,
                    output_tokens=record.output_tokens,
                    cost_microusd=record.cost_microusd,
                    created_at=record.created_at,
                )
            )

    @staticmethod
    def _judgment_cache_record(
        stored: AnkiCoverageJudgmentCacheModel,
    ) -> JudgmentCacheRecord:
        return JudgmentCacheRecord(
            cache_key=stored.cache_key,
            concept_content_hash=stored.concept_content_hash,
            candidate_digest=stored.candidate_digest,
            prompt_version=stored.prompt_version,
            provider=ProviderName(stored.provider),
            model=stored.model,
            result=dict(json.loads(stored.result_json)),
            input_tokens=stored.input_tokens,
            output_tokens=stored.output_tokens,
            cost_microusd=stored.cost_microusd,
            created_at=stored.created_at,
        )

    def get_audit_cache(
        self,
        cache_key: str,
    ) -> AuditCacheRecord | None:
        with self.database.session() as session:
            stored = session.get(AnkiCardAuditCacheModel, cache_key)
            return None if stored is None else self._audit_cache_record(stored)

    def save_audit_cache(self, record: AuditCacheRecord) -> None:
        with self.database.session() as session:
            stored = session.get(AnkiCardAuditCacheModel, record.cache_key)
            if stored is not None:
                if self._audit_cache_record(stored) != record:
                    raise ValueError("audit cache key has conflicting content")
                return
            session.add(
                AnkiCardAuditCacheModel(
                    cache_key=record.cache_key,
                    note_id=record.note_id,
                    lecture_id=record.lecture_id,
                    note_content_hash=record.note_content_hash,
                    source_digest=record.source_digest,
                    prompt_hash=record.prompt_hash,
                    provider=record.provider.value,
                    model=record.model,
                    result_json=_canonical_json(record.result),
                    input_tokens=record.input_tokens,
                    output_tokens=record.output_tokens,
                    cost_microusd=record.cost_microusd,
                    created_at=record.created_at,
                )
            )

    @staticmethod
    def _audit_cache_record(
        stored: AnkiCardAuditCacheModel,
    ) -> AuditCacheRecord:
        return AuditCacheRecord(
            cache_key=stored.cache_key,
            note_id=stored.note_id,
            lecture_id=stored.lecture_id,
            note_content_hash=stored.note_content_hash,
            source_digest=stored.source_digest,
            prompt_hash=stored.prompt_hash,
            provider=ProviderName(stored.provider),
            model=stored.model,
            result=dict(json.loads(stored.result_json)),
            input_tokens=stored.input_tokens,
            output_tokens=stored.output_tokens,
            cost_microusd=stored.cost_microusd,
            created_at=stored.created_at,
        )

    def create_envelope(
        self,
        job_id: UUID,
        envelope: EnvelopeDraft,
    ) -> StoredEnvelope:
        with self.database.session() as session:
            self._require_job_model(session, job_id)
            existing = session.scalar(
                select(AnkiEnvelopeModel).where(AnkiEnvelopeModel.job_id == str(job_id))
            )
            if existing is not None:
                raise ValueError("job already has an envelope")
            envelope_id = str(UUID(envelope.envelope_id))
            payload_document = {
                "payload": envelope.payload,
                "operations": [
                    {
                        "id": operation.operation_id,
                        "type": operation.operation_type,
                        "payload": operation.payload,
                    }
                    for operation in envelope.operations
                ],
            }
            payload_json = _canonical_json(payload_document)
            stored = AnkiEnvelopeModel(
                id=envelope_id,
                job_id=str(job_id),
                payload_json=payload_json,
                payload_sha256=_sha256_text(payload_json),
                snapshot_id=envelope.snapshot_id,
                state="pending",
            )
            session.add(stored)
            for position, operation in enumerate(envelope.operations):
                operation_id = str(UUID(operation.operation_id))
                operation_json = _canonical_json(operation.payload)
                session.add(
                    AnkiEnvelopeOperationModel(
                        id=operation_id,
                        envelope_id=envelope_id,
                        position=position,
                        operation_type=operation.operation_type,
                        content_hash=_sha256_text(
                            _canonical_json(
                                {
                                    "type": operation.operation_type,
                                    "payload": operation.payload,
                                }
                            )
                        ),
                        payload_json=operation_json,
                    )
                )
            session.flush()
            return self._envelope(stored)

    def record_receipt(
        self,
        envelope_id: UUID,
        receipt: dict[str, Any],
    ) -> StoredEnvelope:
        with self.database.session() as session:
            stored = session.get(AnkiEnvelopeModel, str(envelope_id))
            if stored is None:
                raise KeyError(str(envelope_id))
            stored.receipt_summary_json = _canonical_json(receipt)
            stored.state = "complete"
            session.flush()
            return self._envelope(stored)

    def create_action_envelope(
        self,
        job_id: UUID,
        envelope: ActionEnvelopeDocument,
        *,
        expected_review_revision: int | None = None,
    ) -> StoredEnvelope:
        if canonical_payload_sha256(envelope) != envelope.payload_sha256:
            raise ValueError("action envelope payload hash does not match")
        payload_json = _canonical_json(envelope.model_dump(mode="json"))
        with self.database.session() as session:
            job = self._require_job_model(session, job_id)
            if (
                job.pipeline_contract_version == PipelineContractVersion.CARD_CENTRIC_V3.value
                and envelope.contract_version != 2
            ):
                raise ValueError(
                    "v3 jobs require a v3-bound action envelope; no mutation performed"
                )
            if job.pipeline_contract_version == PipelineContractVersion.CARD_CENTRIC_V3.value and (
                not isinstance(envelope, ActionEnvelopeV2)
                or not self._valid_v3_envelope(session, job, envelope)
            ):
                raise ValueError(
                    "v3 envelope does not exactly bind the committed review; no mutation performed"
                )
            if envelope.contract_version == 2:
                if envelope.job_id != job_id:
                    raise ValueError(
                        "action envelope job ID does not match caller job; no mutation performed"
                    )
                supported = self.supported_envelope_versions
                if supported is None:
                    agent = session.get(AnkiAgentStateModel, 1)
                    versions = (
                        cast(dict[str, Any], json.loads(agent.versions_json))
                        if agent is not None
                        else {}
                    )
                    supported = frozenset(
                        versions.get("supported_envelope_contract_versions", [1])
                    )
                if 2 not in supported:
                    raise ValueError(
                        "envelope contract v2 unsupported; upgrade required; no mutation performed"
                    )
                if envelope.pipeline_contract_version != job.pipeline_contract_version:
                    raise ValueError(
                        "action envelope pipeline contract does not match job; "
                        "no mutation performed"
                    )
                if envelope.model_config_sha256 != job.model_config_sha256:
                    raise ValueError(
                        "action envelope model configuration does not match job; "
                        "no mutation performed"
                    )
                if envelope.resolved_model_config and (
                    _sha256_text(_canonical_json(envelope.resolved_model_config))
                    != job.model_config_sha256
                ):
                    raise ValueError(
                        "action envelope resolved model configuration does not match job; "
                        "no mutation performed"
                    )
                if envelope.review_revision != job.review_revision:
                    raise ValueError(
                        "action envelope review revision does not match job; no mutation performed"
                    )
            if expected_review_revision is not None and (
                job.state != CurationState.READY_FOR_REVIEW.value
                or job.review_revision != expected_review_revision
            ):
                raise InvalidCurationTransition(
                    "review changed or is no longer ready for an envelope"
                )
            existing = session.scalar(
                select(AnkiEnvelopeModel).where(AnkiEnvelopeModel.job_id == str(job_id))
            )
            if existing is not None:
                raise ValueError("job already has an envelope")
            stored = AnkiEnvelopeModel(
                id=str(envelope.envelope_id),
                job_id=str(job_id),
                payload_json=payload_json,
                payload_sha256=envelope.payload_sha256,
                snapshot_id=envelope.snapshot_id,
                state=ApplyState.PENDING.value,
            )
            session.add(stored)
            for position, operation in enumerate(envelope.operations):
                session.add(
                    AnkiEnvelopeOperationModel(
                        id=str(operation.operation_id),
                        envelope_id=str(envelope.envelope_id),
                        position=position,
                        operation_type=operation.operation_type,
                        content_hash=operation.content_sha256,
                        payload_json=_canonical_json(operation.model_dump(mode="json")),
                        state="pending",
                    )
                )
            job.apply_state = ApplyState.PENDING.value
            if expected_review_revision is not None:
                job.state = CurationState.ENVELOPE_PENDING.value
            session.flush()
            return self._envelope(stored)

    def get_job_envelope(self, job_id: UUID) -> StoredEnvelope | None:
        with self.database.session() as session:
            self._require_job_model(session, job_id)
            stored = session.scalar(
                select(AnkiEnvelopeModel).where(AnkiEnvelopeModel.job_id == str(job_id))
            )
            return None if stored is None else self._envelope(stored)

    def get_envelope(self, envelope_id: UUID) -> ActionEnvelopeDocument:
        with self.database.session() as session:
            stored = session.get(AnkiEnvelopeModel, str(envelope_id))
            if stored is None:
                raise KeyError(str(envelope_id))
            try:
                envelope = parse_action_envelope(stored.payload_json)
            except ValueError as exc:
                raise ValueError("stored envelope is not an action envelope") from exc
            if (
                envelope.envelope_id != envelope_id
                or envelope.payload_sha256 != stored.payload_sha256
                or canonical_payload_sha256(envelope) != envelope.payload_sha256
            ):
                raise ValueError("stored action envelope failed integrity checks")
            return envelope

    def operation_record(
        self,
        envelope_id: UUID,
        operation_id: UUID,
    ) -> ApplyOperationRecord:
        with self.database.session() as session:
            stored = self._require_envelope_operation(
                session,
                envelope_id,
                operation_id,
            )
            result = (
                cast(dict[str, Any], json.loads(stored.result_json))
                if stored.result_json is not None
                else None
            )
            return ApplyOperationRecord(
                state=stored.state,
                attempts=stored.attempts,
                result=result,
                error=stored.error,
            )

    def begin_operation(
        self,
        envelope_id: UUID,
        operation_id: UUID,
    ) -> None:
        with self.database.session() as session:
            stored = self._require_envelope_operation(
                session,
                envelope_id,
                operation_id,
            )
            stored.state = "intent"
            stored.attempts += 1
            stored.error = None

    def complete_operation(
        self,
        envelope_id: UUID,
        operation_id: UUID,
        result: dict[str, Any],
    ) -> None:
        with self.database.session() as session:
            stored = self._require_envelope_operation(
                session,
                envelope_id,
                operation_id,
            )
            stored.state = "complete"
            stored.result_json = _canonical_json(result)
            stored.error = None

    def fail_operation(
        self,
        envelope_id: UUID,
        operation_id: UUID,
        error: str,
    ) -> None:
        with self.database.session() as session:
            stored = self._require_envelope_operation(
                session,
                envelope_id,
                operation_id,
            )
            stored.state = "failed"
            stored.error = error[:2_000]

    def set_apply_state(
        self,
        envelope_id: UUID,
        state: ApplyState,
        summary: dict[str, Any],
    ) -> None:
        with self.database.session() as session:
            stored = session.get(AnkiEnvelopeModel, str(envelope_id))
            if stored is None:
                raise KeyError(str(envelope_id))
            job = self._require_job_model(session, UUID(stored.job_id))
            stored.state = state.value
            stored.receipt_summary_json = _canonical_json(summary)
            job.apply_state = state.value

    def record_agent_heartbeat(
        self,
        *,
        agent_id: str,
        heartbeat_at: str,
        versions: dict[str, Any],
        active_snapshot_id: str | None,
        health: dict[str, Any],
    ) -> AgentState:
        with self.database.session() as session:
            stored = session.get(AnkiAgentStateModel, 1)
            if stored is None:
                stored = AnkiAgentStateModel(
                    id=1,
                    versions_json="{}",
                    health_json="{}",
                )
                session.add(stored)
            stored.agent_id = agent_id
            stored.heartbeat_at = heartbeat_at
            stored.versions_json = _canonical_json(versions)
            stored.active_snapshot_id = active_snapshot_id
            stored.health_json = _canonical_json(health)
            session.flush()
            return self._agent_state(stored)

    def agent_state(self) -> AgentState:
        with self.database.session() as session:
            stored = session.get(AnkiAgentStateModel, 1)
            if stored is None:
                return AgentState(
                    agent_id=None,
                    heartbeat_at=None,
                    versions={},
                    active_snapshot_id=None,
                    health={},
                )
            return self._agent_state(stored)

    def queue_agent_command(
        self,
        command_type: AgentCommandType,
        payload: dict[str, Any],
    ) -> StoredAgentCommand:
        payload_json = _canonical_json(payload)
        with self.database.session() as session:
            stored = AnkiAgentCommandModel(
                id=str(uuid4()),
                command_type=command_type.value,
                state="queued",
                payload_json=payload_json,
                payload_sha256=_sha256_text(payload_json),
            )
            session.add(stored)
            session.flush()
            return self._agent_command(stored)

    def claim_next_agent_command(
        self,
        agent_id: str,
        now: datetime,
    ) -> StoredAgentCommand | None:
        with self.database.session() as session:
            stored = session.scalar(
                select(AnkiAgentCommandModel)
                .where(AnkiAgentCommandModel.state == "queued")
                .order_by(
                    AnkiAgentCommandModel.created_at,
                    AnkiAgentCommandModel.id,
                )
                .limit(1)
            )
            if stored is None:
                return None
            claimed = session.execute(
                update(AnkiAgentCommandModel)
                .where(
                    AnkiAgentCommandModel.id == stored.id,
                    AnkiAgentCommandModel.state == "queued",
                )
                .values(
                    state="claimed",
                    owner_agent_id=agent_id,
                    claimed_at=now.isoformat(),
                    error=None,
                )
            )
            if cast(CursorResult[Any], claimed).rowcount != 1:
                return None
            session.flush()
            session.refresh(stored)
            return self._agent_command(stored)

    def require_owned_agent_command(
        self,
        command_id: UUID,
        agent_id: str,
        allowed_types: set[AgentCommandType],
    ) -> StoredAgentCommand:
        with self.database.session() as session:
            stored = session.get(AnkiAgentCommandModel, str(command_id))
            if stored is None:
                raise KeyError(str(command_id))
            if (
                stored.state != "claimed"
                or stored.owner_agent_id != agent_id
                or AgentCommandType(stored.command_type) not in allowed_types
            ):
                raise ValueError("agent command is not owned by this agent")
            return self._agent_command(stored)

    def complete_agent_command(
        self,
        command_id: UUID,
        agent_id: str,
        result: dict[str, Any],
    ) -> StoredAgentCommand:
        result_json = _canonical_json(result)
        with self.database.session() as session:
            stored = session.get(AnkiAgentCommandModel, str(command_id))
            if stored is None:
                raise KeyError(str(command_id))
            if stored.state != "claimed" or stored.owner_agent_id != agent_id:
                raise ValueError("agent command is not owned by this agent")
            stored.state = "complete"
            stored.result_json = result_json
            stored.result_sha256 = _sha256_text(result_json)
            stored.completed_at = utc_now()
            session.flush()
            return self._agent_command(stored)

    @staticmethod
    def _require_job_model(session: Session, job_id: UUID) -> AnkiCurationJobModel:
        stored = session.get(AnkiCurationJobModel, str(job_id))
        if stored is None:
            raise KeyError(str(job_id))
        return stored

    @staticmethod
    def _replace_candidate_models(
        session: Session,
        job_id: UUID,
        candidates: Sequence[Candidate],
    ) -> None:
        if len({candidate.note_id for candidate in candidates}) != len(candidates):
            raise ValueError("projected candidates must have unique note IDs")
        session.execute(delete(AnkiCandidateModel).where(AnkiCandidateModel.job_id == str(job_id)))
        for candidate in candidates:
            session.add(
                AnkiCandidateModel(
                    job_id=str(job_id),
                    note_id=candidate.note_id,
                    content_hash=candidate.content_hash,
                    best_concept_id=candidate.best_concept_id,
                    provenance_json=_canonical_json(candidate.provenance),
                    scores_json=_canonical_json(candidate.scores),
                    predicted_band=candidate.predicted_band,
                    verdict=candidate.verdict,
                    confidence=candidate.confidence,
                    reason=candidate.reason,
                    context_trap=candidate.context_trap,
                    recall_direction=candidate.recall_direction,
                    mnemonic_classification=(candidate.mnemonic_classification),
                    dedupe_disposition=candidate.dedupe_disposition,
                    selected=candidate.selected,
                    retrieval_pass=candidate.retrieval_pass.value,
                )
            )

    @staticmethod
    def _replace_source_evidence_models(
        session: Session,
        job_id: UUID,
        evidence: Sequence[SourceEvidence],
    ) -> None:
        if len({item.evidence_id for item in evidence}) != len(evidence):
            raise ValueError("projected source evidence IDs must be unique")
        session.execute(
            delete(AnkiSourceEvidenceModel).where(AnkiSourceEvidenceModel.job_id == str(job_id))
        )
        for item in evidence:
            session.add(
                AnkiSourceEvidenceModel(
                    job_id=str(job_id),
                    evidence_id=item.evidence_id,
                    concept_id=item.concept_id,
                    support=item.support.value,
                    statement=item.statement,
                    source_refs_json=_canonical_json(
                        [
                            {
                                "source_kind": ref.source_kind.value,
                                "revision_id": ref.revision_id,
                                "locator": ref.locator,
                                "content_hash": ref.content_hash,
                            }
                            for ref in item.source_refs
                        ]
                    ),
                    content_hash=item.content_hash,
                )
            )

    @staticmethod
    def _replace_gap_card_models(
        session: Session,
        job_id: UUID,
        cards: Sequence[GapCard],
    ) -> None:
        supplied_ids = [card.card_id for card in cards if card.card_id]
        if len(supplied_ids) != len(set(supplied_ids)):
            raise ValueError("projected gap cards must have unique card IDs")
        session.execute(delete(AnkiGapCardModel).where(AnkiGapCardModel.job_id == str(job_id)))
        for card in cards:
            session.add(
                AnkiGapCardModel(
                    id=card.card_id or str(uuid4()),
                    job_id=str(job_id),
                    concept_id=card.concept_id,
                    text=card.text,
                    extra=card.extra,
                    revision=card.revision,
                    selected=card.selected,
                    image_state=card.image_state,
                    media_filename=card.media_filename,
                    source_note_id=card.source_note_id,
                    generated_image_json=_canonical_json(card.generated_image),
                    validation_state=card.validation_state,
                    source_refs_json=_canonical_json(
                        [
                            {
                                "source_kind": ref.source_kind.value,
                                "revision_id": ref.revision_id,
                                "locator": ref.locator,
                                "content_hash": ref.content_hash,
                            }
                            for ref in card.source_refs
                        ]
                    ),
                    evidence_ids_json=_canonical_json(card.evidence_ids),
                    provenance_json=_canonical_json(card.provenance),
                    initial_tags_json=_canonical_json(card.initial_tags),
                    content_hash=card.content_hash or _sha256_text(f"{card.text}\0{card.extra}"),
                )
            )

    @staticmethod
    def _require_envelope_operation(
        session: Session,
        envelope_id: UUID,
        operation_id: UUID,
    ) -> AnkiEnvelopeOperationModel:
        stored = session.get(
            AnkiEnvelopeOperationModel,
            str(operation_id),
        )
        if stored is None or stored.envelope_id != str(envelope_id):
            raise KeyError(str(operation_id))
        return stored

    @staticmethod
    def _require_stage(
        session: Session,
        job_id: UUID,
        stage: CurationStage,
    ) -> AnkiJobStageModel:
        stored = session.scalar(
            select(AnkiJobStageModel).where(
                AnkiJobStageModel.job_id == str(job_id),
                AnkiJobStageModel.stage == stage.value,
            )
        )
        if stored is None:
            raise KeyError(f"{job_id}:{stage.value}")
        return stored

    @staticmethod
    def _pinned_lecture_metadata(lecture: LectureModel) -> PinnedLectureMetadata:
        title = (
            f"{lecture.subject} Exam {lecture.exam_number} "
            f"Lecture {lecture.lecture_number}: {lecture.topic}"
        )
        metadata = CanonicalJsonObject.from_mapping(
            {
                "campus": lecture.campus,
                "exam_date": lecture.exam_date,
                "exam_number": lecture.exam_number,
                "lecture_number": lecture.lecture_number,
                "lecturer": lecture.lecturer,
                "scheduled_start_utc": lecture.scheduled_start_utc,
                "subject": lecture.subject,
                "topic": lecture.topic,
            }
        )
        document = {
            "lecture_id": lecture.id,
            "title": title,
            "metadata": metadata.as_dict(),
        }
        return PinnedLectureMetadata(
            lecture_id=lecture.id,
            title=title,
            metadata=metadata,
            metadata_sha256=_sha(document),
        )

    @classmethod
    def _load_or_pin_v2_lecture_metadata(
        cls, session: Session, job: AnkiCurationJobModel
    ) -> PinnedLectureMetadata:
        values = (
            job.lecture_title_snapshot,
            job.lecture_metadata_json,
            job.lecture_metadata_sha256,
        )
        if all(value is None for value in values):
            lecture = session.get(LectureModel, job.lecture_id)
            if lecture is None:
                raise KeyError(job.lecture_id)
            pinned = cls._pinned_lecture_metadata(lecture)
            # Compatibility behavior for rows written before schema v19: the
            # first prepared bundle captures the live lecture exactly once.
            job.lecture_title_snapshot = pinned.title
            job.lecture_metadata_json = pinned.metadata.canonical_json
            job.lecture_metadata_sha256 = pinned.metadata_sha256
            session.flush()
            return pinned
        if any(value is None for value in values):
            raise ValueError("stored pinned lecture metadata is incomplete")
        try:
            return PinnedLectureMetadata(
                lecture_id=job.lecture_id,
                title=cast(str, job.lecture_title_snapshot),
                metadata=CanonicalJsonObject(canonical_json=cast(str, job.lecture_metadata_json)),
                metadata_sha256=cast(str, job.lecture_metadata_sha256),
            )
        except ValueError as exc:
            raise ValueError("stored pinned lecture metadata failed integrity checks") from exc

    @classmethod
    def _a11_history_snapshot(
        cls,
        session: Session,
        job_id: UUID,
        lecture_id: int,
        *,
        limit: int,
    ) -> A11HistorySnapshot:
        rows = session.execute(
            select(AnkiReviewedReconciliationModel)
            .join(
                AnkiCurationJobModel,
                AnkiCurationJobModel.id == AnkiReviewedReconciliationModel.job_id,
            )
            .where(
                AnkiCurationJobModel.lecture_id == lecture_id,
                AnkiCurationJobModel.pipeline_contract_version
                == PipelineContractVersion.CARD_CENTRIC_V2.value,
                AnkiReviewedReconciliationModel.job_id != str(job_id),
            )
            .order_by(
                AnkiReviewedReconciliationModel.job_id,
                AnkiReviewedReconciliationModel.review_revision.desc(),
                AnkiReviewedReconciliationModel.id.desc(),
            )
        ).scalars()
        latest_by_job: dict[str, A11HistoryEntry] = {}
        for row in rows:
            if row.job_id in latest_by_job:
                continue
            entry = cls._a11_history_entry(row)
            if entry is not None:
                latest_by_job[row.job_id] = entry
        entries = tuple(
            sorted(
                latest_by_job.values(),
                key=lambda entry: (entry.reviewed_at, str(entry.job_id)),
                reverse=True,
            )[:limit]
        )
        serialized_entries = [entry.model_dump(mode="json") for entry in entries]
        return A11HistorySnapshot(
            entries=entries,
            snapshot_sha256=_sha256_text(_canonical_json(serialized_entries)),
        )

    @staticmethod
    def _a11_history_entry(
        row: AnkiReviewedReconciliationModel,
    ) -> A11HistoryEntry | None:
        try:
            snapshot = cast(dict[str, Any], json.loads(row.payload_json))["snapshot"]
            classifications = cast(list[dict[str, Any]], snapshot["classifications"])
            if not classifications:
                return None
            rate = sum(item.get("verdict") == "keep" for item in classifications) / len(
                classifications
            )
            return A11HistoryEntry(
                job_id=UUID(row.job_id),
                review_revision=row.review_revision,
                yes_rate=rate,
                reviewed_at=datetime.fromisoformat(row.created_at),
            )
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            return None

    @classmethod
    def _stage_replay_document(
        cls,
        session: Session,
        job: AnkiCurationJobModel,
        job_id: UUID,
        stage: CurationStage,
    ) -> dict[str, Any]:
        document: dict[str, Any] = {"schema_version": 1, "stage": stage.value}
        if job.pipeline_contract_version == PipelineContractVersion.CARD_CENTRIC_V2.value:
            document["pinned_lecture"] = cls._load_or_pin_v2_lecture_metadata(
                session, job
            ).model_dump(mode="json")
            if stage is CurationStage.RECONCILIATION:
                document["a11_history"] = cls._a11_history_snapshot(
                    session, job_id, job.lecture_id, limit=12
                ).model_dump(mode="json")
        return document

    @staticmethod
    def _prepared_stage_replay_inputs(
        stored: AnkiStageReplayInputModel,
    ) -> PreparedStageReplayInputs:
        return PreparedStageReplayInputs(
            job_id=UUID(stored.job_id),
            stage=CurationStage(stored.stage),
            canonical_json=stored.canonical_json,
            sha256=stored.sha256,
        )

    @staticmethod
    def _job(stored: AnkiCurationJobModel) -> CurationJob:
        config = AnkiCurationRepository._resolved_model_config(
            stored.resolved_model_config_json, stored.provider, stored.model
        )
        pipeline_contract_version = PipelineContractVersion(stored.pipeline_contract_version)
        if pipeline_contract_version is PipelineContractVersion.CARD_CENTRIC_V2:
            config.require_card_centric_v2_fast_classifier()
        rate_document = (
            None if stored.v3_rate_table_json is None else json.loads(stored.v3_rate_table_json)
        )
        if pipeline_contract_version is PipelineContractVersion.CARD_CENTRIC_V3:
            try:
                table = FrozenRateTable.from_document(cast(dict[str, object], rate_document))
            except (TypeError, ValueError) as exc:
                raise ValueError("stored card-centric-v3 rate table is invalid") from exc
            if table.rate_table_sha256 != stored.v3_rate_table_sha256:
                raise ValueError("stored card-centric-v3 rate table hash changed")
        expected_model_config_sha256 = _sha256_text(_canonical_json(config.canonical_document()))
        if stored.resolved_model_config_json not in {"", "{}"} and (
            stored.model_config_sha256 != expected_model_config_sha256
        ):
            raise ValueError("stored resolved model configuration failed integrity checks")
        return CurationJob(
            id=UUID(stored.id),
            lecture_id=stored.lecture_id,
            state=CurationState(stored.state),
            attempts=stored.attempts,
            block_id=stored.block_id,
            source_revision_ids=tuple(
                int(value) for value in json.loads(stored.source_revision_ids_json)
            ),
            source_revision_hashes={
                int(key): str(value)
                for key, value in cast(
                    dict[str, Any],
                    json.loads(stored.source_revision_hashes_json),
                ).items()
            },
            deck_allowlist=tuple(str(value) for value in json.loads(stored.deck_allowlist_json)),
            tag_allowlist=tuple(str(value) for value in json.loads(stored.tag_allowlist_json)),
            provider=stored.provider,
            model=stored.model,
            pipeline_contract_version=pipeline_contract_version,
            resolved_model_config=config,
            model_config_sha256=expected_model_config_sha256,
            instruction_text=stored.instruction_text,
            instruction_sha256=stored.instruction_sha256,
            target_deck=stored.target_deck,
            target_tag=stored.target_tag,
            index_snapshot_id=stored.index_snapshot_id,
            lcl_prompt_version=stored.lcl_prompt_version,
            judgment_rubric_version=stored.judgment_rubric_version,
            gap_prompt_version=stored.gap_prompt_version,
            semantic_generation=stored.semantic_generation,
            companion_generation=stored.companion_generation,
            source_index_generation=stored.source_index_generation,
            configuration_sha256=stored.configuration_sha256,
            apply_state=ApplyState(stored.apply_state),
            review_revision=stored.review_revision,
            error=stored.error,
            lease_owner=stored.lease_owner,
            lease_expires_at=stored.lease_expires_at,
            available_at=stored.available_at,
            created_at=stored.created_at,
            updated_at=stored.updated_at,
            summary_outline_id=stored.summary_outline_id,
            summary_outline_sha256=stored.summary_outline_sha256,
            policy_sha256=stored.policy_sha256,
            rate_table_document=rate_document,
            rate_table_sha256=stored.v3_rate_table_sha256,
            offline_replay_only=stored.offline_replay_only,
        )

    @staticmethod
    def _course_policy(stored: CourseCurationPolicyModel) -> CourseCurationPolicy:
        payload = cast(dict[str, Any], json.loads(stored.payload_json))
        policy = CourseCurationPolicy.model_validate(
            {**payload, "policy_sha256": stored.policy_sha256}
        )
        if _canonical_json(policy.canonical_payload()) != stored.payload_json:
            raise ValueError("stored course policy payload failed canonical integrity checks")
        if policy.policy_id != stored.policy_id or policy.revision != stored.revision:
            raise ValueError("stored course policy identity failed integrity checks")
        return policy

    @staticmethod
    def _stage(stored: AnkiJobStageModel) -> JobStage:
        return JobStage(
            job_id=UUID(stored.job_id),
            stage=CurationStage(stored.stage),
            state=stored.state,
            attempt_count=stored.attempt_count,
            provider=stored.provider,
            model=stored.model,
            request_id=stored.request_id,
            input_tokens=stored.input_tokens,
            output_tokens=stored.output_tokens,
            cost_microusd=stored.cost_microusd,
            cache_hits=stored.cache_hits,
            error=stored.error,
        )

    @staticmethod
    def _candidate(stored: AnkiCandidateModel) -> Candidate:
        return Candidate(
            note_id=stored.note_id,
            content_hash=stored.content_hash,
            best_concept_id=stored.best_concept_id,
            provenance=cast(dict[str, Any], json.loads(stored.provenance_json)),
            scores=cast(dict[str, float], json.loads(stored.scores_json)),
            predicted_band=stored.predicted_band,
            verdict=stored.verdict,
            confidence=stored.confidence,
            reason=stored.reason,
            context_trap=stored.context_trap,
            recall_direction=stored.recall_direction,
            mnemonic_classification=stored.mnemonic_classification,
            dedupe_disposition=stored.dedupe_disposition,
            selected=stored.selected,
            retrieval_pass=RetrievalPass(stored.retrieval_pass),
        )

    @staticmethod
    def _source_reference(value: dict[str, Any]) -> SourceReference:
        return SourceReference(
            source_kind=SourceKind(str(value["source_kind"])),
            revision_id=int(value["revision_id"]),
            locator=str(value["locator"]),
            content_hash=str(value["content_hash"]),
        )

    @classmethod
    def _source_evidence(
        cls,
        stored: AnkiSourceEvidenceModel,
    ) -> SourceEvidence:
        source_refs = cast(
            list[dict[str, Any]],
            json.loads(stored.source_refs_json),
        )
        return SourceEvidence(
            evidence_id=stored.evidence_id,
            concept_id=stored.concept_id,
            support=EvidenceSupport(stored.support),
            statement=stored.statement,
            source_refs=tuple(cls._source_reference(value) for value in source_refs),
            content_hash=stored.content_hash,
        )

    @staticmethod
    def _stage_artifact(stored: AnkiStageArtifactModel) -> StageArtifact:
        return StageArtifact(
            artifact_id=stored.artifact_id,
            stage=CurationStage(stored.stage),
            kind=stored.kind,
            relative_path=stored.relative_path,
            input_sha256=stored.input_sha256,
            content_sha256=stored.content_sha256,
            pipeline_contract_version=PipelineContractVersion(stored.pipeline_contract_version),
            model_config_sha256=stored.model_config_sha256,
            metadata=cast(
                dict[str, Any],
                json.loads(stored.metadata_json),
            ),
        )

    @staticmethod
    def _validate_stage_artifact_provenance(
        job: AnkiCurationJobModel,
        artifact: StageArtifact,
    ) -> None:
        if artifact.pipeline_contract_version.value != job.pipeline_contract_version:
            raise ValueError("stage artifact pipeline contract does not match job")
        if artifact.model_config_sha256 != job.model_config_sha256:
            raise ValueError("stage artifact model configuration does not match job")

    @classmethod
    def _validate_stage_artifact_for_commit(
        cls,
        job: AnkiCurationJobModel,
        job_id: UUID,
        stage: CurationStage,
        artifact: StageArtifact,
    ) -> None:
        cls._validate_stage_artifact_provenance(job, artifact)
        if artifact.artifact_id != f"{stage.value}:{artifact.content_sha256}":
            raise ValueError("stage artifact identity does not match committed stage")
        expected_path = f"{job_id}/{stage.value}/{artifact.content_sha256}.json"
        if artifact.relative_path != expected_path:
            raise ValueError("stage artifact path does not match committed job provenance")

    @staticmethod
    def _resolved_model_config(value: str, provider: str, model: str) -> ResolvedModelConfiguration:
        if value in {"", "{}"}:
            return ResolvedModelConfiguration.legacy(provider, model)
        try:
            raw = cast(dict[str, Any], json.loads(value))
            if not raw:
                raise ValueError

            def stage(name: str) -> ResolvedStageModel:
                item = cast(dict[str, Any], raw[name])
                return ResolvedStageModel(
                    item["provider"],
                    item["model"],
                    item.get("thinking_mode", "default"),
                    item.get("fixture_validation_signature"),
                )

            def classifier_execution() -> ResolvedClassifierExecution | None:
                item = raw.get("classifier_execution")
                if item is None:
                    return None
                if not isinstance(item, dict):
                    raise ValueError
                return ResolvedClassifierExecution.from_document(item)

            resolved = ResolvedModelConfiguration(
                raw["profile"],
                stage("ledger_s2"),
                stage("classify_s4"),
                stage("residual_s6"),
                stage("gap_fill_s7"),
                bool(raw.get("residual_unlocked", False)),
                stage("fast_classify_s4b") if raw.get("fast_classify_s4b") is not None else None,
                classifier_execution(),
                stage("scope_r3") if raw.get("scope_r3") is not None else None,
                stage("cheap_classify_r7") if raw.get("cheap_classify_r7") is not None else None,
                stage("thorough_classify_r7")
                if raw.get("thorough_classify_r7") is not None
                else None,
                stage("generation_r9") if raw.get("generation_r9") is not None else None,
            )
            if _canonical_json(resolved.canonical_document()) != value:
                raise ValueError("stored resolved model configuration is not canonical")
            return resolved
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ValueError("stored resolved model configuration is invalid") from exc

    @staticmethod
    def _gap_card(stored: AnkiGapCardModel) -> GapCard:
        source_refs = cast(
            list[dict[str, Any]],
            json.loads(stored.source_refs_json),
        )
        return GapCard(
            concept_id=stored.concept_id,
            text=stored.text,
            extra=stored.extra,
            revision=stored.revision,
            selected=stored.selected,
            image_state=stored.image_state,
            media_filename=stored.media_filename,
            source_note_id=stored.source_note_id,
            generated_image=cast(
                dict[str, Any],
                json.loads(stored.generated_image_json),
            ),
            validation_state=stored.validation_state,
            source_refs=tuple(
                AnkiCurationRepository._source_reference(value) for value in source_refs
            ),
            evidence_ids=tuple(json.loads(stored.evidence_ids_json)),
            provenance=cast(
                dict[str, Any],
                json.loads(stored.provenance_json),
            ),
            initial_tags=tuple(json.loads(stored.initial_tags_json)),
            content_hash=stored.content_hash,
            card_id=stored.id,
        )

    @staticmethod
    def _envelope(stored: AnkiEnvelopeModel) -> StoredEnvelope:
        receipt = (
            cast(dict[str, Any], json.loads(stored.receipt_summary_json))
            if stored.receipt_summary_json is not None
            else None
        )
        return StoredEnvelope(
            id=UUID(stored.id),
            job_id=UUID(stored.job_id),
            snapshot_id=stored.snapshot_id,
            payload_sha256=stored.payload_sha256,
            state=stored.state,
            receipt_summary=receipt,
        )

    @staticmethod
    def _agent_state(stored: AnkiAgentStateModel) -> AgentState:
        return AgentState(
            agent_id=stored.agent_id,
            heartbeat_at=stored.heartbeat_at,
            versions=cast(dict[str, Any], json.loads(stored.versions_json)),
            active_snapshot_id=stored.active_snapshot_id,
            health=cast(dict[str, Any], json.loads(stored.health_json)),
        )

    @staticmethod
    def _agent_command(stored: AnkiAgentCommandModel) -> StoredAgentCommand:
        return StoredAgentCommand(
            id=UUID(stored.id),
            command_type=AgentCommandType(stored.command_type),
            state=stored.state,
            payload=cast(dict[str, Any], json.loads(stored.payload_json)),
            payload_sha256=stored.payload_sha256,
            owner_agent_id=stored.owner_agent_id,
            created_at=stored.created_at,
        )


def _validate_card_ledger_attempt_for_write(
    attempt: CardCentricLedgerAttempt,
    parameters_json: str,
    *,
    allow_hash_only_validation_failure: bool = False,
) -> None:
    """Reject evidence that current-schema startup would fail closed on."""
    hashes = (attempt.instruction_sha256, attempt.generation_parameters_sha256)
    if (
        attempt.call_index not in {1, 2}
        or (attempt.call_index == 1) != (attempt.kind == "primary")
        or attempt.outcome not in {"accepted", "validation_failed", "transport_failed"}
        or attempt.provider not in set(ProviderName)
        or not isinstance(attempt.model, str)
        or not attempt.model.strip()
        or len(attempt.model) > 200
        or not isinstance(attempt.request_id, str)
        or len(attempt.request_id) > 200
        or any(not re.fullmatch(r"[0-9a-f]{64}", item) for item in hashes)
        or any(
            not isinstance(value, int) or isinstance(value, bool) or value < 0
            for value in (
                attempt.input_tokens,
                attempt.output_tokens,
                attempt.cost_microusd,
            )
        )
        or (
            attempt.diagnostic_source is not None
            and attempt.diagnostic_source not in {source.value for source in DiagnosticSource}
        )
        or (
            attempt.http_status is not None
            and (
                not isinstance(attempt.http_status, int)
                or isinstance(attempt.http_status, bool)
                or not 100 <= attempt.http_status <= 599
            )
        )
    ):
        raise ValueError("card-ledger attempt evidence is invalid")
    try:
        validate_s2_generation_parameters(
            attempt.provider,
            attempt.model,
            attempt.generation_parameters,
        )
    except ValueError as error:
        raise ValueError("card-ledger generation parameters are invalid") from error
    if hashlib.sha256(parameters_json.encode()).hexdigest() != attempt.generation_parameters_sha256:
        raise ValueError("card-ledger generation parameter hash is invalid")
    if attempt.outcome == "accepted":
        valid_payload = (
            attempt.validation_error is None
            and attempt.invalid_response_sha256 is None
            and attempt.invalid_response is None
        )
    elif attempt.outcome == "validation_failed":
        plaintext_payload = (
            isinstance(attempt.validation_error, str)
            and bool(attempt.validation_error.strip())
            and len(attempt.validation_error) <= 2_000
            and isinstance(attempt.invalid_response, str)
            and len(attempt.invalid_response) <= 12_000
            and isinstance(attempt.invalid_response_sha256, str)
            and bool(re.fullmatch(r"[0-9a-f]{64}", attempt.invalid_response_sha256))
            and attempt.invalid_response == _redacted_invalid_response(attempt.invalid_response)
            and hashlib.sha256(attempt.invalid_response.encode()).hexdigest()
            == attempt.invalid_response_sha256
        )
        hash_only_payload = (
            allow_hash_only_validation_failure
            and isinstance(attempt.validation_error, str)
            and bool(attempt.validation_error.strip())
            and len(attempt.validation_error) <= 2_000
            and attempt.invalid_response is None
            and isinstance(attempt.invalid_response_sha256, str)
            and bool(re.fullmatch(r"[0-9a-f]{64}", attempt.invalid_response_sha256))
        )
        valid_payload = plaintext_payload or hash_only_payload
    else:
        valid_payload = (
            isinstance(attempt.validation_error, str)
            and bool(attempt.validation_error.strip())
            and len(attempt.validation_error) <= 2_000
            and attempt.invalid_response_sha256 is None
            and attempt.invalid_response is None
        )
    if attempt.outcome != "transport_failed" and (
        attempt.diagnostic_source is not None or attempt.http_status is not None
    ):
        valid_payload = False
    if not valid_payload:
        raise ValueError("card-ledger attempt outcome payload is invalid")
