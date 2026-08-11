"""Honest import of already-produced lecture artifacts.

This module deliberately does not create ingestion or generation jobs.  It is
an audit-preserving bridge for a cleaned transcript and a NotebookLM PDF that
already exist outside OMS Study Hub.
"""

import hashlib
import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID, uuid4

from pypdf.errors import PdfReadError
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from oms_hub.anki.models import AnkiCurationJobModel
from oms_hub.anki.sources import NotebookSummaryParser
from oms_hub.artifact_writes import (
    ArtifactWriteClaim,
    ArtifactWriteClaimLost,
    ArtifactWriteCoordinator,
)
from oms_hub.config import Settings
from oms_hub.db import Database
from oms_hub.domain import LectureKey
from oms_hub.files.atomic import sha256_file
from oms_hub.files.handle_relative import (
    HardenedWriteError,
    hardened_prepare_directory,
    hardened_sha256,
    hardened_unlink,
    hardened_verified_copy,
)
from oms_hub.files.pdf import validate_pdf
from oms_hub.files.trusted_paths import (
    is_indirection,
    trusted_existing_directory,
    trusted_managed_path,
)
from oms_hub.ingestion.domain import UploadKind, UploadState
from oms_hub.models import (
    ExistingArtifactImportModel,
    GenerationJobModel,
    LectureModel,
    OutlineOutputModel,
    StudyRevisionModel,
    UploadBatchModel,
    UploadItemModel,
    utc_now,
)
from oms_hub.repositories import CatalogRepository
from oms_hub.routing import build_outline_destination, build_transcript_destination, expanded_path
from oms_hub.study_generation.domain import OutlineRecord
from oms_hub.transcripts.pipeline import TranscriptValidationError, validate_transcript_bytes


class ExistingArtifactImportError(RuntimeError):
    pass


class ExistingArtifactRecoveryError(ExistingArtifactImportError):
    """A fenced adoption could not prove restoration of the old presentation."""


A0_PPTX_SHA256 = "b1c7abc3fb5d86476a3477d397e679ec42e61cff982fcec9dcb55a9d0a9c5469"
A0_PDF_SHA256 = "8bb427c3265f3a97997fd870f42794d59bd4850f963ccf292f3f9160ea9e0d38"
A0_CLEANED_TRANSCRIPT_SHA256 = "8d9b6d482c80401fb45c3bc76e70783b0ed49b7134d39ccfeb7da0451cd7f6a9"
A0_OUTLINE_SHA256 = "47a55e7cdfb6ddf4bc240626f48233392fd016fd7cc9acb96e331a820b7053ea"


def verify_a0_operator_files(
    pptx: Path, pdf: Path, cleaned_transcript: Path, outline: Path
) -> None:
    """Explicit A0 operator gate; callers provide the real shared files."""
    for path, digest, label in (
        (pptx, A0_PPTX_SHA256, "authoritative PPTX"),
        (pdf, A0_PDF_SHA256, "derived lecture PDF"),
        (cleaned_transcript, A0_CLEANED_TRANSCRIPT_SHA256, "cleaned transcript"),
        (outline, A0_OUTLINE_SHA256, "NotebookLM outline"),
    ):
        if not path.is_file() or sha256_file(path) != digest:
            raise ExistingArtifactImportError(f"A0 {label} SHA-256 does not match")


DestinationResolver = Callable[[Settings, LectureKey], tuple[Path, Path]]


def _default_destinations(settings: Settings, lecture: LectureKey) -> tuple[Path, Path]:
    return (
        build_transcript_destination(settings, lecture),
        build_outline_destination(settings, lecture),
    )


@dataclass(frozen=True, slots=True)
class ExistingArtifactImportRequest:
    lecture_id: int
    slides_revision_id: int
    slides_source_sha256: str
    slides_pdf_sha256: str
    cleaned_transcript: Path
    cleaned_transcript_sha256: str
    notebooklm_outline: Path
    notebooklm_outline_sha256: str
    # Supplying the file alone is deliberately inert.  All five values below
    # are required to adopt a derived slide PDF for the pinned PPTX revision.
    authoritative_derived_pdf: Path | None = None
    expected_current_pdf_sha256: str | None = None
    adoption_operator: str | None = None
    adoption_reason: str | None = None
    confirm_derived_adoption: bool = False


def verify_a0_request_identities(request: ExistingArtifactImportRequest) -> None:
    """Ensure the separately verified A0 files are the identities imported."""
    expected = (
        (request.slides_source_sha256, A0_PPTX_SHA256, "slides source PPTX"),
        (request.slides_pdf_sha256, A0_PDF_SHA256, "slides derived PDF"),
        (
            request.cleaned_transcript_sha256,
            A0_CLEANED_TRANSCRIPT_SHA256,
            "cleaned transcript",
        ),
        (request.notebooklm_outline_sha256, A0_OUTLINE_SHA256, "NotebookLM outline"),
    )
    for actual, required, label in expected:
        if actual != required:
            raise ExistingArtifactImportError(
                f"A0 request {label} SHA-256 does not match the authoritative file"
            )


@dataclass(frozen=True, slots=True)
class ExistingArtifactImportResult:
    import_id: str
    status: str
    idempotent: bool
    transcript_revision_id: int
    outline_id: int
    transcript_path: Path
    outline_path: Path
    transcript_sha256: str
    outline_sha256: str
    lecture_id: int
    slides_revision_id: int
    slides_source_sha256: str
    slides_pdf_sha256: str
    immutable_transcript_path: Path
    immutable_outline_path: Path
    bundle_sha256: str
    attempts: int
    subject: str
    exam_number: int
    lecture_number: int
    topic: str
    transcript_filename: str
    outline_filename: str
    adoption: dict[str, object] | None = None

    def as_dict(self) -> dict[str, object]:
        result: dict[str, object] = {
            "import_id": self.import_id,
            "bundle_sha256": self.bundle_sha256,
            "attempts": self.attempts,
            "status": self.status,
            "idempotent": self.idempotent,
            "lecture": {
                "id": self.lecture_id,
                "subject": self.subject,
                "exam_number": self.exam_number,
                "lecture_number": self.lecture_number,
                "topic": self.topic,
                "slides_revision_id": self.slides_revision_id,
                "slides_source_sha256": self.slides_source_sha256,
                "slides_pdf_sha256": self.slides_pdf_sha256,
            },
            "transcript": {
                "revision_id": self.transcript_revision_id,
                "canonical_path": str(self.transcript_path),
                "immutable_path": str(self.immutable_transcript_path),
                "sha256": self.transcript_sha256,
                "provenance_kind": "imported_cleaned",
                "original_filename": self.transcript_filename,
            },
            "outline": {
                "id": self.outline_id,
                "canonical_path": str(self.outline_path),
                "immutable_path": str(self.immutable_outline_path),
                "sha256": self.outline_sha256,
                "provenance_kind": "imported_notebooklm",
                "original_filename": self.outline_filename,
                "linked_slide_revision_id": self.slides_revision_id,
                "linked_slide_source_sha256": self.slides_source_sha256,
                "linked_slide_pdf_sha256": self.slides_pdf_sha256,
                "linked_transcript_revision_id": self.transcript_revision_id,
                "linked_transcript_sha256": self.transcript_sha256,
            },
        }
        if self.adoption is not None:
            result["derived_pdf_adoption"] = self.adoption
        return result

    def public_dict(self) -> dict[str, object]:
        """Compatibility spelling used by API callers."""
        return self.as_dict()


