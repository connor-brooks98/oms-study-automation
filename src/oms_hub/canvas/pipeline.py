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
    ) -> tuple[SourceRevisionModel, CanvasSourceItemModel, LectureModel]:
        with self.database.session() as session:
            revision = session.get(SourceRevisionModel, revision_id)
            if revision is None:
                raise KeyError(revision_id)
            source = session.get(CanvasSourceItemModel, revision.source_item_id)
            if source is None or source.lecture_id is None:
                raise ValueError("Canvas source is not matched to a lecture")
            lecture = session.get(LectureModel, source.lecture_id)
            if lecture is None:
                raise ValueError("matched catalog lecture does not exist")
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
        if kind is SourceKind.LECTURE and self._has_current_lecture(lecture.id):
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
        if kind is SourceKind.LECTURE:
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
