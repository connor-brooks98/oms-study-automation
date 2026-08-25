import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, cast
from uuid import UUID, uuid4

from sqlalchemy import func, or_, select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.orm import Session

from oms_hub.db import Database
from oms_hub.files.atomic import sha256_file
from oms_hub.files.trusted_paths import is_indirection, trusted_managed_path
from oms_hub.ingestion.domain import (
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
    ExistingArtifactImportModel,
    GenerationJobModel,
    IngestionJobModel,
    OutlineOutputModel,
    OutlineReplacementReviewModel,
    StudyRevisionModel,
    StudyUsageModel,
    UploadBatchModel,
    UploadItemModel,
    utc_now,
)


def _filed_artifact_matches(revision: StudyRevisionModel) -> bool:
    if not revision.canonical_derived_path or not revision.derived_sha256:
        return False
    try:
        derived = Path(revision.canonical_derived_path)
        if not derived.is_file() or sha256_file(derived) != revision.derived_sha256:
            return False
        if revision.kind != UploadKind.SLIDES.value:
            return True
        if not revision.canonical_source_path or not revision.icloud_path:
            return False
        source = Path(revision.canonical_source_path)
        icloud = Path(revision.icloud_path)
        return (
            source.is_file()
            and sha256_file(source) == revision.source_sha256
            and icloud.is_file()
            and sha256_file(icloud) == revision.derived_sha256
        )
    except OSError:
        return False


def _immutable_derived_matches(revision: StudyRevisionModel) -> bool:
    if not revision.immutable_derived_path or not revision.derived_sha256:
        return False
    path = Path(revision.immutable_derived_path)
    try:
        return path.is_file() and sha256_file(path) == revision.derived_sha256
    except OSError:
        return False


