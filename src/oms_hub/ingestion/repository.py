import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

from sqlalchemy import func, or_, select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.orm import Session

from oms_hub.db import Database
from oms_hub.ingestion.domain import (
    FailedRevision,
    IngestionJob,
    MatchDecision,
    StagedUpload,
    StoredUploadItem,
    StudyRevision,
    UploadBatch,
    UploadItem,
    UploadKind,
    UploadState,
)
from oms_hub.models import (
    IngestionJobModel,
    LectureModel,
    StudyRevisionModel,
    StudyUsageModel,
    UploadBatchModel,
    UploadItemModel,
    utc_now,
)


class IngestionRepository:
    def __init__(self, database: Database):
        self.database = database

    def create_batch(
        self,
        kind: UploadKind,
        batch_id: str | None = None,
    ) -> str:
        resolved_id = batch_id or str(uuid4())
        with self.database.session() as session:
            session.add(
                UploadBatchModel(
                    id=resolved_id,
                    kind=kind.value,
                    state=UploadState.UPLOADING.value,
                )
            )
        return resolved_id

    def add_item(
        self,
        kind: UploadKind,
        staged: StagedUpload,
    ) -> None:
        with self.database.session() as session:
            batch = session.get(UploadBatchModel, staged.batch_id)
            if batch is None:
                raise KeyError(staged.batch_id)
            session.add(
                UploadItemModel(
                    id=staged.item_id,
                    batch_id=staged.batch_id,
                    kind=kind.value,
                    original_filename=staged.original_filename,
                    staged_path=str(staged.path),
                    sha256=staged.sha256,
                    size_bytes=staged.size_bytes,
                    state=UploadState.MATCHING.value,
                )
            )
            batch.state = UploadState.MATCHING.value

    def set_batch_state(
        self,
        batch_id: str,
        state: UploadState,
    ) -> None:
        with self.database.session() as session:
            batch = session.get(UploadBatchModel, batch_id)
            if batch is None:
                raise KeyError(batch_id)
            batch.state = state.value

    def require_item(self, item_id: str) -> StoredUploadItem:
        with self.database.session() as session:
            item = session.get(UploadItemModel, item_id)
            if item is None:
                raise KeyError(item_id)
            return self._stored_item(item)

    def list_quarantined(self) -> list[StoredUploadItem]:
        with self.database.session() as session:
            models = session.scalars(
                select(UploadItemModel)
                .where(
                    UploadItemModel.state
                    == UploadState.QUARANTINED.value
                )
                .order_by(UploadItemModel.created_at, UploadItemModel.id)
            ).all()
            return [self._stored_item(item) for item in models]

    def apply_match(
        self,
        item_id: str,
        decision: MatchDecision,
    ) -> None:
        with self.database.session() as session:
            item = session.get(UploadItemModel, item_id)
            if item is None:
                raise KeyError(item_id)
            if item.manual_assignment:
                return
            item.lecture_id = decision.lecture_id
            item.confidence = decision.confidence
            item.evidence_json = json.dumps(list(decision.evidence))
            item.state = (
                UploadState.QUEUED.value
                if decision.state == "matched"
                else UploadState.QUARANTINED.value
            )
            if decision.state == "matched":
                self._enqueue_unless_current_duplicate(session, item)
            self._sync_batch_state(session, item.batch_id)

    def set_manual_assignment(
        self,
        item_id: str,
        lecture_id: int,
    ) -> None:
        with self.database.session() as session:
            item = session.get(UploadItemModel, item_id)
            if item is None:
                raise KeyError(item_id)
            item.lecture_id = lecture_id
            item.confidence = 1.0
            item.evidence_json = json.dumps(
                ["Assigned manually in Quarantine"]
            )
            item.manual_assignment = True
            item.state = UploadState.QUEUED.value
            item.error = None
            self._enqueue_unless_current_duplicate(session, item)
            self._sync_batch_state(session, item.batch_id)

    def assign_quarantined_items(
        self,
        item_ids: list[str],
        lecture_id: int,
    ) -> list[StoredUploadItem]:
        unique_ids = list(dict.fromkeys(item_ids))
        if not unique_ids:
            raise ValueError("select at least one quarantined file")
        with self.database.session() as session:
            if session.get(LectureModel, lecture_id) is None:
                raise KeyError(lecture_id)
            items = list(
                session.scalars(
                    select(UploadItemModel).where(
                        UploadItemModel.id.in_(unique_ids)
                    )
                ).all()
            )
            by_id = {item.id: item for item in items}
            if set(by_id) != set(unique_ids):
                raise KeyError("one or more upload items were not found")
            if any(
                item.state != UploadState.QUARANTINED.value
                for item in items
            ):
                raise ValueError("all selected files must still be quarantined")
            for item_id in unique_ids:
                item = by_id[item_id]
                item.lecture_id = lecture_id
                item.confidence = 1.0
                item.evidence_json = json.dumps(
                    [
                        *json.loads(item.evidence_json),
                        "Assigned manually in Quarantine",
                    ]
                )
                item.manual_assignment = True
                item.state = UploadState.QUEUED.value
                item.error = None
                self._enqueue_unless_current_duplicate(session, item)
                self._sync_batch_state(session, item.batch_id)
            session.flush()
            return [self._stored_item(by_id[item_id]) for item_id in unique_ids]

    def count_jobs(self, item_id: str, action: str) -> int:
        with self.database.session() as session:
            return int(
                session.scalar(
                    select(func.count())
                    .select_from(IngestionJobModel)
                    .where(
                        IngestionJobModel.upload_item_id == item_id,
                        IngestionJobModel.action == action,
                    )
                )
                or 0
            )

    def confirm_processing(self, item_id: str) -> StoredUploadItem:
        with self.database.session() as session:
            item = session.get(UploadItemModel, item_id)
            if item is None:
                raise KeyError(item_id)
            if item.state == UploadState.AWAITING_CONFIRMATION.value:
                item.state = UploadState.QUEUED.value
                item.error = None
                self._enqueue(session, item.id, "process")
                self._sync_batch_state(session, item.batch_id)
            elif item.state not in {
                UploadState.QUEUED.value,
                UploadState.PROCESSING.value,
                UploadState.NEEDS_REVIEW.value,
                UploadState.COMPLETE.value,
                UploadState.FAILED.value,
            }:
                raise ValueError("upload is not awaiting confirmation")
            session.flush()
            return self._stored_item(item)

    def mark_discarded(self, item_id: str) -> StoredUploadItem:
        with self.database.session() as session:
            item = session.get(UploadItemModel, item_id)
            if item is None:
                raise KeyError(item_id)
            if item.state == UploadState.AWAITING_CONFIRMATION.value:
                item.state = UploadState.DISCARDED.value
                item.error = None
                self._sync_batch_state(session, item.batch_id)
            elif item.state != UploadState.DISCARDED.value:
                raise ValueError("upload is not awaiting confirmation")
            session.flush()
            return self._stored_item(item)

    def claim_next_job(self, now: datetime) -> IngestionJob | None:
        now_value = now.isoformat()
        with self.database.session() as session:
            job = session.scalar(
                select(IngestionJobModel)
                .where(
                    IngestionJobModel.state == UploadState.QUEUED.value,
                    or_(
                        IngestionJobModel.next_attempt_at.is_(None),
                        IngestionJobModel.next_attempt_at <= now_value,
                    ),
                )
                .order_by(IngestionJobModel.created_at, IngestionJobModel.id)
                .limit(1)
            )
            if job is None:
                return None
            claimed = session.execute(
                update(IngestionJobModel)
                .where(
                    IngestionJobModel.id == job.id,
                    IngestionJobModel.state == UploadState.QUEUED.value,
                )
                .values(
                    state=UploadState.PROCESSING.value,
                    attempts=IngestionJobModel.attempts + 1,
                    next_attempt_at=None,
                    error=None,
                )
            )
            if cast(CursorResult[Any], claimed).rowcount != 1:
                return None
            item = session.get(UploadItemModel, job.upload_item_id)
            if item is None:
                raise ValueError("ingestion job upload item is missing")
            item.state = UploadState.PROCESSING.value
            item.error = None
            self._sync_batch_state(session, item.batch_id)
            session.flush()
            session.refresh(job)
            return IngestionJob(
                id=job.id,
                upload_item_id=job.upload_item_id,
                kind=UploadKind(item.kind),
                action=job.action,
                attempts=job.attempts,
                claimed_at=now,
            )

    def recover_interrupted_jobs(self) -> int:
        with self.database.session() as session:
            jobs = session.scalars(
                select(IngestionJobModel).where(
                    IngestionJobModel.state == UploadState.PROCESSING.value
                )
            ).all()
            for job in jobs:
                job.state = UploadState.QUEUED.value
                job.next_attempt_at = None
                job.error = "requeued after an interrupted Hub process"
                item = session.get(UploadItemModel, job.upload_item_id)
                if item is not None and item.state == UploadState.PROCESSING.value:
                    item.state = UploadState.QUEUED.value
                    item.error = job.error
                    self._sync_batch_state(session, item.batch_id)
            return len(jobs)

    def retry_job(
        self,
        job: IngestionJob,
        error: str,
        *,
        delay: timedelta,
    ) -> None:
        with self.database.session() as session:
            stored = session.get(IngestionJobModel, job.id)
            item = session.get(UploadItemModel, job.upload_item_id)
            if stored is None or item is None:
                raise KeyError(job.id)
            stored.state = UploadState.QUEUED.value
            stored.next_attempt_at = (job.claimed_at + delay).isoformat()
            stored.error = error
            item.state = UploadState.QUEUED.value
            item.error = error
            self._sync_batch_state(session, item.batch_id)

    def fail_job(
        self,
        job: IngestionJob,
        error: str,
        *,
        state: UploadState,
    ) -> None:
        if state not in {
            UploadState.QUARANTINED,
            UploadState.NEEDS_REVIEW,
            UploadState.FAILED,
        }:
            raise ValueError("job failure state is invalid")
        with self.database.session() as session:
            stored = session.get(IngestionJobModel, job.id)
            item = session.get(UploadItemModel, job.upload_item_id)
            if stored is None or item is None:
                raise KeyError(job.id)
            stored.state = state.value
            stored.next_attempt_at = None
            stored.error = error
            item.state = state.value
            item.error = error
            self._sync_batch_state(session, item.batch_id)

    def begin_revision(
        self,
        item_id: str,
        immutable_root: Path,
    ) -> StudyRevision:
        with self.database.session() as session:
            item = session.get(UploadItemModel, item_id)
            if item is None:
                raise KeyError(item_id)
            if item.lecture_id is None:
                raise ValueError("upload item has not been matched to a lecture")
            revision = session.scalar(
                select(StudyRevisionModel).where(
                    StudyRevisionModel.upload_item_id == item_id
                )
            )
            if revision is not None and revision.state in {"failed", "retrying"}:
                revision.state = "proposed"
            if revision is None:
                revision = session.scalar(
                    select(StudyRevisionModel).where(
                        StudyRevisionModel.lecture_id == item.lecture_id,
                        StudyRevisionModel.kind == item.kind,
                        StudyRevisionModel.source_sha256 == item.sha256,
                        StudyRevisionModel.state.in_(
                            {"current", "proposed", "promoting"}
                        ),
                    )
                )
            if revision is None:
                revision = StudyRevisionModel(
                    upload_item_id=item.id,
                    lecture_id=item.lecture_id,
                    kind=item.kind,
                    source_sha256=item.sha256,
                    immutable_source_path="",
                )
                session.add(revision)
                session.flush()
                revision_dir = immutable_root / str(revision.id)
                extension = Path(item.original_filename).suffix.casefold()
                revision.immutable_source_path = str(
                    revision_dir
                    / (
                        f"original{extension}"
                        if item.kind == UploadKind.SLIDES.value
                        else "raw.txt"
                    )
                )
                if item.kind == UploadKind.SLIDES.value:
                    revision.immutable_derived_path = str(
                        revision_dir / "converted.pdf"
                    )
                else:
                    revision.immutable_derived_path = str(
                        revision_dir / "cleaned.txt"
                    )
            item.state = UploadState.PROCESSING.value
            item.error = None
            job = session.scalar(
                select(IngestionJobModel).where(
                    IngestionJobModel.upload_item_id == item_id,
                    IngestionJobModel.action == "process",
                )
            )
            if job is not None:
                if job.state != UploadState.PROCESSING.value:
                    job.attempts += 1
                job.state = UploadState.PROCESSING.value
                job.error = None
            self._sync_batch_state(session, item.batch_id)
            session.flush()
            return self._study_revision(revision)

    def has_other_current_revision(
        self,
        lecture_id: int,
        kind: UploadKind,
        revision_id: int,
    ) -> bool:
        with self.database.session() as session:
            return (
                session.scalar(
                    select(StudyRevisionModel.id).where(
                        StudyRevisionModel.lecture_id == lecture_id,
                        StudyRevisionModel.kind == kind.value,
                        StudyRevisionModel.current.is_(True),
                        StudyRevisionModel.id != revision_id,
                    )
                )
                is not None
            )

    def get_study_revision(
        self,
        revision_id: int,
    ) -> StudyRevision:
        with self.database.session() as session:
            revision = session.get(StudyRevisionModel, revision_id)
            if revision is None:
                raise KeyError(revision_id)
            return self._study_revision(revision)

    def list_proposed_revisions(self) -> list[StudyRevision]:
        with self.database.session() as session:
            revisions = session.scalars(
                select(StudyRevisionModel)
                .where(StudyRevisionModel.state == "proposed")
                .order_by(StudyRevisionModel.created_at, StudyRevisionModel.id)
            ).all()
            return [self._study_revision(item) for item in revisions]

    def list_failed_revisions(self) -> list[FailedRevision]:
        with self.database.session() as session:
            rows = session.execute(
                select(StudyRevisionModel, UploadItemModel.error)
                .join(
                    UploadItemModel,
                    UploadItemModel.id == StudyRevisionModel.upload_item_id,
                )
                .where(
                    StudyRevisionModel.state == "failed",
                    StudyRevisionModel.kind == UploadKind.TRANSCRIPTS.value,
                    UploadItemModel.state.in_(
                        {
                            UploadState.NEEDS_REVIEW.value,
                            UploadState.QUARANTINED.value,
                            UploadState.FAILED.value,
                        }
                    ),
                )
                .order_by(StudyRevisionModel.created_at, StudyRevisionModel.id)
            ).all()
            return [
                FailedRevision(self._study_revision(revision), error or "Unknown failure")
                for revision, error in rows
            ]

    def retry_failed_revision(self, revision_id: int) -> StudyRevision:
        with self.database.session() as session:
            revision = session.get(StudyRevisionModel, revision_id)
            if revision is None:
                raise KeyError(revision_id)
            if revision.state != "failed":
                raise ValueError("revision is not failed")
            item = session.get(UploadItemModel, revision.upload_item_id)
            if item is None:
                raise ValueError("revision upload item is missing")
            if item.state in {
                UploadState.QUEUED.value,
                UploadState.PROCESSING.value,
            }:
                raise ValueError("revision retry is already in progress")
            revision.state = "retrying"
            revision.current = False
            item.state = UploadState.QUEUED.value
            item.error = None
            self._enqueue(session, item.id, "process")
            self._sync_batch_state(session, item.batch_id)
            session.flush()
            return self._study_revision(revision)

    def list_current_revisions(
        self,
        lecture_id: int,
    ) -> list[StudyRevision]:
        with self.database.session() as session:
            revisions = session.scalars(
                select(StudyRevisionModel)
                .where(
                    StudyRevisionModel.lecture_id == lecture_id,
                    StudyRevisionModel.current.is_(True),
                )
                .order_by(StudyRevisionModel.kind)
            ).all()
            return [self._study_revision(item) for item in revisions]

    def update_revision_paths(
        self,
        revision_id: int,
        *,
        derived_sha256: str,
        canonical_source_path: Path,
        canonical_derived_path: Path,
        icloud_path: Path,
    ) -> StudyRevision:
        with self.database.session() as session:
            revision = session.get(StudyRevisionModel, revision_id)
            if revision is None:
                raise KeyError(revision_id)
            revision.derived_sha256 = derived_sha256
            revision.canonical_source_path = str(canonical_source_path)
            revision.canonical_derived_path = str(canonical_derived_path)
            revision.icloud_path = str(icloud_path)
            session.flush()
            return self._study_revision(revision)

    def update_transcript_revision(
        self,
        revision_id: int,
        *,
        derived_sha256: str,
        prompt_sha256: str,
        canonical_derived_path: Path,
    ) -> StudyRevision:
        with self.database.session() as session:
            revision = session.get(StudyRevisionModel, revision_id)
            if revision is None:
                raise KeyError(revision_id)
            revision.derived_sha256 = derived_sha256
            revision.prompt_sha256 = prompt_sha256
            revision.canonical_source_path = None
            revision.canonical_derived_path = str(canonical_derived_path)
            revision.icloud_path = None
            session.flush()
            return self._study_revision(revision)

    def record_study_usage(
        self,
        revision_id: int,
        *,
        model: str,
        request_id: str,
        input_tokens: int,
        output_tokens: int,
        cost_microusd: int,
        provider: str = "openai",
    ) -> None:
        with self.database.session() as session:
            usage = session.scalar(
                select(StudyUsageModel).where(
                    StudyUsageModel.revision_id == revision_id
                )
            )
            if usage is None:
                session.add(
                    StudyUsageModel(
                        revision_id=revision_id,
                        provider=provider,
                        model=model,
                        request_id=request_id,
                        input_tokens=input_tokens,
                        output_tokens=output_tokens,
                        cost_microusd=cost_microusd,
                    )
                )

    def finish_revision(
        self,
        item_id: str,
        revision_id: int,
        state: UploadState,
        *,
        current: bool,
        error: str | None = None,
        revision_state: str | None = None,
    ) -> StudyRevision:
        with self.database.session() as session:
            item = session.get(UploadItemModel, item_id)
            revision = session.get(StudyRevisionModel, revision_id)
            if item is None:
                raise KeyError(item_id)
            if revision is None:
                raise KeyError(revision_id)
            item.state = state.value
            item.error = error
            if not revision.current or current:
                revision.state = revision_state or (
                    "current"
                    if current
                    else "proposed"
                    if state is UploadState.NEEDS_REVIEW
                    else "failed"
                )
                revision.current = current
                revision.promoted_at = utc_now() if current else None
            job = session.scalar(
                select(IngestionJobModel).where(
                    IngestionJobModel.upload_item_id == item_id,
                    IngestionJobModel.action == "process",
                )
            )
            if job is not None:
                job.state = state.value
                job.error = error
            self._sync_batch_state(session, item.batch_id)
            session.flush()
            return self._study_revision(revision)

    def promote_study_revision(
        self,
        revision_id: int,
    ) -> StudyRevision:
        with self.database.session() as session:
            revision = session.get(StudyRevisionModel, revision_id)
            if revision is None:
                raise KeyError(revision_id)
            if revision.state not in {"proposed", "promoting"}:
                raise ValueError("revision is not awaiting approval")
            current = session.scalars(
                select(StudyRevisionModel).where(
                    StudyRevisionModel.lecture_id == revision.lecture_id,
                    StudyRevisionModel.kind == revision.kind,
                    StudyRevisionModel.current.is_(True),
                    StudyRevisionModel.id != revision.id,
                )
            ).all()
            for previous in current:
                previous.current = False
                previous.state = "superseded"
            revision.current = True
            revision.state = "current"
            revision.promoted_at = utc_now()
            self._complete_revision_item(session, revision)
            session.flush()
            return self._study_revision(revision)

    def begin_study_promotion(
        self,
        revision_id: int,
    ) -> StudyRevision:
        with self.database.session() as session:
            revision = session.get(StudyRevisionModel, revision_id)
            if revision is None:
                raise KeyError(revision_id)
            if revision.state != "proposed":
                raise ValueError("revision is not awaiting approval")
            revision.state = "promoting"
            session.flush()
            return self._study_revision(revision)

    def reset_study_promotion(self, revision_id: int) -> None:
        with self.database.session() as session:
            revision = session.get(StudyRevisionModel, revision_id)
            if revision is None:
                raise KeyError(revision_id)
            if revision.state == "promoting":
                revision.state = "proposed"

    def keep_study_revision(
        self,
        revision_id: int,
    ) -> StudyRevision:
        with self.database.session() as session:
            revision = session.get(StudyRevisionModel, revision_id)
            if revision is None:
                raise KeyError(revision_id)
            if revision.state != "proposed":
                raise ValueError("revision is not awaiting approval")
            revision.current = False
            revision.state = "kept"
            self._complete_revision_item(session, revision)
            session.flush()
            return self._study_revision(revision)

    def get_batch(self, batch_id: str) -> UploadBatch | None:
        with self.database.session() as session:
            batch = session.get(UploadBatchModel, batch_id)
            if batch is None:
                return None
            models = session.scalars(
                select(UploadItemModel)
                .where(UploadItemModel.batch_id == batch_id)
                .order_by(UploadItemModel.created_at, UploadItemModel.id)
            ).all()
            items = tuple(
                UploadItem(
                    id=item.id,
                    kind=UploadKind(item.kind),
                    original_filename=item.original_filename,
                    sha256=item.sha256,
                    size_bytes=item.size_bytes,
                    state=UploadState(item.state),
                    lecture_id=item.lecture_id,
                    confidence=item.confidence,
                    evidence=tuple(json.loads(item.evidence_json)),
                    manual_assignment=item.manual_assignment,
                    error=item.error,
                )
                for item in models
            )
            return UploadBatch(
                id=batch.id,
                kind=UploadKind(batch.kind),
                state=UploadState(batch.state),
                created_at=batch.created_at,
                updated_at=batch.updated_at,
                items=items,
            )

    def _stored_item(self, item: UploadItemModel) -> StoredUploadItem:
        return StoredUploadItem(
            id=item.id,
            batch_id=item.batch_id,
            kind=UploadKind(item.kind),
            original_filename=item.original_filename,
            staged_path=Path(item.staged_path),
            sha256=item.sha256,
            size_bytes=item.size_bytes,
            state=UploadState(item.state),
            lecture_id=item.lecture_id,
            confidence=item.confidence,
            evidence=tuple(json.loads(item.evidence_json)),
            manual_assignment=item.manual_assignment,
            error=item.error,
        )

    def _study_revision(
        self,
        revision: StudyRevisionModel,
    ) -> StudyRevision:
        return StudyRevision(
            id=revision.id,
            upload_item_id=revision.upload_item_id,
            lecture_id=revision.lecture_id,
            kind=UploadKind(revision.kind),
            source_sha256=revision.source_sha256,
            immutable_source_path=Path(revision.immutable_source_path),
            derived_sha256=revision.derived_sha256,
            immutable_derived_path=(
                Path(revision.immutable_derived_path)
                if revision.immutable_derived_path
                else None
            ),
            canonical_source_path=(
                Path(revision.canonical_source_path)
                if revision.canonical_source_path
                else None
            ),
            canonical_derived_path=(
                Path(revision.canonical_derived_path)
                if revision.canonical_derived_path
                else None
            ),
            icloud_path=(
                Path(revision.icloud_path)
                if revision.icloud_path
                else None
            ),
            prompt_sha256=revision.prompt_sha256,
            state=revision.state,
            current=revision.current,
        )

    def _complete_revision_item(
        self,
        session: Session,
        revision: StudyRevisionModel,
    ) -> None:
        item = session.get(
            UploadItemModel,
            revision.upload_item_id,
        )
        if item is None:
            raise ValueError("revision upload item is missing")
        item.state = UploadState.COMPLETE.value
        item.error = None
        job = session.scalar(
            select(IngestionJobModel).where(
                IngestionJobModel.upload_item_id == item.id,
                IngestionJobModel.action == "process",
            )
        )
        if job is not None:
            job.state = UploadState.COMPLETE.value
            job.error = None
        self._sync_batch_state(session, item.batch_id)

    def _enqueue(
        self,
        session: Session,
        item_id: str,
        action: str,
    ) -> None:
        stored = session.scalar(
            select(IngestionJobModel).where(
                IngestionJobModel.upload_item_id == item_id,
                IngestionJobModel.action == action,
            )
        )
        if stored is None:
            session.add(
                IngestionJobModel(
                    upload_item_id=item_id,
                    action=action,
                    state=UploadState.QUEUED.value,
                )
            )
        else:
            stored.state = UploadState.QUEUED.value
            stored.next_attempt_at = None
            stored.error = None

    def _enqueue_unless_current_duplicate(
        self,
        session: Session,
        item: UploadItemModel,
    ) -> None:
        exact = session.scalar(
            select(StudyRevisionModel.id).where(
                StudyRevisionModel.lecture_id == item.lecture_id,
                StudyRevisionModel.kind == item.kind,
                StudyRevisionModel.source_sha256 == item.sha256,
                StudyRevisionModel.current.is_(True),
            )
        )
        if exact is not None:
            item.state = UploadState.COMPLETE.value
            item.error = None
            evidence = list(json.loads(item.evidence_json))
            evidence.append("Exact transcript already processed")
            item.evidence_json = json.dumps(evidence)
            return
        current = session.scalar(
            select(StudyRevisionModel.id).where(
                StudyRevisionModel.lecture_id == item.lecture_id,
                StudyRevisionModel.kind == item.kind,
                StudyRevisionModel.current.is_(True),
            )
        )
        if (
            current is not None
            and item.kind == UploadKind.TRANSCRIPTS.value
        ):
            item.state = UploadState.AWAITING_CONFIRMATION.value
            item.error = None
            return
        self._enqueue(session, item.id, "process")

    def _sync_batch_state(
        self,
        session: Session,
        batch_id: str,
    ) -> None:
        batch = session.get(UploadBatchModel, batch_id)
        states = set(
            session.scalars(
                select(UploadItemModel.state).where(
                    UploadItemModel.batch_id == batch_id
                )
            ).all()
        )
        priorities = (
            UploadState.FAILED,
            UploadState.QUARANTINED,
            UploadState.NEEDS_REVIEW,
            UploadState.AWAITING_CONFIRMATION,
            UploadState.PROCESSING,
            UploadState.QUEUED,
            UploadState.MATCHING,
            UploadState.COMPLETE,
            UploadState.DISCARDED,
        )
        if batch is not None:
            batch.state = next(
                (
                    state.value
                    for state in priorities
                    if state.value in states
                ),
                UploadState.UPLOADING.value,
            )
