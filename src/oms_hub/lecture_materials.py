"""Detach lecture materials without deleting their files or study history."""

import json

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from oms_hub.artifact_writes import ArtifactWriteCoordinator
from oms_hub.artifacts import ArtifactError, ArtifactService
from oms_hub.config import Settings
from oms_hub.db import Database
from oms_hub.domain import StepStatus, V2StepName
from oms_hub.files.atomic import sha256_file
from oms_hub.ingestion.domain import StudyRevision
from oms_hub.ingestion.repository import IngestionRepository
from oms_hub.models import (
    GenerationJobModel,
    IngestionJobModel,
    LectureStepModel,
    ProcessControlModel,
    StudioRunArtifactModel,
    StudioRunModel,
    StudyRevisionModel,
    UploadItemModel,
)


class MaterialConflict(ValueError):
    """The material cannot safely change until the stated prerequisite is resolved."""


class LectureMaterials:
    def __init__(self, database: Database, settings: Settings):
        self.database, self.settings = database, settings
        self.repository = IngestionRepository(database)

    def list_removed(self, lecture_id: int) -> list[StudyRevision]:
        with self.database.session() as session:
            rows = session.scalars(
                select(StudyRevisionModel)
                .where(
                    StudyRevisionModel.lecture_id == lecture_id,
                    StudyRevisionModel.state == "removed",
                )
                .order_by(StudyRevisionModel.id.desc())
            ).all()
            return [self.repository._study_revision(row) for row in rows]

    def remove(self, lecture_id: int, revision_id: int) -> StudyRevision:
        return self._change(lecture_id, revision_id, restore=False)

    def restore(self, lecture_id: int, revision_id: int) -> StudyRevision:
        return self._change(lecture_id, revision_id, restore=True)

    def _change(self, lecture_id: int, revision_id: int, *, restore: bool) -> StudyRevision:
        if self.repository.get_study_revision(revision_id).lecture_id != lecture_id:
            raise KeyError(revision_id)
        with ArtifactWriteCoordinator(self.database, self.settings).claim(
            lecture_id, "material-restore" if restore else "material-remove"
        ) as claim:
            claim.assert_owned()
            with self.database.session() as session:
                # Serialize admission with new uploads and worker claims, not just file writes.
                session.execute(text("BEGIN IMMEDIATE"))
                row = session.get(StudyRevisionModel, revision_id)
                if row is None or row.lecture_id != lecture_id:
                    raise KeyError(revision_id)
                self._require_idle(session, lecture_id)
                if restore:
                    if row.state != "removed" or row.current:
                        raise MaterialConflict("This material is not removed.")
                    current = session.scalar(
                        select(StudyRevisionModel.id).where(
                            StudyRevisionModel.lecture_id == lecture_id,
                            StudyRevisionModel.kind == row.kind,
                            StudyRevisionModel.current.is_(True),
                        )
                    )
                    if current is not None:
                        raise MaterialConflict(
                            "Remove the current material before restoring this one."
                        )
                    self._check_restore(self.repository._study_revision(row))
                elif not row.current or row.state != "current":
                    raise MaterialConflict(
                        "Only the current, fully processed material can be removed."
                    )
                row.current = restore
                row.state = "current" if restore else "removed"
                prefix = "slides_" if row.kind == "slides" else "transcript_"
                names = [step.value for step in V2StepName if step.value.startswith(prefix)]
                if row.kind == "slides":
                    names.extend(
                        (V2StepName.PDF_CONVERTED.value, V2StepName.ICLOUD_PDF_STAGED.value)
                    )
                for step in session.scalars(
                    select(LectureStepModel).where(
                        LectureStepModel.lecture_id == lecture_id,
                        LectureStepModel.name.in_(names),
                    )
                ):
                    step.status = (StepStatus.COMPLETE if restore else StepStatus.WAITING).value
                    step.detail = (
                        "Retained material restored"
                        if restore
                        else "Material removed from lecture; files retained"
                    )
                claim.assert_owned()
            return self.repository.get_study_revision(revision_id)

    def _check_restore(self, revision: StudyRevision) -> None:
        service = ArtifactService(self.database, self.settings)
        try:
            service._validate_file(
                revision.immutable_source_path, revision.source_sha256, service._immutable_root()
            )
            for source, destination in service._approval_pairs(revision):
                # Approval pairs already validate immutable files and destination roots.
                if not destination.is_file() or sha256_file(destination) != sha256_file(source):
                    raise MaterialConflict(
                        "The retained filed copy changed or is missing. Download remains "
                        "available; restore requires recovery of the original filed copy."
                    )
        except (ArtifactError, OSError, ValueError) as error:
            raise MaterialConflict(
                "The retained files cannot be verified. Download available history; "
                "restore requires recovery of the original filed copy."
            ) from error

    @staticmethod
    def _stopped(session: Session, family: str, job_id: int | str) -> bool:
        control = session.get(ProcessControlModel, (family, str(job_id)))
        return bool(
            control and control.requested_action in {"pause", "remove"} and control.acknowledged_at
        )

    @classmethod
    def _require_idle(cls, session: Session, lecture_id: int) -> None:
        jobs = session.scalars(
            select(IngestionJobModel.id)
            .join(
                UploadItemModel,
                UploadItemModel.id == IngestionJobModel.upload_item_id,
            )
            .where(
                UploadItemModel.lecture_id == lecture_id,
                IngestionJobModel.state.in_(("queued", "processing")),
            )
        )
        if any(not cls._stopped(session, "ingestion", job) for job in jobs):
            raise MaterialConflict("Pause the active upload process and wait for it to stop first.")
        jobs = session.scalars(
            select(GenerationJobModel.id).where(
                GenerationJobModel.lecture_id == lecture_id,
                GenerationJobModel.state.in_(("queued", "running", "retrying")),
            )
        )
        if any(not cls._stopped(session, "generation", job) for job in jobs):
            raise MaterialConflict("Pause or finish lecture generation before changing materials.")
        runs = session.execute(
            select(
                StudioRunModel.id,
                StudioRunModel.state,
                StudioRunArtifactModel.payload_json,
            )
            .join(StudioRunArtifactModel, StudioRunModel.id == StudioRunArtifactModel.run_id)
            .where(
                StudioRunArtifactModel.artifact_key == "gpt:manifest",
                StudioRunModel.state.in_(
                    ("queued", "running", "retrying", "interrupted", "paused")
                ),
            )
        )
        for run_id, state, payload in runs:
            try:
                bound_lecture = json.loads(payload)["lecture_id"]
                control = session.scalar(
                    select(StudioRunArtifactModel.payload_json).where(
                        StudioRunArtifactModel.run_id == run_id,
                        StudioRunArtifactModel.artifact_key == "gpt:control",
                    )
                )
                stopping = bool(control and json.loads(control).get("worker_stopping"))
            except (ValueError, KeyError, TypeError) as error:
                raise MaterialConflict(
                    "Finish the quiz with an unreadable source record first."
                ) from error
            if bound_lecture == lecture_id and (
                stopping
                or (
                    state in {"queued", "running", "retrying"}
                    and not cls._stopped(session, "studio", run_id)
                )
            ):
                raise MaterialConflict(
                    "Pause or finish lecture quiz generation before changing materials."
                )
