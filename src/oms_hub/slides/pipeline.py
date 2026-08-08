from pathlib import Path

from oms_hub.config import Settings
from oms_hub.db import Database
from oms_hub.document_processing.domain import ParsedDocument, SourceSnapshot
from oms_hub.document_processing.router import ParserMode
from oms_hub.document_processing.shadow import DocumentShadowEvaluator
from oms_hub.domain import LectureKey, StepStatus, V2StepName
from oms_hub.files.atomic import sha256_file, verified_atomic_copy
from oms_hub.files.office import (
    OfficeConversionError,
    OfficeConverter,
    OfficeTimeoutError,
    OfficeUnavailableError,
)
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
        document_evaluator: DocumentShadowEvaluator | None = None,
    ):
        self.settings = settings
        self.converter = converter
        self.promotion = PromotionCoordinator()
        self.repository = IngestionRepository(database)
        self.catalog = CatalogRepository(database)
        self.document_evaluator = document_evaluator
        self.last_document: ParsedDocument | None = None

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
        recovering_promotion = revision.state == "promoting"
        try:
            if recovering_promotion:
                revision = self._promote_revision(
                    revision,
                    self._persisted_promotion_pairs(revision, derived),
                )
                self._mark_promoted(revision.lecture_id)
                return revision
            destinations = build_slide_destinations(
                self.settings,
                LectureKey(
                    lecture.subject,
                    lecture.exam_number,
                    lecture.lecture_number,
                    lecture.topic,
                ),
            )
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
            self._evaluate_document(item.original_filename, revision)
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
            self._mark_promoted(revision.lecture_id)
            return revision
        except Exception as error:
            self._mark_failed(revision.lecture_id, str(error))
            revision_state = None
            if recovering_promotion:
                stored = self.repository.get_study_revision(revision.id)
                if stored.state in {"proposed", "promoting"}:
                    revision_state = stored.state
            elif isinstance(
                error,
                (
                    OfficeConversionError,
                    OfficeTimeoutError,
                    OfficeUnavailableError,
                ),
            ):
                revision_state = "proposed"
            self.repository.finish_revision(
                item_id,
                revision.id,
                UploadState.FAILED,
                current=False,
                error=str(error),
                revision_state=revision_state,
            )
            raise

    def _evaluate_document(self, title: str, revision: StudyRevision) -> None:
        if self.document_evaluator is None:
            return
        snapshot = SourceSnapshot(
            id=f"slide-revision-{revision.id}",
            title=title,
            path=revision.immutable_source_path,
            media_type="application/vnd.openxmlformats-officedocument.presentationml.presentation",
            sha256=revision.source_sha256,
        )
        destination = (
            expanded_path(self.settings.data_dir)
            / "document-processing"
            / "shadow"
            / f"{revision.id}-{revision.source_sha256}.json"
        )
        try:
            result = self.document_evaluator.parse(
                snapshot,
                expanded_path(self.settings.data_dir)
                / "document-processing"
                / "assets"
                / str(revision.id),
                ParserMode(self.settings.document_parser_mode),
            )
            self.last_document = result.document
        except Exception:  # noqa: BLE001 - document analysis must not block filing
            result = None
        if result is None:
            report = self.document_evaluator.exceptional_report(
                revision.source_sha256,
                ParserMode(self.settings.document_parser_mode),
                "document_evaluation_failed",
            )
        else:
            report = result.report
        try:
            self.document_evaluator.write_report(report, destination)
        except Exception:
            return

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

    @staticmethod
    def _persisted_promotion_pairs(
        revision: StudyRevision,
        derived: Path,
    ) -> list[tuple[Path, Path]]:
        if (
            revision.canonical_source_path is None
            or revision.canonical_derived_path is None
            or revision.icloud_path is None
        ):
            raise ValueError("promoting slide revision has incomplete canonical paths")
        return [
            (revision.immutable_source_path, revision.canonical_source_path),
            (derived, revision.canonical_derived_path),
            (derived, revision.icloud_path),
        ]

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

    def _mark_promoted(self, lecture_id: int) -> None:
        self.catalog.set_step_status(
            lecture_id,
            V2StepName.SLIDES_FILED,
            StepStatus.COMPLETE,
            "PowerPoint and PDF filed on the NUC",
        )
        self.catalog.set_step_status(
            lecture_id,
            V2StepName.ICLOUD_PDF_STAGED,
            StepStatus.COMPLETE,
            "PDF staged in iCloud",
        )

    def _mark_failed(self, lecture_id: int, detail: str) -> None:
        for step in SLIDE_PIPELINE_STEPS:
            self.catalog.set_step_status(
                lecture_id,
                step,
                StepStatus.FAILED,
                detail,
            )
