import hashlib
import json
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Any, cast
from uuid import UUID, uuid4

from sqlalchemy import delete, or_, select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.orm import Session

from oms_hub.anki.apply import ApplyOperationRecord
from oms_hub.anki.audit import AuditCacheRecord
from oms_hub.anki.contracts import (
    ActionEnvelopeDocument,
    canonical_payload_sha256,
    parse_action_envelope,
)
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
    AnkiCoverageJudgmentCacheModel,
    AnkiCurationJobModel,
    AnkiEnvelopeModel,
    AnkiEnvelopeOperationModel,
    AnkiGapCardModel,
    AnkiJobStageModel,
    AnkiReviewChangeSetModel,
    AnkiSourceEvidenceModel,
    AnkiStageArtifactModel,
    AnkiTagPatchModel,
)
from oms_hub.db import Database
from oms_hub.llm.domain import ProviderName
from oms_hub.models import LectureModel, utc_now

ALLOWED_TRANSITIONS: dict[CurationState, set[CurationState]] = {
    CurationState.QUEUED: {CurationState.PREFLIGHT, CurationState.FAILED},
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
        CurationState.FAILED,
    },
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
    CurationState.READY_FOR_REVIEW: {CurationState.ENVELOPE_PENDING},
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
        CurationState.REMOVED,
    },
}

_INTERRUPTED_PRE_REVIEW_STATES = {
    CurationState.PREFLIGHT,
    CurationState.SNAPSHOTTING_EMBEDDINGS,
    CurationState.BUILDING_COMPANION_INDEX,
    CurationState.BUILDING_SOURCE_INDEX,
    CurationState.BUILDING_LCL,
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
}

_CLAIMABLE_STATES = {
    CurationState.QUEUED,
    CurationState.PREFLIGHT,
    CurationState.BUILDING_SOURCE_INDEX,
    CurationState.BUILDING_LCL,
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
}

_RETRY_STATE_BY_STAGE = {
    CurationStage.PREFLIGHT: CurationState.PREFLIGHT,
    CurationStage.SOURCE_INDEX: CurationState.BUILDING_SOURCE_INDEX,
    CurationStage.LCL: CurationState.BUILDING_LCL,
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
}

class InvalidCurationTransition(ValueError):
    """A curation job did not match the required state transition."""


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("job timestamps must be timezone-aware")
    return value.astimezone(UTC)