class IngestionRepository:
    def __init__(
        self,
        database: Database,
        *,
        artifact_v2_root: Path | None = None,
        study_root: Path | None = None,
        icloud_root: Path | None = None,
    ):
        self.database = database
        self.artifact_v2_root = artifact_v2_root
        self.study_root = study_root
        self.icloud_root = icloud_root

    def imported_derived_audit_matches(
        self,
        revision: StudyRevision,
        *,
        allow_repair_destinations: bool = False,
    ) -> bool:
        """Return whether a slide replacement still has its complete audit graph.

        Slide repair alone may tolerate a damaged mutable PDF destination; it is
        the operation that restores those exact bytes.  All immutable evidence
        and all persisted graph identities remain mandatory.
        """
        if revision.provenance_kind != "imported_derived" or revision.import_id is None:
            return False
        if (
            revision.kind is not UploadKind.SLIDES
            or revision.derived_sha256 is None
            or revision.immutable_derived_path is None
            or revision.canonical_source_path is None
            or revision.canonical_derived_path is None
            or revision.icloud_path is None
        ):
            return False
        with self.database.session() as session:
            audit = session.get(ExistingArtifactImportModel, revision.import_id)
            model = session.get(StudyRevisionModel, revision.id)
            transcript = (
                session.get(StudyRevisionModel, audit.transcript_revision_id)
                if audit is not None and audit.transcript_revision_id is not None
                else None
            )
            outline = (
                session.get(OutlineOutputModel, audit.outline_id)
                if audit is not None and audit.outline_id is not None
                else None
            )
            current_outline = session.scalar(
                select(OutlineOutputModel).where(
                    OutlineOutputModel.lecture_id == revision.lecture_id,
                    OutlineOutputModel.current.is_(True),
                )
            )
            replacement_job = (
                session.get(GenerationJobModel, current_outline.job_id)
                if current_outline is not None and current_outline.job_id is not None
                else None
            )
            replacement_review = (
                session.get(OutlineReplacementReviewModel, current_outline.job_id)
                if current_outline is not None and current_outline.job_id is not None
                else None
            )
            approved_replacement = (
                audit is not None
                and outline is not None
                and current_outline is not None
                and current_outline.id != outline.id
                and current_outline.provenance_kind == "notebooklm_generated"
                and current_outline.path == audit.canonical_outline_path
                and replacement_job is not None
                and replacement_job.lecture_id == audit.lecture_id
                and replacement_job.kind == "outline"
                and replacement_job.state == "complete"
                and replacement_job.stage == "complete"
                and replacement_job.pdf_revision_id == revision.id
                and replacement_job.transcript_revision_id == audit.transcript_revision_id
                and replacement_review is not None
                and replacement_review.lecture_id == audit.lecture_id
                and replacement_review.import_id == audit.id
                and bool(replacement_review.operator.strip())
                and bool(replacement_review.reason.strip())
            )
            valid = (
                audit is not None
                and model is not None
                and audit.status == "complete"
                and audit.recovery_phase == "committed"
                and audit.slide_revision_id == revision.id
                and audit.slide_source_sha256 == revision.source_sha256
                and audit.slide_pdf_sha256 == revision.derived_sha256
                and audit.expected_current_pdf_sha256 is not None
                and audit.previous_pdf_sha256 == audit.expected_current_pdf_sha256
                and audit.previous_immutable_pdf_path is not None
                and audit.imported_pdf_sha256 == revision.derived_sha256
                and audit.imported_immutable_pdf_path == str(revision.immutable_derived_path)
                and audit.derived_provenance == "imported_derived"
                and model.current
                and model.kind == UploadKind.SLIDES.value
                and model.source_sha256 == audit.slide_source_sha256
                and model.derived_sha256 == audit.imported_pdf_sha256
                and model.immutable_derived_path == audit.imported_immutable_pdf_path
                and model.canonical_derived_path == str(revision.canonical_derived_path)
                and model.icloud_path == str(revision.icloud_path)
                and model.provenance_kind == "imported_derived"
                and model.import_id == audit.id
                and transcript is not None
                and transcript.current
                and transcript.kind == UploadKind.TRANSCRIPTS.value
                and transcript.provenance_kind == "imported_cleaned"
                and transcript.import_id == audit.id
                and transcript.source_sha256 == audit.transcript_sha256
                and transcript.derived_sha256 == audit.transcript_sha256
                and transcript.immutable_source_path == audit.immutable_transcript_path
                and transcript.immutable_derived_path == audit.immutable_transcript_path
                and transcript.canonical_source_path == audit.canonical_transcript_path
                and transcript.canonical_derived_path == audit.canonical_transcript_path
                and outline is not None
                and (outline.current or approved_replacement)
                and outline.provenance_kind == "imported_notebooklm"
                and outline.import_id == audit.id
                and outline.lecture_id == audit.lecture_id
                and outline.job_id is None
                and outline.path == audit.canonical_outline_path
                and outline.sha256 == audit.outline_sha256
                and outline.immutable_path == audit.immutable_outline_path
                and outline.slide_revision_id == revision.id
                and outline.slide_source_sha256 == revision.source_sha256
                and outline.slide_sha256 == revision.derived_sha256
                and outline.transcript_revision_id == transcript.id
                and outline.transcript_sha256 == transcript.derived_sha256
            )
        if not valid:
            return False
        assert audit is not None
        assert transcript is not None
        assert outline is not None
        try:
            canonical_id = str(UUID(audit.id))
            immutable_root = self.artifact_v2_root
            study_root = self.study_root
            icloud_root = self.icloud_root
            if (
                canonical_id != audit.id
                or immutable_root is None
                or study_root is None
                or icloud_root is None
            ):
                return False
            import_root = immutable_root.parent / "existing-imports"
            audit_root = Path(audit.immutable_transcript_path or "").parent
            imported_archive = Path(audit.imported_immutable_pdf_path or "")
            previous_archive = Path(audit.previous_immutable_pdf_path or "")
            transcript_archive = Path(audit.immutable_transcript_path or "")
            outline_archive = Path(audit.immutable_outline_path or "")
            slide_immutable_source = revision.immutable_source_path
            slide_canonical_source = revision.canonical_source_path
            slide_immutable_pdf = revision.immutable_derived_path
            slide_canonical_pdf = revision.canonical_derived_path
            slide_icloud_pdf = revision.icloud_path
            transcript_canonical = Path(audit.canonical_transcript_path or "")
            outline_canonical = Path(audit.canonical_outline_path or "")
            generated_outline = (
                immutable_root.parent
                / "generation"
                / current_outline.job_id
                / "outline.pdf"
                if approved_replacement
                and current_outline is not None
                and current_outline.job_id is not None
                else None
            )
            if (
                not audit.adoption_operator
                or not audit.adoption_operator.strip()
                or not audit.adoption_reason
                or not audit.adoption_reason.strip()
                or not audit.adoption_confirmed_at
                or not audit.adoption_confirmed_at.strip()
                or any(
                    is_indirection(path) or not path.is_dir()
                    for path in (immutable_root, import_root, audit_root, study_root, icloud_root)
                )
                or audit_root != import_root / canonical_id
                or imported_archive != audit_root / "derived-slide.pdf"
                or transcript_archive != audit_root / "cleaned.txt"
                or outline_archive != audit_root / "outline.pdf"
                or imported_archive == previous_archive
                or not all(
                    trusted_managed_path(path, immutable_root, require_regular_file=True)
                    for path in (slide_immutable_source, previous_archive)
                )
                or not all(
                    trusted_managed_path(path, import_root, require_regular_file=True)
                    for path in (
                        slide_immutable_pdf,
                        imported_archive,
                        transcript_archive,
                        outline_archive,
                    )
                )
                or not all(
                    trusted_managed_path(path, study_root, require_regular_file=True)
                    for path in (
                        slide_canonical_source,
                        transcript_canonical,
                        outline_canonical,
                    )
                )
                or (
                    generated_outline is not None
                    and not trusted_managed_path(
                        generated_outline,
                        immutable_root.parent,
                        require_regular_file=True,
                    )
                )
                or not trusted_managed_path(
                    slide_canonical_pdf,
                    study_root,
                    require_regular_file=not allow_repair_destinations,
                )
                or not trusted_managed_path(
                    slide_icloud_pdf,
                    icloud_root,
                    require_regular_file=not allow_repair_destinations,
                )
            ):
                return False
            immutable_graph_matches = (
                sha256_file(revision.immutable_source_path) == revision.source_sha256
                and sha256_file(revision.canonical_source_path) == revision.source_sha256
                and sha256_file(revision.immutable_derived_path) == revision.derived_sha256
                and sha256_file(previous_archive) == audit.previous_pdf_sha256
                and sha256_file(imported_archive) == audit.imported_pdf_sha256
                and sha256_file(Path(audit.immutable_transcript_path or ""))
                == audit.transcript_sha256
                and sha256_file(Path(audit.canonical_transcript_path or ""))
                == audit.transcript_sha256
                and sha256_file(Path(audit.immutable_outline_path or "")) == audit.outline_sha256
                and sha256_file(Path(audit.canonical_outline_path or ""))
                == (
                    current_outline.sha256
                    if approved_replacement and current_outline is not None
                    else audit.outline_sha256
                )
                and (
                    generated_outline is None
                    or (
                        current_outline is not None
                        and sha256_file(generated_outline) == current_outline.sha256
                    )
                )
            )
            if not immutable_graph_matches:
                return False
            if allow_repair_destinations:
                return True
            return (
                sha256_file(revision.canonical_derived_path) == revision.derived_sha256
                and sha256_file(revision.icloud_path) == revision.derived_sha256
            )
        except OSError:
            return False

    def has_imported_derived_audit(self, revision_id: int) -> bool:
        with self.database.session() as session:
            return (
                session.scalar(
                    select(ExistingArtifactImportModel.id).where(
                        ExistingArtifactImportModel.slide_revision_id == revision_id,
                        ExistingArtifactImportModel.derived_provenance == "imported_derived",
                    )
                )
                is not None
            )

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
            if revision is None:
                revision = session.scalar(
                    select(StudyRevisionModel).where(
                        StudyRevisionModel.lecture_id == item.lecture_id,
                        StudyRevisionModel.kind == item.kind,
                        StudyRevisionModel.source_sha256 == item.sha256,
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
            elif revision.state == "failed" and not revision.current:
                revision.state = "proposed"
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

    def fail_incomplete_study_revision(self, item_id: str) -> None:
        """Retire an unusable revision after its processing retries are exhausted."""
        with self.database.session() as session:
            item = session.get(UploadItemModel, item_id)
            if item is None:
                raise KeyError(item_id)
            revision = session.scalar(
                select(StudyRevisionModel).where(
                    StudyRevisionModel.upload_item_id == item_id
                )
            )
            if revision is None:
                revision = session.scalar(
                    select(StudyRevisionModel).where(
                        StudyRevisionModel.lecture_id == item.lecture_id,
                        StudyRevisionModel.kind == item.kind,
                        StudyRevisionModel.source_sha256 == item.sha256,
                    )
                )
            if (
                revision is not None
                and not revision.current
                and revision.state in {"proposed", "promoting"}
                and (
                    not _immutable_derived_matches(revision)
                    or revision.canonical_derived_path is None
                )
            ):
                revision.state = "failed"

    def promote_study_revision(
        self,
        revision_id: int,
        *,
        complete_item: bool = True,
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
            if complete_item:
                self._complete_revision_item(session, revision)
            session.flush()
            return self._study_revision(revision)

    def complete_promoted_revision(
        self,
        revision_id: int,
        item_id: str,
    ) -> StudyRevision:
        with self.database.session() as session:
            revision = session.get(StudyRevisionModel, revision_id)
            if revision is None:
                raise KeyError(revision_id)
            if not revision.current or revision.state != "current":
                raise ValueError("revision promotion is not committed")
            self._complete_revision_item(session, revision)
            if item_id != revision.upload_item_id:
                item = session.get(UploadItemModel, item_id)
                if item is None:
                    raise KeyError(item_id)
                item.state = UploadState.COMPLETE.value
                item.error = None
                job = session.scalar(
                    select(IngestionJobModel).where(
                        IngestionJobModel.upload_item_id == item_id,
                        IngestionJobModel.action == "process",
                    )
                )
                if job is not None:
                    job.state = UploadState.COMPLETE.value
                    job.error = None
                self._sync_batch_state(session, item.batch_id)
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
            provenance_kind=revision.provenance_kind,
            import_id=revision.import_id,
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
            stored.error = None

    def _enqueue_unless_current_duplicate(
        self,
        session: Session,
        item: UploadItemModel,
    ) -> None:
        exact = session.scalar(
            select(StudyRevisionModel).where(
                StudyRevisionModel.lecture_id == item.lecture_id,
                StudyRevisionModel.kind == item.kind,
                StudyRevisionModel.source_sha256 == item.sha256,
                StudyRevisionModel.current.is_(True),
            )
        )
        if exact is not None and _filed_artifact_matches(exact):
            item.state = UploadState.COMPLETE.value
            item.error = None
            evidence = list(json.loads(item.evidence_json))
            label = (
                "transcript"
                if item.kind == UploadKind.TRANSCRIPTS.value
                else "slide"
            )
            evidence.append(f"Exact {label} already processed")
            item.evidence_json = json.dumps(evidence)
            return
        if exact is not None:
            evidence = list(json.loads(item.evidence_json))
            evidence.append("Exact source queued to repair filed artifact")
            item.evidence_json = json.dumps(evidence)
            self._enqueue(session, item.id, "process")
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
