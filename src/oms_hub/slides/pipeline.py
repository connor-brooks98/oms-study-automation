from pathlib import Path

from oms_hub.config import Settings
from oms_hub.db import Database
from oms_hub.domain import LectureKey, StepStatus, V2StepName
from oms_hub.files.atomic import sha256_file, verified_atomic_copy
from oms_hub.files.office import OfficeConverter
from oms_hub.files.pdf import validate_pdf
from oms_hub.files.promotion import PromotionCoordinator
from oms_hub.ingestion.domain import (
    StudyRevision,
    UploadKind,
    UploadState,
)
from oms_hub.ingestion.repository import IngestionRepository
from oms_hub.progress import SLIDE_PIPELINE_STEPS
from oms_hub.repositories import CatalogRepository
from oms_hub.routing import build_slide_destinations, expanded_path


class SlidePipeline:
    def __init__(
        self,
        database: Database,
        settings: Settings,
        converter: OfficeConverter,
    ):
        self.settings = settings
        self.converter = converter
        self.promotion = PromotionCoordinator()
        self.repository = IngestionRepository(database)
        self.catalog = CatalogRepository(database)

    def process(self, item_id: str) -> StudyRevision:
        item = self.repository.require_item(item_id)
        if item.kind is not UploadKind.SLIDES:
            raise ValueError("upload item is not a PowerPoint")
        if item.lecture_id is None:
            raise ValueError("upload item has not been matched to a lecture")
        lecture = self.catalog.get_lecture(item.lecture_id)
        if lecture is None:
            raise ValueError("matched catalog lecture does not exist")

        revision = self.repository.begin_revision(
            item_id,
            expanded_path(self.settings.data_dir)
            / "artifacts"
            / "v2"
            / "slides",
        )
        derived = revision.immutable_derived_path
        if derived is None:
            raise ValueError("slide revision has no PDF destination")
        try:
            self._set_slide_steps(
                revision.lecture_id,
                StepStatus.RUNNING,
                "Validating and converting the uploaded PowerPoint",
            )
            self._preserve_source(
                item.staged_path,
                revision.immutable_source_path,
                revision.source_sha256,
            )
            self.catalog.set_step_status(
                revision.lecture_id,
                V2StepName.SLIDES_VALIDATED,
                StepStatus.COMPLETE,
                "Original PowerPoint preserved and checksum verified",
            )
            derived_sha256 = self._ensure_pdf(
                revision.immutable_source_path,
                derived,
                revision.derived_sha256,
            )
            self.catalog.set_step_status(
                revision.lecture_id,
                V2StepName.PDF_CONVERTED,
                StepStatus.COMPLETE,
                "Converted PDF validated",
            )
            destinations = build_slide_destinations(
                self.settings,
                LectureKey(
                    lecture.subject,
                    lecture.exam_number,
                    lecture.lecture_number,
                    lecture.topic,
                ),
            )
            revision = self.repository.update_revision_paths(
                revision.id,
                derived_sha256=derived_sha256,
                canonical_source_path=destinations.source,
                canonical_derived_path=destinations.pdf,
                icloud_path=destinations.icloud_pdf,
            )
            if self.repository.has_other_current_revision(
                revision.lecture_id,
                UploadKind.SLIDES,
                revision.id,
            ):
                self.catalog.set_step_status(
                    revision.lecture_id,
                    V2StepName.SLIDES_FILED,
                    StepStatus.NEEDS_REVIEW,
                    "A replacement is ready for approval",
                )
                return self.repository.finish_revision(
                    item_id,
                    revision.id,
                    UploadState.NEEDS_REVIEW,
                    current=False,
                    error="lecture replacement awaits approval",
                )

            revision = self._promote_revision(
                revision,
                [
                    (revision.immutable_source_path, destinations.source),
                    (derived, destinations.pdf),
                    (derived, destinations.icloud_pdf),
                ],
            )
            self.catalog.set_step_status(
                revision.lecture_id,
                V2StepName.SLIDES_FILED,
                StepStatus.COMPLETE,
                "PowerPoint and PDF filed on the NUC",
            )
            self.catalog.set_step_status(
                revision.lecture_id,
                V2StepName.ICLOUD_PDF_STAGED,
                StepStatus.COMPLETE,
                "PDF staged in iCloud",
            )
            return revision
        except Exception as error:
            self._mark_failed(revision.lecture_id, str(error))
            self.repository.finish_revision(
                item_id,
                revision.id,
                UploadState.FAILED,
                current=False,
                error=str(error),
            )
            raise

    def _preserve_source(
        self,
        staged: Path,
        immutable: Path,
        expected_sha256: str,
    ) -> None:
        if sha256_file(staged) != expected_sha256:
            raise ValueError("staged PowerPoint checksum mismatch")
        if immutable.is_file():
            if sha256_file(immutable) != expected_sha256:
                raise ValueError("immutable PowerPoint checksum mismatch")
            return
        copied_sha256 = verified_atomic_copy(staged, immutable)
        if copied_sha256 != expected_sha256:
            raise ValueError("preserved PowerPoint checksum mismatch")

    def _ensure_pdf(
        self,
        source: Path,
        destination: Path,
        expected_sha256: str | None,
    ) -> str:
        if (
            destination.is_file()
            and expected_sha256
            and sha256_file(destination) == expected_sha256
        ):
            validate_pdf(destination)
            return expected_sha256
        destination.unlink(missing_ok=True)
        self.converter.convert(source, destination)
        validate_pdf(destination)
        return sha256_file(destination)

    def _promote_revision(
        self,
        revision: StudyRevision,
        pairs: list[tuple[Path, Path]],
    ) -> StudyRevision:
        if revision.state == "promoting":
            recovered = self.promotion.recover(
                pairs,
                revision.id,
                lambda: self.repository.promote_study_revision(revision.id),
                lambda: self.repository.reset_study_promotion(revision.id),
            )
            if recovered is not None:
                return recovered
            revision = self.repository.get_study_revision(revision.id)
        self.repository.begin_study_promotion(revision.id)
        try:
            return self.promotion.promote(
                pairs,
                revision.id,
                lambda: self.repository.promote_study_revision(revision.id),
            )
        except Exception:
            self.repository.reset_study_promotion(revision.id)
            raise

    def _set_slide_steps(
        self,
        lecture_id: int,
        status: StepStatus,
        detail: str,
    ) -> None:
        for step in SLIDE_PIPELINE_STEPS:
            self.catalog.set_step_status(
                lecture_id,
                step,
                status,
                detail,
            )

    def _mark_failed(self, lecture_id: int, detail: str) -> None:
        for step in SLIDE_PIPELINE_STEPS:
            self.catalog.set_step_status(
                lecture_id,
                step,
                StepStatus.FAILED,
                detail,
            )
