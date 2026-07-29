import hashlib
import json
from collections.abc import Sequence
from datetime import datetime
from typing import Any, cast
from uuid import UUID, uuid4

from sqlalchemy import delete, select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.orm import Session

from oms_hub.anki.domain import (
    Candidate,
    CreateCurationJob,
    CurationJob,
    CurationStage,
    CurationState,
    EnvelopeDraft,
    GapCard,
    JobStage,
    ReviewChangeSet,
    SavedReview,
    StageUsage,
    StoredEnvelope,
    StoredEnvelopeOperation,
)
from oms_hub.anki.models import (
    AnkiCandidateModel,
    AnkiCurationJobModel,
    AnkiEnvelopeModel,
    AnkiEnvelopeOperationModel,
    AnkiGapCardModel,
    AnkiJobStageModel,
)
from oms_hub.db import Database
from oms_hub.models import LectureModel, utc_now

ALLOWED_TRANSITIONS: dict[CurationState, set[CurationState]] = {
    CurationState.QUEUED: {CurationState.BUILDING_LCL, CurationState.FAILED},
    CurationState.BUILDING_LCL: {CurationState.RETRIEVING, CurationState.FAILED},
    CurationState.RETRIEVING: {CurationState.JUDGING, CurationState.FAILED},
    CurationState.JUDGING: {CurationState.DEDUPING, CurationState.FAILED},
    CurationState.DEDUPING: {CurationState.PROPOSING_GAPS, CurationState.FAILED},
    CurationState.PROPOSING_GAPS: {
        CurationState.READY_FOR_REVIEW,
        CurationState.FAILED,
    },
    CurationState.READY_FOR_REVIEW: {CurationState.ENVELOPE_PENDING},
    CurationState.ENVELOPE_PENDING: {CurationState.APPLYING, CurationState.FAILED},
    CurationState.APPLYING: {CurationState.COMPLETE, CurationState.FAILED},
}

_INTERRUPTED_PRE_REVIEW_STATES = {
    CurationState.BUILDING_LCL,
    CurationState.RETRIEVING,
    CurationState.JUDGING,
    CurationState.DEDUPING,
    CurationState.PROPOSING_GAPS,
}


class InvalidCurationTransition(ValueError):
    """A curation job did not match the required state transition."""


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


