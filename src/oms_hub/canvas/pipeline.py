import os
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import select

from oms_hub.canvas.domain import (
    ArtifactRole,
    CanonicalPaths,
    JobState,
    RevisionState,
    SourceKind,
    ValidationState,
)
from oms_hub.canvas.routing import build_paths
from oms_hub.checklist import ChecklistService
from oms_hub.config import Settings
from oms_hub.db import Database
from oms_hub.domain import LectureKey, LectureStepName, StepStatus
from oms_hub.files.atomic import sha256_file, verified_atomic_copy
from oms_hub.files.office import OfficeConverter
from oms_hub.files.pdf import validate_pdf
from oms_hub.models import (
    ArtifactModel,
    CanvasConnectionModel,
    CanvasSourceItemModel,
    LectureModel,
    ProcessingJobModel,
    SourceRevisionModel,
    utc_now,
)
from oms_hub.repositories import CatalogRepository


@dataclass(frozen=True, slots=True)
class PipelineResult:
    revision_id: int
    state: str
    paths: CanonicalPaths


@dataclass(frozen=True, slots=True)
class RecoveryReport:
    requeued: int
    needs_review: int


@dataclass(frozen=True, slots=True)
class PromotionResult:
    revision_id: int
    paths: CanonicalPaths


class CanvasPipeline:
    def __init__(
        self,
        database: Database,
        settings: Settings,
        converter: OfficeConverter,
    ):
        self.database = database
        self.settings = settings
        self.converter = converter
        self.catalog = CatalogRepository(database)

    def _records(
        self, revision_id: int
    ) -> tuple[SourceRevisionModel, CanvasSourceItemModel, LectureModel | None]:
        with self.database.session() as session:
            revision = session.get(SourceRevisionModel, revision_id)
            if revision is None:
                raise KeyError(revision_id)
            source = session.get(CanvasSourceItemModel, revision.source_item_id)
            if source is None:
                raise ValueError("Canvas source record is missing")
            lecture = (
                session.get(LectureModel, source.lecture_id)
                if source.lecture_id is not None
                else None
            )
            if source.lecture_id is not None and lecture is None:
                raise ValueError("matched catalog lecture does not exist")
            if lecture is None and not (
                source.source_kind == SourceKind.PRACTICE_QUESTIONS.value
                and source.subject
                and source.exam_number
            ):
                raise ValueError("Canvas source has no reliable destination")
            return revision, source, lecture

    def _effective_settings(self) -> Settings:
        with self.database.session() as session:
            connection = session.scalar(select(CanvasConnectionModel).limit(1))
            updates: dict[str, object] = {}
            if connection and connection.study_root:
                updates["study_root"] = Path(connection.study_root)
            if connection and connection.icloud_staging_root:
                updates["icloud_staging_root"] = Path(connection.icloud_staging_root)
        return self.settings.model_copy(update=updates)

    def _has_current_lecture(self, lecture_id: int) -> bool:
        with self.database.session() as session:
            artifact = session.scalar(
                select(ArtifactModel)
                .join(SourceRevisionModel, ArtifactModel.revision_id == SourceRevisionModel.id)
                .join(
                    CanvasSourceItemModel,
                    SourceRevisionModel.source_item_id == CanvasSourceItemModel.id,
                )
                .where(
                    CanvasSourceItemModel.lecture_id == lecture_id,
                    ArtifactModel.role == ArtifactRole.LOCAL_PDF.value,
                    ArtifactModel.current.is_(True),
                )
            )
            return artifact is not None

    def _record_artifact(
        self,
        revision_id: int,
        role: ArtifactRole,
        path: Path,
        *,
        current: bool,
    ) -> None:
        digest = sha256_file(path)
        with self.database.session() as session:
            artifact = session.scalar(
                select(ArtifactModel).where(
                    ArtifactModel.revision_id == revision_id,
                    ArtifactModel.role == role.value,
                    ArtifactModel.path == str(path),
                )
            )
            if artifact is None:
                artifact = ArtifactModel(
                    revision_id=revision_id,
                    role=role.value,
                    path=str(path),
                    sha256=digest,
                )
                session.add(artifact)
            artifact.sha256 = digest
            artifact.validation_state = ValidationState.VALID.value
            artifact.current = current
            artifact.promoted_at = utc_now() if current else None

    @staticmethod
    def _promote_group(pairs: list[tuple[Path, Path]], revision_id: int) -> None:
        backups: dict[Path, Path | None] = {}
        try:
            for _, destination in pairs:
                if destination.exists():
                    candidate = destination.with_name(
                        f".{destination.name}.oms-backup-{revision_id}"
                    )
                    verified_atomic_copy(destination, candidate)
                    backups[destination] = candidate
                else:
                    backups[destination] = None
            for source, destination in pairs:
                verified_atomic_copy(source, destination)
        except Exception:
            for destination, backup_path in backups.items():
                if backup_path and backup_path.exists():
                    os.replace(backup_path, destination)
                elif backup_path is None:
                    destination.unlink(missing_ok=True)
            raise
        finally:
            for backup_path in backups.values():
                if backup_path:
                    backup_path.unlink(missing_ok=True)

    def process_revision(self, revision_id: int) -> PipelineResult:
        revision, source, lecture = self._records(revision_id)
        if not revision.stored_path or not revision.sha256:
            raise ValueError("revision has not completed immutable ingestion")
        original = Path(revision.stored_path)
        if sha256_file(original) != revision.sha256:
            raise ValueError("immutable source checksum mismatch")
        kind = SourceKind(source.source_kind)
        lecture_key = (
            LectureKey(
                lecture.subject,
                lecture.exam_number,
                lecture.lecture_number,
                lecture.topic,
            )
            if lecture is not None
            else LectureKey(source.subject or "", source.exam_number or 0, 0, "")
        )
        paths = build_paths(
            self._effective_settings(),
            lecture_key,
            kind,
            revision.original_filename,
            revision.id,
        )
        paths.revision_pdf.parent.mkdir(parents=True, exist_ok=True)
        if original.suffix.casefold() == ".pdf":
            verified_atomic_copy(original, paths.revision_pdf)
        else:
            self.converter.convert(original, paths.revision_pdf)
        validate_pdf(paths.revision_pdf)
        self._record_artifact(
            revision.id,
            ArtifactRole.ORIGINAL,
            original,
            current=False,
        )
        self._record_artifact(
            revision.id,
            ArtifactRole.STAGED_PDF,
            paths.revision_pdf,
            current=False,
        )
        if kind is SourceKind.LECTURE and lecture is not None and self._has_current_lecture(lecture.id):
            self._set_revision_state(revision.id, RevisionState.PROPOSED)
            self._finish_job(revision.id, JobState.NEEDS_REVIEW, "lecture replacement awaits approval")
            return PipelineResult(revision.id, RevisionState.PROPOSED.value, paths)
        pairs = [(paths.revision_pdf, paths.local_pdf), (paths.revision_pdf, paths.icloud_pdf)]
        if kind is SourceKind.LECTURE and paths.local_source is not None:
            pairs.insert(0, (original, paths.local_source))
        self._promote_group(pairs, revision.id)
        if paths.local_source is not None:
            self._record_artifact(
                revision.id,
                ArtifactRole.LOCAL_PPTX,
                paths.local_source,
                current=True,
            )
        self._record_artifact(
            revision.id,
            ArtifactRole.LOCAL_PDF,
            paths.local_pdf,
            current=True,
        )
        self._record_artifact(
            revision.id,
            ArtifactRole.ICLOUD_PDF,
            paths.icloud_pdf,
            current=True,
        )
        self._set_revision_state(revision.id, RevisionState.CURRENT)
        self._finish_job(revision.id, JobState.COMPLETE)
        if kind is SourceKind.LECTURE and lecture is not None:
            self._complete_lecture_steps(lecture.id, paths.icloud_pdf)
        return PipelineResult(revision.id, RevisionState.CURRENT.value, paths)

    def _set_revision_state(self, revision_id: int, state: RevisionState) -> None:
        with self.database.session() as session:
            revision = session.get(SourceRevisionModel, revision_id)
            if revision is None:
                raise KeyError(revision_id)
            revision.state = state.value

    def _finish_job(
        self,
        revision_id: int,
        state: JobState,
        error: str | None = None,
    ) -> None:
        with self.database.session() as session:
            job = session.scalar(
                select(ProcessingJobModel).where(
                    ProcessingJobModel.revision_id == revision_id,
                    ProcessingJobModel.action == "convert",
                )
            )
            if job:
                job.state = state.value
                job.error = error

    def _complete_lecture_steps(self, lecture_id: int, cloud_path: Path) -> None:
        checklist = ChecklistService(self.catalog)
        checklist.transition(
            lecture_id, LectureStepName.CANVAS_PPTX_FOUND, StepStatus.COMPLETE
        )
        checklist.transition(
            lecture_id, LectureStepName.PPTX_DOWNLOADED, StepStatus.COMPLETE
        )
        checklist.transition(lecture_id, LectureStepName.PDF_FILED, StepStatus.COMPLETE)
        checklist.transition(
            lecture_id,
            LectureStepName.GOODNOTES_DELIVERED,
            StepStatus.COMPLETE,
            f"Staged for import: {cloud_path}",
        )

    def run_next(self) -> bool:
        with self.database.session() as session:
            job = session.scalar(
                select(ProcessingJobModel)
                .where(ProcessingJobModel.state == JobState.QUEUED.value)
                .order_by(ProcessingJobModel.created_at, ProcessingJobModel.id)
            )
            if job is None:
                return False
            job.state = JobState.RUNNING.value
            job.attempts += 1
            revision_id = job.revision_id
        try:
            self.process_revision(revision_id)
        except Exception as error:
            self._finish_job(revision_id, JobState.NEEDS_REVIEW, str(error)[:1000])
            self._set_revision_state(revision_id, RevisionState.FAILED)
        return True

    def recover_abandoned_jobs(self) -> RecoveryReport:
        needs_review = 0
        with self.database.session() as session:
            jobs = list(
                session.scalars(
                    select(ProcessingJobModel).where(
                        ProcessingJobModel.state == JobState.RUNNING.value
                    )
                ).all()
            )
            for job in jobs:
                job.state = JobState.NEEDS_REVIEW.value
                job.error = "Hub restarted during processing; inspect staged artifacts before retry"
                needs_review += 1
        return RecoveryReport(requeued=0, needs_review=needs_review)

    def approve_replacement(self, revision_id: int) -> PromotionResult:
        revision, source, lecture = self._records(revision_id)
        if revision.state != RevisionState.PROPOSED.value:
            raise ValueError("only a proposed lecture revision can be approved")
        kind = SourceKind(source.source_kind)
        if kind is not SourceKind.LECTURE:
            raise ValueError("replacement approval applies only to lectures")
        if lecture is None:
            raise ValueError("lecture replacement has no catalog match")
        paths = build_paths(
            self._effective_settings(),
            LectureKey(
                lecture.subject,
                lecture.exam_number,
                lecture.lecture_number,
                lecture.topic,
            ),
            kind,
            revision.original_filename,
            revision.id,
        )
        original = Path(revision.stored_path or "")
        validate_pdf(paths.revision_pdf)
        if not original.is_file() or not revision.sha256 or sha256_file(original) != revision.sha256:
            raise ValueError("proposed immutable source is missing or changed")
        if paths.local_source is None:
            raise ValueError("lecture replacement has no local source destination")
        self._promote_group(
            [
                (original, paths.local_source),
                (paths.revision_pdf, paths.local_pdf),
                (paths.revision_pdf, paths.icloud_pdf),
            ],
            revision.id,
        )
        with self.database.session() as session:
            current_artifacts = list(
                session.scalars(
                    select(ArtifactModel)
                    .join(SourceRevisionModel, ArtifactModel.revision_id == SourceRevisionModel.id)
                    .join(
                        CanvasSourceItemModel,
                        SourceRevisionModel.source_item_id == CanvasSourceItemModel.id,
                    )
                    .where(
                        CanvasSourceItemModel.lecture_id == lecture.id,
                        ArtifactModel.current.is_(True),
                    )
                ).all()
            )
            for artifact in current_artifacts:
                artifact.current = False
            stored_revision = session.get(SourceRevisionModel, revision.id)
            if stored_revision is None:
                raise KeyError(revision.id)
            stored_revision.state = RevisionState.CURRENT.value
        self._record_artifact(
            revision.id, ArtifactRole.LOCAL_PPTX, paths.local_source, current=True
        )
        self._record_artifact(
            revision.id, ArtifactRole.LOCAL_PDF, paths.local_pdf, current=True
        )
        self._record_artifact(
            revision.id, ArtifactRole.ICLOUD_PDF, paths.icloud_pdf, current=True
        )
        self._finish_job(revision.id, JobState.COMPLETE)
        self.catalog.set_step_status(
            lecture.id,
            LectureStepName.GOODNOTES_DELIVERED,
            StepStatus.COMPLETE,
            "Updated PDF staged; Goodnotes re-import may be required",
        )
        return PromotionResult(revision.id, paths)

    def keep_current(self, revision_id: int) -> None:
        revision, _, _ = self._records(revision_id)
        if revision.state != RevisionState.PROPOSED.value:
            raise ValueError("only a proposed revision can be kept without promotion")
        with self.database.session() as session:
            stored = session.get(SourceRevisionModel, revision_id)
            if stored is None:
                raise KeyError(revision_id)
            stored.state = RevisionState.KEPT.value
            source = session.get(CanvasSourceItemModel, stored.source_item_id)
            if source:
                source.review_state = "resolved"
        self._finish_job(revision_id, JobState.COMPLETE, "current lecture retained")

    def remap_source(self, source_item_id: int, lecture_id: int) -> None:
        with self.database.session() as session:
            source = session.get(CanvasSourceItemModel, source_item_id)
            lecture = session.get(LectureModel, lecture_id)
            if source is None or lecture is None:
                raise KeyError((source_item_id, lecture_id))
            source.lecture_id = lecture.id
            source.subject = lecture.subject
            source.exam_number = lecture.exam_number
            source.review_state = "needs_review"
            source.evidence_json = '{"match": "manually remapped in dashboard"}'

    def retry_revision(self, revision_id: int) -> None:
        revision = self._records(revision_id)[0]
        if revision.state not in {
            RevisionState.FAILED.value,
            RevisionState.PROPOSED.value,
        }:
            raise ValueError("revision is not eligible for retry")
        if not revision.stored_path or not revision.sha256:
            raise ValueError("staged source is missing")
        path = Path(revision.stored_path)
        if not path.is_file() or sha256_file(path) != revision.sha256:
            raise ValueError("staged source checksum is invalid")
        with self.database.session() as session:
            job = session.scalar(
                select(ProcessingJobModel).where(
                    ProcessingJobModel.revision_id == revision_id,
                    ProcessingJobModel.action == "convert",
                )
            )
            if job is None:
                job = ProcessingJobModel(revision_id=revision_id, action="convert")
                session.add(job)
            job.state = JobState.QUEUED.value
            job.error = None
            stored = session.get(SourceRevisionModel, revision_id)
            if stored:
                stored.state = RevisionState.DOWNLOADED.value