class ExistingArtifactImporter:
    def __init__(
        self,
        database: Database,
        settings: Settings,
        checkpoint: Callable[[str], None] | None = None,
        destination_resolver: DestinationResolver | None = None,
    ):
        self.database = database
        self.settings = settings
        self.catalog = CatalogRepository(database)
        self.writes = ArtifactWriteCoordinator(database, settings)
        self.checkpoint = checkpoint or (lambda phase: None)
        self.destination_resolver = destination_resolver or _default_destinations

    def import_artifacts(
        self, request: ExistingArtifactImportRequest
    ) -> ExistingArtifactImportResult:
        transcript, outline = self._validate_inputs(request)
        self._validate_adoption_request(request)
        self.checkpoint("post-initial-validation")
        bundle = self._bundle_identity(request)
        # Reject an absent or stale catalog pin before attempting to create the
        # foreign-keyed durable writer fence.
        self._validate_lecture_and_slide(
            request,
            bundle=bundle,
            allow_incomplete_adoption=True,
        )
        with self.writes.claim(request.lecture_id, "existing-artifact-import") as claim:
            # Re-read after acquiring the process-wide write exclusion.
            transcript, outline = self._validate_inputs(request)
            self._validate_adoption_request(request)
            self.checkpoint("post-lock-revalidation")
            lecture, slide = self._validate_lecture_and_slide(
                request,
                bundle=bundle,
                allow_incomplete_adoption=True,
            )
            lecture_key = LectureKey(
                lecture.subject,
                lecture.exam_number,
                lecture.lecture_number,
                lecture.topic,
            )
            canonical_transcript, canonical_outline = self._destinations(lecture_key)
            self._assert_adoption_consumers_absent(request, slide, bundle)
            # This deliberately happens before the first audit row.  A hostile
            # archive root must never acquire either a durable owner or bytes.
            self._prepare_existing_imports_root()
            existing = self._start_or_resume(
                bundle, request, claim.owner, lecture_key, canonical_transcript, canonical_outline
            )
            if existing.status == "complete":
                return self._idempotent(
                    existing,
                    request,
                    lecture,
                    slide,
                    canonical_transcript,
                    canonical_outline,
                )
            # `_start_or_resume` has committed the durable preparing audit,
            # but no UUID directory or immutable archive write has begun.
            # Keeping this checkpoint outside the Exception rollback block
            # models an abrupt process death for recovery coverage.
            self.checkpoint("after-audit-commit-before-first-copy")
            import_id = existing.id
            immutable_transcript = Path(existing.immutable_transcript_path or "")
            immutable_outline = Path(existing.immutable_outline_path or "")
            adopted_pdf = (
                Path(existing.imported_immutable_pdf_path or "")
                if self._adoption_requested(request)
                else None
            )
            created: list[tuple[Path, Path]] = []
            try:
                self._assert_no_current_artifacts(request.lecture_id)
                # The inputs are caller-controlled paths.  Check their pins
                # again after the durable audit exists but before any copy.
                self._validate_inputs(request)
                self._validate_adoption_request(request)
                imports_root = self._prepare_existing_imports_root()
                study_root = expanded_path(self.settings.study_root)
                phase = existing.recovery_phase
                if adopted_pdf is not None and phase == "recovery_required":
                    destinations = self._slide_promotion_destinations(slide)
                    pair = tuple(sha256_file(path) for path in destinations)
                    old_hash = request.expected_current_pdf_sha256 or ""
                    target_hash = request.slides_pdf_sha256
                    if pair == (old_hash, old_hash):
                        self._transition_phase(import_id, claim, "archived")
                        phase = "archived"
                    elif pair == (target_hash, old_hash):
                        self._transition_phase(import_id, claim, "canonical_promoted")
                        phase = "canonical_promoted"
                    elif pair == (target_hash, target_hash):
                        self._transition_phase(import_id, claim, "icloud_promoted")
                        phase = "icloud_promoted"
                    elif pair == (old_hash, target_hash):
                        self._replace_exact(
                            claim,
                            adopted_pdf,
                            destinations[0],
                            target_hash,
                            trusted_root=study_root,
                            source_root=imports_root,
                        )
                        self._transition_phase(import_id, claim, "icloud_promoted")
                        phase = "icloud_promoted"
                    else:
                        raise ExistingArtifactImportError(
                            "failed adoption recovery state is not monotonic"
                        )
                for source, destination, digest, root in (
                    (
                        request.cleaned_transcript,
                        immutable_transcript,
                        request.cleaned_transcript_sha256,
                        imports_root,
                    ),
                    (
                        request.notebooklm_outline,
                        immutable_outline,
                        request.notebooklm_outline_sha256,
                        imports_root,
                    ),
                    (
                        immutable_transcript,
                        canonical_transcript,
                        request.cleaned_transcript_sha256,
                        study_root,
                    ),
                    (
                        immutable_outline,
                        canonical_outline,
                        request.notebooklm_outline_sha256,
                        study_root,
                    ),
                ):
                    if not destination.exists():
                        created.append((destination, root))
                    claim.assert_owned()
                    self.checkpoint("before-copy")
                    self._copy_exact(
                        claim,
                        source,
                        destination,
                        digest,
                        trusted_root=root,
                        source_root=(
                            imports_root
                            if source in {immutable_transcript, immutable_outline}
                            else None
                        ),
                    )
                    claim.assert_owned()
                    self.checkpoint("after-copy")
                if adopted_pdf is not None and phase == "preparing":
                    assert request.authoritative_derived_pdf is not None
                    self._transition_phase(import_id, claim, "archive_copying")
                    self.checkpoint("after-adoption-archive_copying")
                    phase = "archive_copying"
                if adopted_pdf is not None and phase == "archive_copying":
                    assert request.authoritative_derived_pdf is not None
                    claim.assert_owned()
                    self._copy_exact(
                        claim,
                        request.authoritative_derived_pdf,
                        adopted_pdf,
                        request.slides_pdf_sha256,
                        trusted_root=imports_root,
                        source_root=None,
                    )
                    if not trusted_managed_path(
                        adopted_pdf, imports_root, require_regular_file=True
                    ):
                        raise ExistingArtifactImportError(
                            "adopted archive destination changed after pinned copy"
                        )
                    self.checkpoint("after-adoption-archive-copy")
                    self._transition_phase(import_id, claim, "archived")
                    self.checkpoint("after-adoption-archive")
                    phase = "archived"
                    claim.assert_owned()
                if adopted_pdf is not None and phase in {"archived", "canonical_promoted"}:
                    # The new archive is the only durable source permitted to
                    # replace the two mutable presentation copies.
                    destinations = self._slide_promotion_destinations(slide)
                    if self.settings.icloud_staging_root is None:
                        raise ExistingArtifactImportError("iCloud staging root is not configured")
                    icloud_root = expanded_path(self.settings.icloud_staging_root)
                    promotion_phases = (
                        ("canonical_promoted",) if phase == "archived" else ()
                    ) + (
                        ("icloud_promoted",)
                        if phase in {"archived", "canonical_promoted"}
                        else ()
                    )
                    for destination, phase in zip(
                        destinations[-len(promotion_phases) :], promotion_phases, strict=True
                    ):
                        claim.assert_owned()
                        self._replace_exact(
                            claim,
                            adopted_pdf,
                            destination,
                            request.slides_pdf_sha256,
                            trusted_root=(
                                study_root if destination == destinations[0] else icloud_root
                            ),
                            source_root=imports_root,
                        )
                        self.checkpoint(f"after-adoption-{phase}-copy")
                        self._transition_phase(import_id, claim, phase)
                        self.checkpoint(f"after-adoption-{phase}")
                        claim.assert_owned()
                # Pin again immediately before the durable current-record commit.
                if adopted_pdf is not None:
                    self._validate_adoption_post_promotion(request, slide, adopted_pdf)
                else:
                    lecture, slide = self._validate_lecture_and_slide(request)
                current_key = LectureKey(
                    lecture.subject,
                    lecture.exam_number,
                    lecture.lecture_number,
                    lecture.topic,
                )
                current_transcript, current_outline = self._destinations(current_key)
                current_transcript_path = str(current_transcript)
                current_outline_path = str(current_outline)
                if (
                    lecture.subject != existing.subject
                    or lecture.exam_number != existing.exam_number
                    or lecture.lecture_number != existing.lecture_number
                    or lecture.topic != existing.topic
                    or current_transcript_path != existing.canonical_transcript_path
                    or current_outline_path != existing.canonical_outline_path
                ):
                    raise ExistingArtifactImportError(
                        "pinned catalog identity or canonical destination changed"
                    )
                self._validate_inputs(request)
                claim.assert_owned()
                self.checkpoint("pre-current-db-commit")
                if adopted_pdf is not None:
                    self._transition_phase(import_id, claim, "precommit")
                    self.checkpoint("after-adoption-precommit")
                    claim.assert_owned()
                claim.assert_owned()
                with self.database.session() as session:
                    self._assert_no_current_artifacts(request.lecture_id, session)
                    batch_id = str(uuid4())
                    item_id = str(uuid4())
                    session.add(
                        UploadBatchModel(
                            id=batch_id,
                            kind=UploadKind.TRANSCRIPTS.value,
                            state=UploadState.COMPLETE.value,
                        )
                    )
                    session.flush()
                    session.add(
                        UploadItemModel(
                            id=item_id,
                            batch_id=batch_id,
                            kind=UploadKind.TRANSCRIPTS.value,
                            original_filename=request.cleaned_transcript.name,
                            staged_path=str(immutable_transcript),
                            sha256=request.cleaned_transcript_sha256,
                            size_bytes=len(transcript),
                            state=UploadState.COMPLETE.value,
                            lecture_id=request.lecture_id,
                            confidence=1.0,
                            evidence_json=json.dumps(
                                ["Imported already-cleaned transcript"], separators=(",", ":")
                            ),
                            manual_assignment=True,
                        )
                    )
                    session.flush()
                    revision = StudyRevisionModel(
                        upload_item_id=item_id,
                        lecture_id=request.lecture_id,
                        kind=UploadKind.TRANSCRIPTS.value,
                        source_sha256=request.cleaned_transcript_sha256,
                        immutable_source_path=str(immutable_transcript),
                        derived_sha256=request.cleaned_transcript_sha256,
                        immutable_derived_path=str(immutable_transcript),
                        canonical_source_path=str(canonical_transcript),
                        canonical_derived_path=str(canonical_transcript),
                        state="current",
                        current=True,
                        promoted_at=utc_now(),
                        provenance_kind="imported_cleaned",
                        import_id=import_id,
                    )
                    session.add(revision)
                    session.flush()
                    output = OutlineOutputModel(
                        lecture_id=request.lecture_id,
                        job_id=None,
                        path=str(canonical_outline),
                        sha256=request.notebooklm_outline_sha256,
                        current=True,
                        provenance_kind="imported_notebooklm",
                        original_filename=request.notebooklm_outline.name,
                        immutable_path=str(immutable_outline),
                        slide_revision_id=slide.id,
                        slide_sha256=request.slides_pdf_sha256,
                        slide_source_sha256=request.slides_source_sha256,
                        transcript_revision_id=revision.id,
                        transcript_sha256=request.cleaned_transcript_sha256,
                        import_id=import_id,
                    )
                    session.add(output)
                    session.flush()
                    audit = session.get(ExistingArtifactImportModel, import_id)
                    assert audit is not None
                    if adopted_pdf is not None:
                        # This is intentionally the same transaction as the
                        # imported transcript/outline graph and audit finish.
                        current_slide = session.get(StudyRevisionModel, slide.id)
                        if current_slide is None:
                            raise ExistingArtifactImportError("slides revision disappeared")
                        current_slide.derived_sha256 = request.slides_pdf_sha256
                        current_slide.immutable_derived_path = str(adopted_pdf)
                        current_slide.provenance_kind = "imported_derived"
                        current_slide.import_id = import_id
                        audit.recovery_phase = "committed"
                        existing.recovery_phase = "committed"
                    audit.status = "complete"
                    audit.error = None
                    audit.transcript_revision_id = revision.id
                    audit.outline_id = output.id
                    self.checkpoint("during-current-db-commit")
                    return ExistingArtifactImportResult(
                        import_id,
                        "complete",
                        False,
                        revision.id,
                        output.id,
                        canonical_transcript,
                        canonical_outline,
                        request.cleaned_transcript_sha256,
                        request.notebooklm_outline_sha256,
                        request.lecture_id,
                        request.slides_revision_id,
                        request.slides_source_sha256,
                        request.slides_pdf_sha256,
                        immutable_transcript,
                        immutable_outline,
                        existing.bundle_sha256,
                        existing.attempts,
                        existing.subject,
                        existing.exam_number,
                        existing.lecture_number,
                        existing.topic,
                        existing.transcript_filename or "",
                        existing.outline_filename or "",
                        self._adoption_public(existing),
                    )
            except ArtifactWriteClaimLost as error:
                # A successor may have promoted its own bytes.  Preserve all
                # evidence and surface the typed ownership loss unchanged.
                # This narrow conditional update is safe after a loss: it
                # cannot overwrite a successor's audit ownership or a
                # completed record, and it never touches presentation files.
                self._mark_claim_lost(import_id, claim.owner, error)
                raise
            except Exception as error:
                # A lost owner must not remove or overwrite successor state,
                # but it may record its own failed attempt only while the
                # durable audit still names that exact owner.
                if adopted_pdf is None:
                    self._mark_failed(import_id, claim.owner, error)
                else:
                    archive_root = (
                        expanded_path(self.settings.data_dir) / "artifacts" / "existing-imports"
                    )
                    archive_absent = (
                        trusted_managed_path(
                            adopted_pdf, archive_root, require_regular_file=False
                        )
                        and not adopted_pdf.exists()
                    )
                    if archive_absent:
                        self._mark_failed(import_id, claim.owner, error)
                    else:
                        self._mark_recovery_required(import_id, claim, error)
                    self.checkpoint("after-adoption-failure-state")
                if adopted_pdf is not None:
                    # The displaced archive is immutable evidence and is the
                    # sole rollback source.  Never delete the new audit copy.
                    rollback_error: Exception | None = None
                    try:
                        if self.settings.icloud_staging_root is None:
                            raise ExistingArtifactImportError(
                                "iCloud staging root is not configured"
                            )
                        rollback_destinations = self._slide_promotion_destinations(slide)
                        rollback_study_root = expanded_path(self.settings.study_root)
                        rollback_icloud_root = expanded_path(self.settings.icloud_staging_root)
                        for destination in rollback_destinations:
                            claim.assert_owned()
                            self._replace_exact(
                                claim,
                                Path(slide.immutable_derived_path or ""),
                                destination,
                                request.expected_current_pdf_sha256 or "",
                                trusted_root=(
                                    rollback_study_root
                                    if destination == rollback_destinations[0]
                                    else rollback_icloud_root
                                ),
                                source_root=(
                                    expanded_path(self.settings.data_dir) / "artifacts" / "v2"
                                ),
                            )
                        self._validate_old_presentation(slide, request)
                        archive_root = (
                            expanded_path(self.settings.data_dir)
                            / "artifacts"
                            / "existing-imports"
                        )
                        if not trusted_managed_path(
                            adopted_pdf, archive_root, require_regular_file=False
                        ):
                            raise ExistingArtifactRecoveryError(
                                "adopted archive changed while rollback was restoring"
                            )
                        if not adopted_pdf.exists():
                            self._mark_rollback_resumable(import_id, claim, "preparing")
                        elif (
                            not trusted_managed_path(
                                adopted_pdf, archive_root, require_regular_file=True
                            )
                            or sha256_file(adopted_pdf) != request.slides_pdf_sha256
                        ):
                            raise ExistingArtifactRecoveryError(
                                "adopted archive is not exact after rollback"
                            )
                        else:
                            self._mark_rollback_resumable(import_id, claim, "archived")
                    except Exception as rollback_failure:
                        rollback_error = rollback_failure
                    if rollback_error is not None:
                        raise ExistingArtifactRecoveryError(
                            "derived PDF adoption rollback could not restore the old presentation"
                        ) from rollback_error
                for path, root in reversed(created):
                    claim.assert_owned()
                    self.checkpoint("before-cleanup")
                    try:
                        hardened_unlink(path, root)
                    except OSError:
                        # A substituted path must never be followed merely to
                        # make cleanup best-effort; preserve the original
                        # import failure and leave its immutable evidence.
                        pass
                raise

    def _validate_inputs(self, request: ExistingArtifactImportRequest) -> tuple[bytes, bytes]:
        for label, digest in (
            ("slides source PPTX", request.slides_source_sha256),
            ("slides derived PDF", request.slides_pdf_sha256),
            ("cleaned transcript", request.cleaned_transcript_sha256),
            ("NotebookLM outline", request.notebooklm_outline_sha256),
        ):
            if not re.fullmatch(r"[0-9a-f]{64}", digest):
                raise ExistingArtifactImportError(
                    f"{label} SHA-256 must be 64 lowercase hexadecimal characters"
                )
        transcript_path = self._safe_file(request.cleaned_transcript, ".txt", "cleaned transcript")
        outline_path = self._safe_file(request.notebooklm_outline, ".pdf", "NotebookLM outline")
        transcript = transcript_path.read_bytes()
        outline = outline_path.read_bytes()
        if (
            len(transcript) > self.settings.max_upload_file_bytes
            or len(outline) > self.settings.max_upload_file_bytes
        ):
            raise ExistingArtifactImportError("artifact exceeds configured max upload size")
        if hashlib.sha256(transcript).hexdigest() != request.cleaned_transcript_sha256:
            raise ExistingArtifactImportError("cleaned transcript SHA-256 does not match")
        if hashlib.sha256(outline).hexdigest() != request.notebooklm_outline_sha256:
            raise ExistingArtifactImportError("NotebookLM outline SHA-256 does not match")
        try:
            validate_transcript_bytes(transcript, self.settings.max_upload_file_bytes)
        except TranscriptValidationError as error:
            raise ExistingArtifactImportError(f"cleaned transcript is invalid: {error}") from error
        try:
            validate_pdf(outline_path)
        except (OSError, ValueError, PdfReadError) as error:
            raise ExistingArtifactImportError(
                f"NotebookLM outline PDF is invalid: {error}"
            ) from error
        try:
            NotebookSummaryParser().parse(
                OutlineRecord(
                    1,
                    request.lecture_id,
                    None,
                    outline_path,
                    request.notebooklm_outline_sha256,
                    True,
                )
            )
        except (OSError, ValueError) as error:
            raise ExistingArtifactImportError(
                f"NotebookLM outline is not a valid required-heading summary: {error}"
            ) from error
        return transcript, outline

    @staticmethod
    def _adoption_requested(request: ExistingArtifactImportRequest) -> bool:
        return request.authoritative_derived_pdf is not None

    def _validate_adoption_request(self, request: ExistingArtifactImportRequest) -> None:
        """Reject incomplete adoption intent before opening an audit or claim."""
        supplied = (
            request.authoritative_derived_pdf,
            request.expected_current_pdf_sha256,
            request.adoption_operator,
            request.adoption_reason,
        )
        if (
            not any(value is not None for value in supplied)
            and not request.confirm_derived_adoption
        ):
            return
        if (
            request.authoritative_derived_pdf is None
            or request.expected_current_pdf_sha256 is None
            or not request.confirm_derived_adoption
            or not (request.adoption_operator or "").strip()
            or not (request.adoption_reason or "").strip()
        ):
            raise ExistingArtifactImportError(
                "derived PDF adoption requires authoritative PDF, expected current SHA-256, "
                "nonempty operator/reason, and --confirm-derived-adoption"
            )
        for label, digest in (
            ("expected current PDF", request.expected_current_pdf_sha256),
            ("target derived PDF", request.slides_pdf_sha256),
        ):
            if not re.fullmatch(r"[0-9a-f]{64}", digest):
                raise ExistingArtifactImportError(
                    f"{label} SHA-256 must be 64 lowercase hexadecimal characters"
                )
        target = self._safe_file(
            request.authoritative_derived_pdf, ".pdf", "authoritative derived PDF"
        )
        if target.stat().st_size > self.settings.max_upload_file_bytes:
            raise ExistingArtifactImportError("artifact exceeds configured max upload size")
        try:
            validate_pdf(target)
        except (OSError, ValueError, PdfReadError) as error:
            raise ExistingArtifactImportError(
                f"authoritative derived PDF is invalid: {error}"
            ) from error
        if sha256_file(target) != request.slides_pdf_sha256:
            raise ExistingArtifactImportError("authoritative derived PDF SHA-256 does not match")

    def _destinations(self, lecture: LectureKey) -> tuple[Path, Path]:
        try:
            transcript, outline = self.destination_resolver(self.settings, lecture)
        except (OSError, ValueError) as error:
            raise ExistingArtifactImportError(
                "configured canonical artifact destination is not trusted"
            ) from error
        root = expanded_path(self.settings.study_root)
        self._require_managed_destination(transcript, root, "transcript")
        self._require_managed_destination(outline, root, "NotebookLM outline")
        return transcript, outline

    def _prepare_existing_imports_root(self) -> Path:
        """Create the fixed immutable-import root only below trusted directories."""
        data_dir = expanded_path(self.settings.data_dir)
        artifacts = data_dir / "artifacts"
        imports_root = artifacts / "existing-imports"
        if not trusted_existing_directory(data_dir):
            raise ExistingArtifactImportError("configured data directory is not trusted")
        try:
            hardened_prepare_directory(artifacts)
            hardened_prepare_directory(imports_root)
        except HardenedWriteError as error:
            raise ExistingArtifactImportError(
                "existing-import managed root is not trusted"
            ) from error
        # Recheck after creation: another process may have substituted an
        # indirection between the two mkdir operations.
        if not trusted_existing_directory(imports_root):
            raise ExistingArtifactImportError("existing-import managed root is not trusted")
        return imports_root

    def _validate_future_audit_destinations(self, imports_root: Path, import_id: str) -> Path:
        audit_root = imports_root / import_id
        if audit_root.exists():
            raise ExistingArtifactImportError("existing-import audit root already exists")
        for destination, label in (
            (audit_root / "cleaned.txt", "transcript"),
            (audit_root / "outline.pdf", "NotebookLM outline"),
            (audit_root / "derived-slide.pdf", "derived PDF"),
        ):
            self._require_managed_destination(destination, imports_root, label)
        return audit_root

    @staticmethod
    def _require_managed_destination(path: Path, root: Path, label: str) -> None:
        if not trusted_managed_path(path, root, require_regular_file=False):
            raise ExistingArtifactImportError(
                f"{label} destination escapes its configured managed root"
            )

    @staticmethod
    def _safe_file(path: Path, suffix: str, label: str) -> Path:
        if (
            not path.is_absolute()
            or is_indirection(path)
            or not path.is_file()
            or path.suffix.casefold() != suffix
        ):
            raise ExistingArtifactImportError(
                f"{label} must be an absolute regular non-symlink {suffix} file"
            )
        return path

    def _validate_lecture_and_slide(
        self,
        request: ExistingArtifactImportRequest,
        *,
        bundle: str | None = None,
        allow_incomplete_adoption: bool = False,
    ) -> tuple[LectureModel, StudyRevisionModel]:
        lecture = self.catalog.get_lecture(request.lecture_id)
        if lecture is None:
            raise ExistingArtifactImportError("catalog lecture does not exist")
        with self.database.session() as session:
            slide = session.get(StudyRevisionModel, request.slides_revision_id)
            if slide is None:
                raise ExistingArtifactImportError(
                    "slides revision is not the exact current lecture PPTX/PDF revision/hashes"
                )
            expected_pdf = request.slides_pdf_sha256
            accepting_committed_adoption = False
            resuming_adoption = False
            if self._adoption_requested(request):
                old_pdf = request.expected_current_pdf_sha256
                assert old_pdf is not None
                accepting_committed_adoption = (
                    slide.derived_sha256 == request.slides_pdf_sha256
                    and slide.provenance_kind == "imported_derived"
                    and slide.import_id is not None
                )
                if accepting_committed_adoption:
                    audit = session.get(ExistingArtifactImportModel, slide.import_id)
                    accepting_committed_adoption = (
                        audit is not None
                        and audit.status == "complete"
                        and audit.expected_current_pdf_sha256 == old_pdf
                        and audit.imported_pdf_sha256 == request.slides_pdf_sha256
                    )
                if allow_incomplete_adoption and bundle is not None:
                    audit = session.scalar(
                        select(ExistingArtifactImportModel).where(
                            ExistingArtifactImportModel.bundle_sha256 == bundle,
                            ExistingArtifactImportModel.status.in_(("failed", "preparing")),
                        )
                    )
                    resuming_adoption = audit is not None
                expected_pdf = (
                    request.slides_pdf_sha256 if accepting_committed_adoption else old_pdf
                )
            if (
                slide.lecture_id != request.lecture_id
                or slide.kind != UploadKind.SLIDES.value
                or not slide.current
                or slide.source_sha256 != request.slides_source_sha256
                or slide.derived_sha256 != expected_pdf
            ):
                raise ExistingArtifactImportError(
                    "slides revision is not the exact current lecture PPTX/PDF revision/hashes"
                )
            immutable_root = expanded_path(self.settings.data_dir) / "artifacts" / "v2"
            managed_root = expanded_path(self.settings.study_root)
            derived_root = (
                expanded_path(self.settings.data_dir) / "artifacts" / "existing-imports"
                if accepting_committed_adoption
                else immutable_root
            )
            checks: tuple[tuple[str | None, str, Path, str], ...] = (
                (
                    slide.immutable_source_path,
                    request.slides_source_sha256,
                    immutable_root,
                    "PPTX immutable",
                ),
                (
                    slide.canonical_source_path,
                    request.slides_source_sha256,
                    managed_root,
                    "PPTX canonical",
                ),
                (
                    slide.immutable_derived_path,
                    expected_pdf,
                    derived_root,
                    "PDF immutable",
                ),
            )
            if not resuming_adoption:
                checks += (
                    (
                        slide.canonical_derived_path,
                        expected_pdf,
                        managed_root,
                        "PDF canonical",
                    ),
                )
            for path, digest, root, label in checks:
                if path is None:
                    raise ExistingArtifactImportError(f"slides revision {label} path is missing")
                self._validate_file(Path(path), digest, root, label)
            if self._adoption_requested(request):
                if self.settings.icloud_staging_root is None:
                    raise ExistingArtifactImportError("iCloud staging root is not configured")
                icloud_root = expanded_path(self.settings.icloud_staging_root)
                if slide.icloud_path is None:
                    raise ExistingArtifactImportError("slides revision iCloud PDF path is missing")
                if not resuming_adoption:
                    self._validate_file(
                        Path(slide.icloud_path), expected_pdf, icloud_root, "PDF iCloud"
                    )
        return lecture, slide

    @staticmethod
    def _slide_promotion_destinations(slide: StudyRevisionModel) -> tuple[Path, Path]:
        if slide.canonical_derived_path is None or slide.icloud_path is None:
            raise ExistingArtifactImportError("slides revision PDF promotion paths are missing")
        return Path(slide.canonical_derived_path), Path(slide.icloud_path)

    def _validate_adoption_post_promotion(
        self, request: ExistingArtifactImportRequest, slide: StudyRevisionModel, archived: Path
    ) -> None:
        """Validate the fenced intermediate state before its single DB commit."""
        old_pdf = request.expected_current_pdf_sha256
        assert old_pdf is not None
        immutable_root = expanded_path(self.settings.data_dir) / "artifacts" / "v2"
        managed_root = expanded_path(self.settings.study_root)
        if self.settings.icloud_staging_root is None:
            raise ExistingArtifactImportError("iCloud staging root is not configured")
        icloud_root = expanded_path(self.settings.icloud_staging_root)
        self._validate_file(
            Path(slide.immutable_source_path),
            request.slides_source_sha256,
            immutable_root,
            "PPTX immutable",
        )
        self._validate_file(
            Path(slide.canonical_source_path or ""),
            request.slides_source_sha256,
            managed_root,
            "PPTX canonical",
        )
        self._validate_file(
            Path(slide.immutable_derived_path or ""),
            old_pdf,
            immutable_root,
            "PDF immutable",
        )
        imported_root = expanded_path(self.settings.data_dir) / "artifacts" / "existing-imports"
        self._validate_file(
            archived,
            request.slides_pdf_sha256,
            imported_root,
            "PDF imported archive",
        )
        canonical, icloud = self._slide_promotion_destinations(slide)
        self._validate_file(canonical, request.slides_pdf_sha256, managed_root, "PDF promoted")
        self._validate_file(icloud, request.slides_pdf_sha256, icloud_root, "PDF iCloud")

    def _validate_old_presentation(
        self, slide: StudyRevisionModel, request: ExistingArtifactImportRequest
    ) -> None:
        old_pdf = request.expected_current_pdf_sha256
        assert old_pdf is not None
        managed_root = expanded_path(self.settings.study_root)
        if self.settings.icloud_staging_root is None:
            raise ExistingArtifactImportError("iCloud staging root is not configured")
        icloud_root = expanded_path(self.settings.icloud_staging_root)
        canonical, icloud = self._slide_promotion_destinations(slide)
        self._validate_file(canonical, old_pdf, managed_root, "PDF rollback")
        self._validate_file(icloud, old_pdf, icloud_root, "PDF iCloud rollback")

    def _transition_phase(self, import_id: str, claim: ArtifactWriteClaim, phase: str) -> None:
        claim.assert_owned()
        with self.database.session() as session:
            session.execute(
                update(ExistingArtifactImportModel)
                .where(
                    ExistingArtifactImportModel.id == import_id,
                    ExistingArtifactImportModel.owner == claim.owner,
                    ExistingArtifactImportModel.status != "complete",
                )
                .values(recovery_phase=phase)
            )
            audit = session.get(ExistingArtifactImportModel, import_id)
            if audit is None or audit.owner != claim.owner or audit.status == "complete":
                raise ArtifactWriteClaimLost("existing import audit fence was replaced")
        claim.assert_owned()

    def _mark_rollback_resumable(
        self, import_id: str, claim: ArtifactWriteClaim, phase: str
    ) -> None:
        """Publish an owned rollback as a retryable preparing audit without erasing its error."""
        claim.assert_owned()
        with self.database.session() as session:
            session.execute(
                update(ExistingArtifactImportModel)
                .where(
                    ExistingArtifactImportModel.id == import_id,
                    ExistingArtifactImportModel.owner == claim.owner,
                    ExistingArtifactImportModel.status != "complete",
                )
                .values(status="preparing", recovery_phase=phase)
            )
            audit = session.get(ExistingArtifactImportModel, import_id)
            if audit is None or audit.owner != claim.owner or audit.status != "preparing":
                raise ArtifactWriteClaimLost("existing import audit fence was replaced")
        claim.assert_owned()

    def _validate_file(self, path: Path, digest: str, root: Path, label: str) -> None:
        if not trusted_managed_path(path, root, require_regular_file=True):
            raise ExistingArtifactImportError(
                f"slides revision {label} must be an absolute regular non-symlink file"
            )
        if sha256_file(path) != digest:
            raise ExistingArtifactImportError(f"slides revision {label} path/hash is not exact")

    def _assert_no_current_artifacts(self, lecture_id: int, session: Session | None = None) -> None:
        if session is None:
            with self.database.session() as owned_session:
                self._assert_no_current_artifacts(lecture_id, owned_session)
                return
        current_revision = session.scalar(
            select(StudyRevisionModel.id).where(
                StudyRevisionModel.lecture_id == lecture_id,
                StudyRevisionModel.kind == UploadKind.TRANSCRIPTS.value,
                StudyRevisionModel.current.is_(True),
            )
        )
        current_outline = session.scalar(
            select(OutlineOutputModel.id).where(
                OutlineOutputModel.lecture_id == lecture_id,
                OutlineOutputModel.current.is_(True),
            )
        )
        if current_revision is not None or current_outline is not None:
            raise ExistingArtifactImportError(
                "lecture already has current artifacts; replacement is unsupported"
            )

    def _assert_adoption_consumers_absent(
        self, request: ExistingArtifactImportRequest, slide: StudyRevisionModel, bundle: str
    ) -> None:
        if not self._adoption_requested(request):
            return
        with self.database.session() as session:
            imported = session.scalar(
                select(ExistingArtifactImportModel).where(
                    ExistingArtifactImportModel.slide_revision_id == slide.id,
                    ExistingArtifactImportModel.status == "complete",
                )
            )
            if imported is not None and imported.bundle_sha256 == bundle:
                # The completed exact bundle is an idempotent observation;
                # later Anki consumers do not turn that observation into a
                # mutation attempt.
                return
            generation = session.scalar(
                select(GenerationJobModel.id).where(GenerationJobModel.pdf_revision_id == slide.id)
            )
            outline = session.scalar(
                select(OutlineOutputModel.id).where(
                    OutlineOutputModel.slide_revision_id == slide.id,
                    OutlineOutputModel.slide_sha256 == request.expected_current_pdf_sha256,
                )
            )
            # The source ids are a persisted JSON pin, so do not infer that
            # only queued jobs matter: completed/replay snapshots consume the
            # old derived identity too.
            anki_jobs = session.scalars(select(AnkiCurationJobModel)).all()
            anki = any(
                slide.id in self._validated_anki_source_ids(job.source_revision_ids_json)
                for job in anki_jobs
            )
            if imported is not None or generation is not None or outline is not None or anki:
                raise ExistingArtifactImportError(
                    "derived slide identity is already consumed; adoption is unsupported"
                )

    @staticmethod
    def _validated_anki_source_ids(payload: str) -> tuple[int, ...]:
        try:
            values = json.loads(payload)
        except (TypeError, json.JSONDecodeError) as error:
            raise ExistingArtifactImportError(
                "Anki source pin is malformed; adoption is unsupported"
            ) from error
        if (
            not isinstance(values, list)
            or any(type(value) is not int or value <= 0 for value in values)
            or len(values) != len(set(values))
        ):
            raise ExistingArtifactImportError(
                "Anki source pin is malformed; adoption is unsupported"
            )
        return tuple(values)

    @staticmethod
    def _bundle_identity(request: ExistingArtifactImportRequest) -> str:
        data = {
            "lecture_id": request.lecture_id,
            "slides_revision_id": request.slides_revision_id,
            "slides_source_sha256": request.slides_source_sha256,
            "slides_pdf_sha256": request.slides_pdf_sha256,
            "transcript_filename": request.cleaned_transcript.name,
            "outline_filename": request.notebooklm_outline.name,
            "transcript_sha256": request.cleaned_transcript_sha256,
            "outline_sha256": request.notebooklm_outline_sha256,
            "expected_current_pdf_sha256": request.expected_current_pdf_sha256,
            "adoption_filename": (
                request.authoritative_derived_pdf.name
                if request.authoritative_derived_pdf is not None
                else None
            ),
            "adoption_operator": (
                request.adoption_operator.strip() if request.adoption_operator else None
            ),
            "adoption_reason": (
                request.adoption_reason.strip() if request.adoption_reason else None
            ),
            "confirm_derived_adoption": request.confirm_derived_adoption,
        }
        return hashlib.sha256(
            json.dumps(data, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()

    def _start_or_resume(
        self,
        bundle: str,
        request: ExistingArtifactImportRequest,
        owner: str,
        lecture: LectureKey,
        canonical_transcript: Path,
        canonical_outline: Path,
    ) -> ExistingArtifactImportModel:
        with self.database.session() as session:
            current = session.scalar(
                select(ExistingArtifactImportModel).where(
                    ExistingArtifactImportModel.bundle_sha256 == bundle
                )
            )
            if current is None:
                provenance_conflict = session.scalar(
                    select(ExistingArtifactImportModel.id).where(
                        ExistingArtifactImportModel.lecture_id == request.lecture_id,
                        ExistingArtifactImportModel.slide_revision_id == request.slides_revision_id,
                        ExistingArtifactImportModel.slide_source_sha256
                        == request.slides_source_sha256,
                        ExistingArtifactImportModel.slide_pdf_sha256 == request.slides_pdf_sha256,
                        ExistingArtifactImportModel.transcript_sha256
                        == request.cleaned_transcript_sha256,
                        ExistingArtifactImportModel.outline_sha256
                        == request.notebooklm_outline_sha256,
                    )
                )
                if provenance_conflict is not None:
                    raise ExistingArtifactImportError(
                        "same-byte import has different source filenames; provenance conflict"
                    )
                self._assert_no_current_artifacts(request.lecture_id, session)
                imports_root = self._prepare_existing_imports_root()
                import_id = str(uuid4())
                immutable_root = self._validate_future_audit_destinations(imports_root, import_id)
                current = ExistingArtifactImportModel(
                    id=import_id,
                    bundle_sha256=bundle,
                    lecture_id=request.lecture_id,
                    slide_revision_id=request.slides_revision_id,
                    slide_source_sha256=request.slides_source_sha256,
                    slide_pdf_sha256=request.slides_pdf_sha256,
                    transcript_sha256=request.cleaned_transcript_sha256,
                    outline_sha256=request.notebooklm_outline_sha256,
                    status="preparing",
                    owner=owner,
                    subject=lecture.subject,
                    exam_number=lecture.exam_number,
                    lecture_number=lecture.lecture_number,
                    topic=lecture.topic,
                    canonical_transcript_path=str(canonical_transcript),
                    canonical_outline_path=str(canonical_outline),
                    immutable_transcript_path=str(immutable_root / "cleaned.txt"),
                    immutable_outline_path=str(immutable_root / "outline.pdf"),
                    transcript_filename=request.cleaned_transcript.name,
                    outline_filename=request.notebooklm_outline.name,
                    expected_current_pdf_sha256=request.expected_current_pdf_sha256,
                    previous_pdf_sha256=(
                        request.expected_current_pdf_sha256
                        if self._adoption_requested(request)
                        else None
                    ),
                    previous_immutable_pdf_path=(
                        str(self._require_old_immutable_path(request))
                        if self._adoption_requested(request)
                        else None
                    ),
                    imported_pdf_sha256=(
                        request.slides_pdf_sha256 if self._adoption_requested(request) else None
                    ),
                    imported_immutable_pdf_path=(
                        str(immutable_root / "derived-slide.pdf")
                        if self._adoption_requested(request)
                        else None
                    ),
                    derived_provenance=(
                        "imported_derived" if self._adoption_requested(request) else None
                    ),
                    adoption_operator=(
                        request.adoption_operator.strip()
                        if self._adoption_requested(request) and request.adoption_operator
                        else None
                    ),
                    adoption_reason=(
                        request.adoption_reason.strip()
                        if self._adoption_requested(request) and request.adoption_reason
                        else None
                    ),
                    adoption_confirmed_at=(
                        utc_now() if self._adoption_requested(request) else None
                    ),
                    recovery_phase=("preparing" if self._adoption_requested(request) else None),
                )
                session.add(current)
                session.flush()
            elif current.status != "complete":
                self._validate_resume_audit(
                    current,
                    request,
                    lecture,
                    canonical_transcript,
                    canonical_outline,
                )
                current.status = "preparing"
                current.attempts += 1
                current.error = None
                current.owner = owner
            session.flush()
            session.expunge(current)
            return current

    def _require_old_immutable_path(self, request: ExistingArtifactImportRequest) -> Path:
        with self.database.session() as session:
            slide = session.get(StudyRevisionModel, request.slides_revision_id)
            if slide is None or slide.immutable_derived_path is None:
                raise ExistingArtifactImportError("slides revision immutable PDF path is missing")
            return Path(slide.immutable_derived_path)

    def _validate_resume_audit(
        self,
        audit: ExistingArtifactImportModel,
        request: ExistingArtifactImportRequest,
        lecture: LectureKey,
        canonical_transcript: Path,
        canonical_outline: Path,
    ) -> None:
        """Treat failed/preparing audit metadata as hostile before a retry writes."""
        transcript_name = request.cleaned_transcript.name
        outline_name = request.notebooklm_outline.name
        if (
            not transcript_name
            or transcript_name != Path(transcript_name).name
            or not outline_name
            or outline_name != Path(outline_name).name
            or audit.lecture_id != request.lecture_id
            or audit.slide_revision_id != request.slides_revision_id
            or audit.slide_source_sha256 != request.slides_source_sha256
            or audit.slide_pdf_sha256 != request.slides_pdf_sha256
            or audit.transcript_sha256 != request.cleaned_transcript_sha256
            or audit.outline_sha256 != request.notebooklm_outline_sha256
            or audit.transcript_filename != transcript_name
            or audit.outline_filename != outline_name
            or audit.status not in ({"failed", "preparing"})
            or (audit.subject, audit.exam_number, audit.lecture_number, audit.topic)
            != (lecture.subject, lecture.exam_number, lecture.lecture_number, lecture.topic)
            or audit.canonical_transcript_path != str(canonical_transcript)
            or audit.canonical_outline_path != str(canonical_outline)
        ):
            raise ExistingArtifactImportError("failed import audit identity does not match retry")
        try:
            if str(UUID(audit.id)) != audit.id:
                raise ValueError
        except ValueError as error:
            raise ExistingArtifactImportError(
                "failed import audit ID is not a canonical UUID"
            ) from error
        managed_root = expanded_path(self.settings.data_dir) / "artifacts" / "existing-imports"
        if is_indirection(managed_root):
            raise ExistingArtifactImportError("existing-import managed root is indirect")
        immutable_root = managed_root / audit.id
        expected = (
            (audit.immutable_transcript_path, immutable_root / "cleaned.txt", "transcript"),
            (audit.immutable_outline_path, immutable_root / "outline.pdf", "outline"),
        )
        if self._adoption_requested(request):
            adoption_expected = (
                (audit.expected_current_pdf_sha256, request.expected_current_pdf_sha256),
                (audit.previous_pdf_sha256, request.expected_current_pdf_sha256),
                (audit.imported_pdf_sha256, request.slides_pdf_sha256),
                (audit.derived_provenance, "imported_derived"),
            )
            if (
                any(actual != expected for actual, expected in adoption_expected)
                or audit.previous_immutable_pdf_path is None
                or audit.imported_immutable_pdf_path is None
                or audit.adoption_operator != (request.adoption_operator or "").strip()
                or audit.adoption_reason != (request.adoption_reason or "").strip()
                or not audit.adoption_confirmed_at
                or audit.recovery_phase
                not in {
                    "preparing",
                    "archive_copying",
                    "archived",
                    "canonical_promoted",
                    "icloud_promoted",
                    "precommit",
                    "recovery_required",
                }
            ):
                raise ExistingArtifactImportError(
                    "failed adoption audit identity does not match retry"
                )
            if audit.imported_immutable_pdf_path != str(immutable_root / "derived-slide.pdf"):
                raise ExistingArtifactImportError(
                    "failed adoption audit immutable PDF destination is invalid"
                )
            imported_path = Path(audit.imported_immutable_pdf_path)
            if not trusted_managed_path(
                imported_path, managed_root, require_regular_file=False
            ):
                raise ExistingArtifactImportError(
                    "failed adoption audit immutable PDF destination is unmanaged"
                )
            old_path = self._require_old_immutable_path(request)
            if audit.previous_immutable_pdf_path != str(old_path):
                raise ExistingArtifactImportError(
                    "failed adoption audit preserved Office PDF path is invalid"
                )
            self._validate_resume_adoption_state(audit, request, old_path)
        for stored, destination, label in expected:
            if stored is None or Path(stored) != destination:
                raise ExistingArtifactImportError(
                    f"failed import audit immutable {label} destination is invalid"
                )
            self._require_managed_destination(destination, managed_root, label)
            if is_indirection(destination) or is_indirection(immutable_root):
                raise ExistingArtifactImportError(
                    f"failed import audit immutable {label} destination is a symlink"
                )
        for destination, label in (
            (canonical_transcript, "canonical transcript"),
            (canonical_outline, "canonical outline"),
        ):
            if not trusted_managed_path(
                destination,
                expanded_path(self.settings.study_root),
                require_regular_file=False,
            ):
                raise ExistingArtifactImportError(
                    f"failed import audit {label} destination is unmanaged"
                )
        if self._adoption_requested(request):
            if audit.recovery_phase == "preparing":
                self._validate_preparing_adoption_precursors(
                    audit,
                    request,
                    canonical_transcript,
                    canonical_outline,
                )
            elif audit.recovery_phase == "archive_copying":
                self._validate_archive_copying_precursors(
                    audit,
                    request,
                    canonical_transcript,
                    canonical_outline,
                )

    def _validate_preparing_adoption_precursors(
        self,
        audit: ExistingArtifactImportModel,
        request: ExistingArtifactImportRequest,
        canonical_transcript: Path,
        canonical_outline: Path,
    ) -> None:
        """Accept only a same-hash prefix of the four pre-archive copies."""
        imported = Path(audit.imported_immutable_pdf_path or "")
        precursor_files = (
            (Path(audit.immutable_transcript_path or ""), request.cleaned_transcript_sha256),
            (Path(audit.immutable_outline_path or ""), request.notebooklm_outline_sha256),
            (canonical_transcript, request.cleaned_transcript_sha256),
            (canonical_outline, request.notebooklm_outline_sha256),
        )
        try:
            if imported.exists():
                raise ExistingArtifactImportError(
                    "failed adoption archive exists before its durable phase"
                )
            present = tuple(path.exists() for path, _digest in precursor_files)
            if any(present[index] and not all(present[:index]) for index in range(1, 4)):
                raise ExistingArtifactImportError(
                    "failed adoption precursor files are not an exact copy prefix"
                )
            for path, expected_digest in precursor_files:
                if path.exists() and sha256_file(path) != expected_digest:
                    raise ExistingArtifactImportError(
                        "failed adoption precursor file hash is not exact"
                    )
        except OSError as error:
            raise ExistingArtifactImportError(
                "failed adoption precursor file is unavailable"
            ) from error

    def _validate_archive_copying_precursors(
        self,
        audit: ExistingArtifactImportModel,
        request: ExistingArtifactImportRequest,
        canonical_transcript: Path,
        canonical_outline: Path,
    ) -> None:
        """Require full precursor evidence around the archive-copy boundary."""
        imported = Path(audit.imported_immutable_pdf_path or "")
        precursor_files = (
            (Path(audit.immutable_transcript_path or ""), request.cleaned_transcript_sha256),
            (Path(audit.immutable_outline_path or ""), request.notebooklm_outline_sha256),
            (canonical_transcript, request.cleaned_transcript_sha256),
            (canonical_outline, request.notebooklm_outline_sha256),
        )
        try:
            if any(
                not path.is_file() or sha256_file(path) != expected_digest
                for path, expected_digest in precursor_files
            ):
                raise ExistingArtifactImportError(
                    "failed adoption archive-copying precursors are not exact"
                )
            if imported.exists() and sha256_file(imported) != request.slides_pdf_sha256:
                raise ExistingArtifactImportError(
                    "failed adoption archive-copying archive hash is not exact"
                )
        except OSError as error:
            raise ExistingArtifactImportError(
                "failed adoption archive-copying evidence is unavailable"
            ) from error

    def _validate_resume_adoption_state(
        self,
        audit: ExistingArtifactImportModel,
        request: ExistingArtifactImportRequest,
        old_path: Path,
    ) -> None:
        """Accept only the phase's exact old/target mutable presentation state."""
        old_hash = request.expected_current_pdf_sha256
        assert old_hash is not None
        target_hash = request.slides_pdf_sha256
        imported_path = Path(audit.imported_immutable_pdf_path or "")
        immutable_root = expanded_path(self.settings.data_dir) / "artifacts" / "v2"
        imports_root = immutable_root.parent / "existing-imports"
        managed_root = expanded_path(self.settings.study_root)
        if self.settings.icloud_staging_root is None:
            raise ExistingArtifactImportError("iCloud staging root is not configured")
        icloud_root = expanded_path(self.settings.icloud_staging_root)
        with self.database.session() as session:
            slide = session.get(StudyRevisionModel, request.slides_revision_id)
            if (
                slide is None
                or slide.lecture_id != request.lecture_id
                or slide.kind != UploadKind.SLIDES.value
                or not slide.current
                or slide.source_sha256 != request.slides_source_sha256
                or slide.derived_sha256 != old_hash
                or slide.immutable_derived_path != str(old_path)
                or slide.provenance_kind == "imported_derived"
                or slide.import_id is not None
                or slide.canonical_derived_path is None
                or slide.icloud_path is None
            ):
                raise ExistingArtifactImportError("failed adoption slide state is not resumable")
            canonical = Path(slide.canonical_derived_path)
            icloud = Path(slide.icloud_path)
            source = Path(slide.immutable_source_path)
            canonical_source = Path(slide.canonical_source_path or "")
        self._validate_file(source, request.slides_source_sha256, immutable_root, "PPTX immutable")
        self._validate_file(
            canonical_source,
            request.slides_source_sha256,
            managed_root,
            "PPTX canonical",
        )
        self._validate_file(old_path, old_hash, immutable_root, "PDF immutable")
        if not trusted_managed_path(
            imported_path, imports_root, require_regular_file=False
        ):
            raise ExistingArtifactImportError("failed adoption imported archive is unmanaged")
        for destination, label in ((canonical, "PDF canonical"), (icloud, "PDF iCloud")):
            self._require_managed_destination(
                destination,
                managed_root if destination == canonical else icloud_root,
                label,
            )
            if is_indirection(destination) or not destination.is_file():
                raise ExistingArtifactImportError(f"failed adoption {label} is not a regular file")
        archive_present = imported_path.is_file() and not is_indirection(imported_path)
        if archive_present and sha256_file(imported_path) != target_hash:
            raise ExistingArtifactImportError("failed adoption imported archive hash is not exact")
        phase = audit.recovery_phase
        allowed: tuple[tuple[str, str], ...]
        if phase is None:
            raise ExistingArtifactImportError("failed adoption phase is missing")
        if phase not in {"preparing", "archive_copying"} and not archive_present:
            raise ExistingArtifactImportError("failed adoption imported archive is unavailable")
        # The durable phase is the only authority for a resumable mutable
        # state.  This matrix deliberately applies equally to an abrupt
        # process death (``preparing``) and a handled lost claim (``failed``):
        # no status-specific fallback may admit a third byte sequence.
        allowed = {
            "preparing": ((old_hash, old_hash),),
            "archive_copying": ((old_hash, old_hash),),
            "archived": ((old_hash, old_hash), (target_hash, old_hash)),
            "canonical_promoted": ((target_hash, old_hash), (target_hash, target_hash)),
            "icloud_promoted": ((target_hash, target_hash),),
            "precommit": ((target_hash, target_hash),),
            "recovery_required": (
                (old_hash, old_hash),
                (target_hash, old_hash),
                (old_hash, target_hash),
                (target_hash, target_hash),
            ),
        }.get(phase, ())
        try:
            state = (sha256_file(canonical), sha256_file(icloud))
        except OSError as error:
            raise ExistingArtifactImportError(
                "failed adoption mutable PDF is unavailable"
            ) from error
        if state not in allowed:
            raise ExistingArtifactImportError("failed adoption mutable PDF state is not resumable")

    def _mark_recovery_required(
        self, import_id: str, claim: ArtifactWriteClaim, error: Exception
    ) -> None:
        claim.assert_owned()
        with self.database.session() as session:
            session.execute(
                update(ExistingArtifactImportModel)
                .where(
                    ExistingArtifactImportModel.id == import_id,
                    ExistingArtifactImportModel.owner == claim.owner,
                    ExistingArtifactImportModel.status != "complete",
                )
                .values(status="preparing", error=str(error), recovery_phase="recovery_required")
            )
            audit = session.get(ExistingArtifactImportModel, import_id)
            if audit is None or audit.owner != claim.owner or audit.status == "complete":
                raise ArtifactWriteClaimLost("existing import audit fence was replaced")

    def _mark_failed(self, import_id: str, owner: str, error: Exception) -> None:
        """Preserve the established non-adoption retry behavior."""
        with self.database.session() as session:
            session.execute(
                update(ExistingArtifactImportModel)
                .where(
                    ExistingArtifactImportModel.id == import_id,
                    ExistingArtifactImportModel.owner == owner,
                    ExistingArtifactImportModel.status != "complete",
                )
                .values(status="failed", error=str(error))
            )

    def _mark_claim_lost(self, import_id: str, owner: str, error: Exception) -> None:
        """Record only our own lost claim; never mutate successor state/files."""
        with self.database.session() as session:
            session.execute(
                update(ExistingArtifactImportModel)
                .where(
                    ExistingArtifactImportModel.id == import_id,
                    ExistingArtifactImportModel.owner == owner,
                    ExistingArtifactImportModel.status != "complete",
                )
                .values(status="failed", error=str(error))
            )

    @staticmethod
    def _adoption_public(audit: ExistingArtifactImportModel) -> dict[str, object] | None:
        if audit.derived_provenance is None:
            return None
        return {
            "phase": audit.recovery_phase,
            "expected_current_pdf_sha256": audit.expected_current_pdf_sha256,
            "previous_pdf_sha256": audit.previous_pdf_sha256,
            "previous_immutable_pdf_path": audit.previous_immutable_pdf_path,
            "target_pdf_sha256": audit.imported_pdf_sha256,
            "imported_immutable_pdf_path": audit.imported_immutable_pdf_path,
            "provenance_kind": audit.derived_provenance,
            "operator": audit.adoption_operator,
            "reason": audit.adoption_reason,
            "confirmed_at": audit.adoption_confirmed_at,
            "revision_id": audit.slide_revision_id,
            "source_sha256": audit.slide_source_sha256,
        }

    def _idempotent(
        self,
        audit: ExistingArtifactImportModel,
        request: ExistingArtifactImportRequest,
        lecture: LectureModel,
        slide: StudyRevisionModel,
        canonical_transcript: Path,
        canonical_outline: Path,
    ) -> ExistingArtifactImportResult:
        if audit.transcript_revision_id is None or audit.outline_id is None:
            raise ExistingArtifactImportError("completed import audit is incomplete")
        with self.database.session() as session:
            revision = session.get(StudyRevisionModel, audit.transcript_revision_id)
            outline = session.get(OutlineOutputModel, audit.outline_id)
            item = (
                session.get(UploadItemModel, revision.upload_item_id)
                if revision is not None
                else None
            )
            valid = (
                audit.status == "complete"
                and audit.lecture_id == request.lecture_id
                and audit.slide_revision_id == request.slides_revision_id
                and audit.slide_source_sha256 == request.slides_source_sha256
                and audit.slide_pdf_sha256 == request.slides_pdf_sha256
                and audit.transcript_filename == request.cleaned_transcript.name
                and audit.outline_filename == request.notebooklm_outline.name
                and audit.transcript_sha256 == request.cleaned_transcript_sha256
                and audit.outline_sha256 == request.notebooklm_outline_sha256
                and (audit.subject, audit.exam_number, audit.lecture_number, audit.topic)
                == (
                    lecture.subject,
                    lecture.exam_number,
                    lecture.lecture_number,
                    lecture.topic,
                )
                and audit.canonical_transcript_path == str(canonical_transcript)
                and audit.canonical_outline_path == str(canonical_outline)
                and revision is not None
                and revision.lecture_id == audit.lecture_id
                and revision.kind == UploadKind.TRANSCRIPTS.value
                and revision.source_sha256 == audit.transcript_sha256
                and revision.derived_sha256 == audit.transcript_sha256
                and revision.immutable_source_path == audit.immutable_transcript_path
                and revision.immutable_derived_path == audit.immutable_transcript_path
                and revision.canonical_source_path == audit.canonical_transcript_path
                and revision.canonical_derived_path == audit.canonical_transcript_path
                and revision.state == "current"
                and revision.current
                and revision.provenance_kind == "imported_cleaned"
                and revision.import_id == audit.id
                and item is not None
                and item.lecture_id == audit.lecture_id
                and item.kind == UploadKind.TRANSCRIPTS.value
                and item.original_filename == audit.transcript_filename
                and item.staged_path == audit.immutable_transcript_path
                and item.sha256 == audit.transcript_sha256
                and item.state == UploadState.COMPLETE.value
                and item.manual_assignment
                and outline is not None
                and outline.lecture_id == audit.lecture_id
                and outline.job_id is None
                and outline.path == audit.canonical_outline_path
                and outline.sha256 == audit.outline_sha256
                and outline.current
                and outline.provenance_kind == "imported_notebooklm"
                and outline.original_filename == audit.outline_filename
                and outline.immutable_path == audit.immutable_outline_path
                and outline.slide_revision_id == slide.id
                and outline.slide_sha256 == audit.slide_pdf_sha256
                and outline.slide_source_sha256 == audit.slide_source_sha256
                and outline.transcript_revision_id == revision.id
                and outline.transcript_sha256 == audit.transcript_sha256
                and outline.import_id == audit.id
            )
            imports_root = expanded_path(self.settings.data_dir) / "artifacts" / "existing-imports"
            study_root = expanded_path(self.settings.study_root)
            if self._adoption_requested(request):
                try:
                    audit_root = imports_root / str(UUID(audit.id))
                except ValueError as error:
                    raise ExistingArtifactImportError(
                        "completed import no longer has exact current artifact identity"
                    ) from error
                valid = valid and (
                    audit.expected_current_pdf_sha256 == request.expected_current_pdf_sha256
                    and audit.previous_pdf_sha256 == request.expected_current_pdf_sha256
                    and audit.imported_pdf_sha256 == request.slides_pdf_sha256
                    and audit.imported_immutable_pdf_path is not None
                    and audit.derived_provenance == "imported_derived"
                    and audit.recovery_phase == "committed"
                    and slide.derived_sha256 == request.slides_pdf_sha256
                    and slide.immutable_derived_path == audit.imported_immutable_pdf_path
                    and slide.provenance_kind == "imported_derived"
                    and slide.import_id == audit.id
                    and audit.immutable_transcript_path == str(audit_root / "cleaned.txt")
                    and audit.immutable_outline_path == str(audit_root / "outline.pdf")
                    and audit.imported_immutable_pdf_path == str(audit_root / "derived-slide.pdf")
                )
            if not valid:
                raise ExistingArtifactImportError(
                    "completed import no longer has exact current artifact identity"
                )
            try:
                paths_match = (
                    hardened_sha256(Path(audit.immutable_transcript_path or ""), imports_root)
                    == audit.transcript_sha256
                    and hardened_sha256(
                        Path(audit.canonical_transcript_path or ""), study_root
                    )
                    == audit.transcript_sha256
                    and hardened_sha256(Path(audit.immutable_outline_path or ""), imports_root)
                    == audit.outline_sha256
                    and hardened_sha256(Path(audit.canonical_outline_path or ""), study_root)
                    == audit.outline_sha256
                )
            except OSError as error:
                raise ExistingArtifactImportError(
                    "completed import no longer has exact current artifact files"
                ) from error
            if not paths_match:
                raise ExistingArtifactImportError(
                    "completed import no longer has exact current artifact files"
                )
            if self._adoption_requested(request):
                try:
                    immutable_root = expanded_path(self.settings.data_dir) / "artifacts" / "v2"
                    if self.settings.icloud_staging_root is None:
                        raise ExistingArtifactImportError("iCloud staging root is not configured")
                    icloud_root = expanded_path(self.settings.icloud_staging_root)
                    adoption_files_match = (
                        hardened_sha256(
                            Path(audit.previous_immutable_pdf_path or ""), immutable_root
                        )
                        == audit.previous_pdf_sha256
                        and hardened_sha256(
                            Path(audit.imported_immutable_pdf_path or ""), imports_root
                        )
                        == audit.imported_pdf_sha256
                        and hardened_sha256(Path(slide.immutable_source_path), immutable_root)
                        == audit.slide_source_sha256
                        and hardened_sha256(Path(slide.canonical_source_path or ""), study_root)
                        == audit.slide_source_sha256
                        and hardened_sha256(Path(slide.canonical_derived_path or ""), study_root)
                        == audit.imported_pdf_sha256
                        and hardened_sha256(Path(slide.icloud_path or ""), icloud_root)
                        == audit.imported_pdf_sha256
                    )
                except (HardenedWriteError, OSError) as error:
                    raise ExistingArtifactImportError(
                        "completed adoption no longer has exact presentation files"
                    ) from error
                if not adoption_files_match:
                    raise ExistingArtifactImportError(
                        "completed adoption no longer has exact presentation files"
                    )
            assert revision is not None
            assert outline is not None
            return ExistingArtifactImportResult(
                audit.id,
                "complete",
                True,
                revision.id,
                outline.id,
                canonical_transcript,
                canonical_outline,
                audit.transcript_sha256,
                audit.outline_sha256,
                audit.lecture_id,
                audit.slide_revision_id,
                audit.slide_source_sha256 or "",
                audit.slide_pdf_sha256 or "",
                Path(audit.immutable_transcript_path or ""),
                Path(audit.immutable_outline_path or ""),
                audit.bundle_sha256,
                audit.attempts,
                audit.subject,
                audit.exam_number,
                audit.lecture_number,
                audit.topic,
                audit.transcript_filename or "",
                audit.outline_filename or "",
                self._adoption_public(audit),
            )

    @staticmethod
    def _copy_exact(
        claim: ArtifactWriteClaim,
        source: Path,
        destination: Path,
        digest: str,
        *,
        trusted_root: Path,
        source_root: Path | None,
    ) -> None:
        claim.assert_owned()
        if not trusted_managed_path(destination, trusted_root, require_regular_file=False):
            raise ExistingArtifactImportError("copy destination is no longer trusted")
        try:
            hardened_verified_copy(
                source, destination, trusted_root, digest, replace=False, source_root=source_root
            )
        except HardenedWriteError as error:
            raise ExistingArtifactImportError("pinned atomic copy could not complete") from error

    @staticmethod
    def _replace_exact(
        claim: ArtifactWriteClaim,
        source: Path,
        destination: Path,
        digest: str,
        *,
        trusted_root: Path,
        source_root: Path | None,
    ) -> None:
        """Atomically replace a mutable presentation copy under the fence."""
        claim.assert_owned()
        if not trusted_managed_path(destination, trusted_root, require_regular_file=True):
            raise ExistingArtifactImportError("replacement destination is no longer trusted")
        try:
            hardened_verified_copy(
                source, destination, trusted_root, digest, replace=True, source_root=source_root
            )
        except HardenedWriteError as error:
            raise ExistingArtifactImportError(
                "pinned atomic replacement could not complete"
            ) from error