class AnkiCurationRepository:
    def __init__(self, database: Database) -> None:
        self.database = database

    def create_job(self, request: CreateCurationJob) -> CurationJob:
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
                amboss_input=request.amboss_input,
                amboss_sha256=_sha256_text(request.amboss_input),
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

    def claim_next_job(self, now: datetime) -> CurationJob | None:
        with self.database.session() as session:
            stored = session.scalar(
                select(AnkiCurationJobModel)
                .where(AnkiCurationJobModel.state == CurationState.QUEUED.value)
                .order_by(AnkiCurationJobModel.created_at, AnkiCurationJobModel.id)
                .limit(1)
            )
            if stored is None:
                return None
            claimed = session.execute(
                update(AnkiCurationJobModel)
                .where(
                    AnkiCurationJobModel.id == stored.id,
                    AnkiCurationJobModel.state == CurationState.QUEUED.value,
                )
                .values(
                    state=CurationState.BUILDING_LCL.value,
                    attempts=AnkiCurationJobModel.attempts + 1,
                    started_at=now.isoformat(),
                    error=None,
                )
            )
            if cast(CursorResult[Any], claimed).rowcount != 1:
                return None
            session.flush()
            session.refresh(stored)
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
                raise InvalidCurationTransition(
                    f"job {job_id} is not in {expected_state.value}"
                )
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
                job.state = CurationState.QUEUED.value
                job.error = "requeued after an interrupted Hub process"
                job.started_at = None
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

    def replace_candidates(
        self,
        job_id: UUID,
        candidates: Sequence[Candidate],
    ) -> None:
        with self.database.session() as session:
            self._require_job_model(session, job_id)
            session.execute(
                delete(AnkiCandidateModel).where(
                    AnkiCandidateModel.job_id == str(job_id)
                )
            )
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
                        mnemonic_classification=candidate.mnemonic_classification,
                        dedupe_disposition=candidate.dedupe_disposition,
                        selected=candidate.selected,
                    )
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
            session.execute(
                delete(AnkiGapCardModel).where(
                    AnkiGapCardModel.job_id == str(job_id)
                )
            )
            for card in cards:
                session.add(
                    AnkiGapCardModel(
                        id=str(uuid4()),
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
                    )
                )

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
                gap = session.scalar(
                    select(AnkiGapCardModel).where(
                        AnkiGapCardModel.job_id == str(job_id),
                        AnkiGapCardModel.concept_id == edit.concept_id,
                    )
                )
                if gap is None:
                    raise KeyError(edit.concept_id)
                gap.text = edit.text
                gap.extra = edit.extra
                gap.selected = edit.selected
                gap.revision += 1
            job.review_revision += 1
            session.flush()
            return SavedReview(job_id=job_id, revision=job.review_revision)

    def create_envelope(
        self,
        job_id: UUID,
        envelope: EnvelopeDraft,
    ) -> StoredEnvelope:
        with self.database.session() as session:
            self._require_job_model(session, job_id)
            existing = session.scalar(
                select(AnkiEnvelopeModel).where(
                    AnkiEnvelopeModel.job_id == str(job_id)
                )
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
            if receipt.get("sync_status") == "retryable":
                stored.state = "retryable"
            elif (
                receipt.get("sync_status") == "complete"
                and receipt.get("verified") is True
            ):
                stored.state = "complete"
            else:
                stored.state = "failed"
            session.flush()
            return self._envelope(stored)

    def start_envelope_operation(
        self,
        envelope_id: UUID,
        operation_id: UUID | str,
    ) -> StoredEnvelopeOperation:
        with self.database.session() as session:
            stored = self._require_envelope_operation(
                session,
                envelope_id,
                operation_id,
            )
            if stored.state == "complete":
                return self._envelope_operation(stored)
            if stored.state == "failed":
                raise ValueError("failed envelope operation cannot be retried")
            if stored.state not in {"pending", "retryable", "applying"}:
                raise ValueError("envelope operation has an invalid state")
            stored.state = "applying"
            stored.attempts += 1
            stored.error = None
            session.flush()
            return self._envelope_operation(stored)

    def complete_envelope_operation(
        self,
        envelope_id: UUID,
        operation_id: UUID | str,
        result: dict[str, Any],
    ) -> StoredEnvelopeOperation:
        with self.database.session() as session:
            stored = self._require_envelope_operation(
                session,
                envelope_id,
                operation_id,
            )
            if stored.state == "complete":
                return self._envelope_operation(stored)
            if stored.state != "applying":
                raise ValueError("envelope operation is not being applied")
            stored.result_json = _canonical_json(result)
            stored.state = "complete"
            stored.error = None
            session.flush()
            return self._envelope_operation(stored)

    def fail_envelope_operation(
        self,
        envelope_id: UUID,
        operation_id: UUID | str,
        safe_error: str,
        *,
        retryable: bool,
    ) -> StoredEnvelopeOperation:
        normalized_error = safe_error.strip()[:1_000]
        if not normalized_error:
            raise ValueError("safe_error cannot be empty")
        with self.database.session() as session:
            stored = self._require_envelope_operation(
                session,
                envelope_id,
                operation_id,
            )
            if stored.state != "applying":
                raise ValueError("envelope operation is not being applied")
            stored.state = "retryable" if retryable else "failed"
            stored.error = normalized_error
            session.flush()
            return self._envelope_operation(stored)

    def operation_results(
        self,
        envelope_id: UUID,
    ) -> dict[str, dict[str, Any]]:
        with self.database.session() as session:
            stored = session.scalars(
                select(AnkiEnvelopeOperationModel)
                .where(
                    AnkiEnvelopeOperationModel.envelope_id
                    == str(envelope_id),
                    AnkiEnvelopeOperationModel.state == "complete",
                )
                .order_by(AnkiEnvelopeOperationModel.position)
            ).all()
            return {
                item.id: cast(
                    dict[str, Any],
                    json.loads(item.result_json or "{}"),
                )
                for item in stored
            }

    @staticmethod
    def _require_job_model(session: Session, job_id: UUID) -> AnkiCurationJobModel:
        stored = session.get(AnkiCurationJobModel, str(job_id))
        if stored is None:
            raise KeyError(str(job_id))
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
    def _require_envelope_operation(
        session: Session,
        envelope_id: UUID,
        operation_id: UUID | str,
    ) -> AnkiEnvelopeOperationModel:
        stored = session.scalar(
            select(AnkiEnvelopeOperationModel).where(
                AnkiEnvelopeOperationModel.envelope_id == str(envelope_id),
                AnkiEnvelopeOperationModel.id == str(operation_id),
            )
        )
        if stored is None:
            raise KeyError(f"{envelope_id}:{operation_id}")
        return stored

    @staticmethod
    def _job(stored: AnkiCurationJobModel) -> CurationJob:
        return CurationJob(
            id=UUID(stored.id),
            lecture_id=stored.lecture_id,
            state=CurationState(stored.state),
            attempts=stored.attempts,
            amboss_input=stored.amboss_input,
            amboss_sha256=stored.amboss_sha256,
            instruction_text=stored.instruction_text,
            instruction_sha256=stored.instruction_sha256,
            target_deck=stored.target_deck,
            target_tag=stored.target_tag,
            index_snapshot_id=stored.index_snapshot_id,
            lcl_prompt_version=stored.lcl_prompt_version,
            judgment_rubric_version=stored.judgment_rubric_version,
            gap_prompt_version=stored.gap_prompt_version,
            review_revision=stored.review_revision,
            error=stored.error,
            created_at=stored.created_at,
            updated_at=stored.updated_at,
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
        )

    @staticmethod
    def _gap_card(stored: AnkiGapCardModel) -> GapCard:
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
    def _envelope_operation(
        stored: AnkiEnvelopeOperationModel,
    ) -> StoredEnvelopeOperation:
        return StoredEnvelopeOperation(
            id=UUID(stored.id),
            envelope_id=UUID(stored.envelope_id),
            operation_type=stored.operation_type,
            content_hash=stored.content_hash,
            payload=cast(dict[str, Any], json.loads(stored.payload_json)),
            state=stored.state,
            attempts=stored.attempts,
            result=(
                cast(dict[str, Any], json.loads(stored.result_json))
                if stored.result_json is not None
                else None
            ),
            error=stored.error,
        )
