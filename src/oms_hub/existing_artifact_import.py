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

from oms_hub.anki.sources import NotebookSummaryParser
from oms_hub.artifact_writes import (
    ArtifactWriteClaim,
    ArtifactWriteCoordinator,
)
from oms_hub.config import Settings
from oms_hub.db import Database
from oms_hub.domain import LectureKey
from oms_hub.files.atomic import sha256_file, verified_atomic_copy
from oms_hub.files.pdf import validate_pdf
from oms_hub.ingestion.domain import UploadKind, UploadState
from oms_hub.models import (
    ExistingArtifactImportModel,
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


A0_PPTX_SHA256 = "b1c7abc3fb5d86476a3477d397e679ec42e61cff982fcec9dcb55a9d0a9c5469"
A0_PDF_SHA256 = "8bb427c3265f3a97997fd870f42794d59bd4850f963ccf292f3f9160ea9e0d38"
A0_CLEANED_TRANSCRIPT_SHA256 = (
    "8d9b6d482c80401fb45c3bc76e70783b0ed49b7134d39ccfeb7da0451cd7f6a9"
)
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

    def as_dict(self) -> dict[str, object]:
        return {
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
        self.checkpoint("post-initial-validation")
        bundle = self._bundle_identity(request)
        # Reject an absent or stale catalog pin before attempting to create the
        # foreign-keyed durable writer fence.
        self._validate_lecture_and_slide(request)
        with self.writes.claim(request.lecture_id, "existing-artifact-import") as claim:
            # Re-read after acquiring the process-wide write exclusion.
            transcript, outline = self._validate_inputs(request)
            self.checkpoint("post-lock-revalidation")
            lecture, slide = self._validate_lecture_and_slide(request)
            lecture_key = LectureKey(
                lecture.subject,
                lecture.exam_number,
                lecture.lecture_number,
                lecture.topic,
            )
            canonical_transcript, canonical_outline = self._destinations(lecture_key)
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
            import_id = existing.id
            immutable_transcript = Path(existing.immutable_transcript_path or "")
            immutable_outline = Path(existing.immutable_outline_path or "")
            created: list[Path] = []
            try:
                self._assert_no_current_artifacts(request.lecture_id)
                # The inputs are caller-controlled paths.  Check their pins
                # again after the durable audit exists but before any copy.
                self._validate_inputs(request)
                for source, destination, digest in (
                    (
                        request.cleaned_transcript,
                        immutable_transcript,
                        request.cleaned_transcript_sha256,
                    ),
                    (
                        request.notebooklm_outline,
                        immutable_outline,
                        request.notebooklm_outline_sha256,
                    ),
                    (immutable_transcript, canonical_transcript, request.cleaned_transcript_sha256),
                    (immutable_outline, canonical_outline, request.notebooklm_outline_sha256),
                ):
                    if not destination.exists():
                        created.append(destination)
                    claim.assert_owned()
                    self.checkpoint("before-copy")
                    self._copy_exact(claim, source, destination, digest)
                    claim.assert_owned()
                    self.checkpoint("after-copy")
                # Pin again immediately before the durable current-record commit.
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
                    )
            except Exception as error:
                # A lost owner must not remove or overwrite successor state,
                # but it may record its own failed attempt only while the
                # durable audit still names that exact owner.
                self._mark_failed(import_id, claim.owner, error)
                for path in reversed(created):
                    claim.assert_owned()
                    self.checkpoint("before-cleanup")
                    path.unlink(missing_ok=True)
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

    def _destinations(self, lecture: LectureKey) -> tuple[Path, Path]:
        transcript, outline = self.destination_resolver(self.settings, lecture)
        root = expanded_path(self.settings.study_root)
        self._require_managed_destination(transcript, root, "transcript")
        self._require_managed_destination(outline, root, "NotebookLM outline")
        return transcript, outline

    @staticmethod
    def _require_managed_destination(path: Path, root: Path, label: str) -> None:
        try:
            contained = path.resolve().is_relative_to(root.resolve())
        except OSError as error:
            raise ExistingArtifactImportError(
                f"{label} destination could not be resolved within its managed root"
            ) from error
        if not contained:
            raise ExistingArtifactImportError(
                f"{label} destination escapes its configured managed root"
            )

    @staticmethod
    def _safe_file(path: Path, suffix: str, label: str) -> Path:
        if (
            not path.is_absolute()
            or path.is_symlink()
            or not path.is_file()
            or path.suffix.casefold() != suffix
        ):
            raise ExistingArtifactImportError(
                f"{label} must be an absolute regular non-symlink {suffix} file"
            )
        return path

    def _validate_lecture_and_slide(
        self, request: ExistingArtifactImportRequest
    ) -> tuple[LectureModel, StudyRevisionModel]:
        lecture = self.catalog.get_lecture(request.lecture_id)
        if lecture is None:
            raise ExistingArtifactImportError("catalog lecture does not exist")
        with self.database.session() as session:
            slide = session.get(StudyRevisionModel, request.slides_revision_id)
            if (
                slide is None
                or slide.lecture_id != request.lecture_id
                or slide.kind != UploadKind.SLIDES.value
                or not slide.current
                or slide.source_sha256 != request.slides_source_sha256
                or slide.derived_sha256 != request.slides_pdf_sha256
            ):
                raise ExistingArtifactImportError(
                    "slides revision is not the exact current lecture PPTX/PDF revision/hashes"
                )
            immutable_root = expanded_path(self.settings.data_dir) / "artifacts" / "v2"
            managed_root = expanded_path(self.settings.study_root)
            for path, digest, root, label in (
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
                    request.slides_pdf_sha256,
                    immutable_root,
                    "PDF immutable",
                ),
                (
                    slide.canonical_derived_path,
                    request.slides_pdf_sha256,
                    managed_root,
                    "PDF canonical",
                ),
            ):
                if path is None:
                    raise ExistingArtifactImportError(f"slides revision {label} path is missing")
                self._validate_file(Path(path), digest, root, label)
        return lecture, slide

    def _validate_file(self, path: Path, digest: str, root: Path, label: str) -> None:
        self._require_managed_destination(path, root, label)
        if not path.is_file() or sha256_file(path) != digest:
            raise ExistingArtifactImportError(f"slides revision {label} path/hash is not exact")

    def _assert_no_current_artifacts(
        self, lecture_id: int, session: Session | None = None
    ) -> None:
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
                        ExistingArtifactImportModel.slide_revision_id
                        == request.slides_revision_id,
                        ExistingArtifactImportModel.slide_source_sha256
                        == request.slides_source_sha256,
                        ExistingArtifactImportModel.slide_pdf_sha256
                        == request.slides_pdf_sha256,
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
                import_id = str(uuid4())
                immutable_root = (
                    expanded_path(self.settings.data_dir)
                    / "artifacts"
                    / "existing-imports"
                    / import_id
                )
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
                if current.status == "failed":
                    current.status = "preparing"
                    current.attempts += 1
                    current.error = None
                    current.owner = owner
                else:
                    current.attempts += 1
                    current.owner = owner
            session.flush()
            session.expunge(current)
            return current

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
            or audit.status not in {"failed", "preparing"}
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
        if managed_root.is_symlink():
            raise ExistingArtifactImportError("existing-import managed root is a symlink")
        immutable_root = managed_root / audit.id
        expected = (
            (audit.immutable_transcript_path, immutable_root / "cleaned.txt", "transcript"),
            (audit.immutable_outline_path, immutable_root / "outline.pdf", "outline"),
        )
        for stored, destination, label in expected:
            if stored is None or Path(stored) != destination:
                raise ExistingArtifactImportError(
                    f"failed import audit immutable {label} destination is invalid"
                )
            self._require_managed_destination(destination, managed_root, label)
            if destination.is_symlink() or immutable_root.is_symlink():
                raise ExistingArtifactImportError(
                    f"failed import audit immutable {label} destination is a symlink"
                )
        for destination, label in (
            (canonical_transcript, "canonical transcript"),
            (canonical_outline, "canonical outline"),
        ):
            if destination.is_symlink():
                raise ExistingArtifactImportError(
                    f"failed import audit {label} destination is a symlink"
                )

    def _mark_failed(self, import_id: str, owner: str, error: Exception) -> None:
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
            if not valid:
                raise ExistingArtifactImportError(
                    "completed import no longer has exact current artifact identity"
                )
            try:
                paths_match = (
                    sha256_file(Path(audit.immutable_transcript_path or ""))
                    == audit.transcript_sha256
                    and sha256_file(Path(audit.canonical_transcript_path or ""))
                    == audit.transcript_sha256
                    and sha256_file(Path(audit.immutable_outline_path or ""))
                    == audit.outline_sha256
                    and sha256_file(Path(audit.canonical_outline_path or ""))
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
            )

    @staticmethod
    def _copy_exact(
        claim: ArtifactWriteClaim, source: Path, destination: Path, digest: str
    ) -> None:
        claim.assert_owned()
        if destination.exists():
            if sha256_file(destination) != digest:
                raise ExistingArtifactImportError(
                    f"destination already contains different bytes: {destination}"
                )
            return
        if verified_atomic_copy(source, destination) != digest:
            raise ExistingArtifactImportError("atomic copy checksum mismatch")