class AnkiCurationRepository:
    def __init__(self, database: Database) -> None:
        self.database = database

    def create_job(self, request: CreateCurationJob) -> CurationJob:
        model_config = request.resolved_model_config or ResolvedModelConfiguration.legacy(
            request.provider, request.model
        )
        model_config_json = _canonical_json(model_config.canonical_document())
        model_config_sha256 = _sha256_text(model_config_json)
        configuration = {
            "block_id": request.block_id,
            "source_revision_ids": request.source_revision_ids,
            "source_revision_hashes": request.source_revision_hashes,
            "summary_outline_id": request.summary_outline_id,
            "summary_outline_sha256": request.summary_outline_sha256,
            "deck_allowlist": request.deck_allowlist,
            "tag_allowlist": request.tag_allowlist,
            "target_deck": request.target_deck,
            "target_tag": request.target_tag,
            "index_snapshot_id": request.index_snapshot_id,
            "lcl_prompt_version": request.lcl_prompt_version,
            "judgment_rubric_version": request.judgment_rubric_version,
            "gap_prompt_version": request.gap_prompt_version,
            "provider": request.provider,
            "model": request.model,
            "pipeline_contract_version": request.pipeline_contract_version.value,
            "model_config_sha256": model_config_sha256,
            "semantic_generation": request.semantic_generation,
            "companion_generation": request.companion_generation,
        }
        with self.database.session() as session:
            if session.get(LectureModel, request.lecture_id) is None:
                raise KeyError(request.lecture_id)
            stored = AnkiCurationJobModel(
                id=str(uuid4()),
                lecture_id=request.lecture_id,
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
                configuration_sha256=_sha256_text(_canonical_json(configuration)),
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
                    AnkiCurationJobModel.pipeline_contract_version
                    == PipelineContractVersion.RETRIEVAL_V4.value,
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
                    state=(CurationState.PREFLIGHT.value if queued else stored.state),
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
        with self.database.session() as session:
            changed = session.execute(
                update(AnkiCurationJobModel)
                .where(
                    AnkiCurationJobModel.id == str(job_id),
                    AnkiCurationJobModel.lease_owner == worker_id,
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
        available_at: datetime,
    ) -> CurationJob:
        with self.database.session() as session:
            stored = self._require_owned_job(
                session,
                job_id,
                worker_id,
            )
            stored.error = safe_error[:1_000]
            stored.available_at = _aware_utc(available_at).isoformat()
            stored.lease_owner = None
            stored.lease_expires_at = None
            session.flush()
            return self._job(stored)

    def fail_job(
        self,
        job_id: UUID,
        worker_id: str,
        safe_error: str,
    ) -> CurationJob:
        with self.database.session() as session:
            stored = self._require_owned_job(
                session,
                job_id,
                worker_id,
            )
            stored.state = CurationState.FAILED.value
            stored.error = safe_error[:1_000]
            stored.available_at = None
            stored.lease_owner = None
            stored.lease_expires_at = None
            session.flush()
            return self._job(stored)

    def retry_job(self, job_id: UUID) -> CurationJob:
        with self.database.session() as session:
            stored = self._require_job_model(session, job_id)
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
            target_state = _RETRY_STATE_BY_STAGE.get(CurationStage(failed_stage.stage))
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
    ) -> JobStage:
        with self.database.session() as session:
            self._require_job_model(session, job_id)
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
    ) -> JobStage:
        with self.database.session() as session:
            stored = self._require_stage(session, job_id, stage)
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
            if lease_owner is not None and job.lease_owner != lease_owner:
                raise InvalidCurationTransition(f"worker no longer owns job {job_id}")
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
                or existing.pipeline_contract_version
                != artifact.pipeline_contract_version.value
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
    ) -> SavedReview:
        with self.database.session() as session:
            job = self._require_job_model(session, job_id)
            if job.review_revision != change_set.expected_revision:
                raise ValueError("review revision is stale")
            reviewer = change_set.reviewer.strip()
            if not reviewer or len(reviewer) > 200:
                raise ValueError("reviewer is invalid")
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
            session.flush()
            return SavedReview(job_id=job_id, revision=job.review_revision)

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
            if envelope.contract_version == 2:
                agent = session.get(AnkiAgentStateModel, 1)
                versions = (
                    cast(dict[str, Any], json.loads(agent.versions_json))
                    if agent is not None
                    else {}
                )
                supported = versions.get("supported_envelope_contract_versions", [1])
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
    def _require_owned_job(
        session: Session,
        job_id: UUID,
        worker_id: str,
    ) -> AnkiCurationJobModel:
        stored = AnkiCurationRepository._require_job_model(
            session,
            job_id,
        )
        if stored.lease_owner != worker_id:
            raise InvalidCurationTransition(f"worker no longer owns job {job_id}")
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
    def _job(stored: AnkiCurationJobModel) -> CurationJob:
        config = AnkiCurationRepository._resolved_model_config(
            stored.resolved_model_config_json, stored.provider, stored.model
        )
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
            pipeline_contract_version=PipelineContractVersion(stored.pipeline_contract_version),
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
        )

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

            resolved = ResolvedModelConfiguration(
                raw["profile"],
                stage("ledger_s2"),
                stage("classify_s4"),
                stage("residual_s6"),
                stage("gap_fill_s7"),
                bool(raw.get("residual_unlocked", False)),
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
