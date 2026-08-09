import json
import os
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from oms_hub.config import Settings
from oms_hub.db import Database
from oms_hub.domain import StepStatus, V2StepName
from oms_hub.files.atomic import sha256_file, verified_atomic_copy
from oms_hub.files.pdf import validate_pdf
from oms_hub.ingestion.domain import StudyRevision, UploadKind
from oms_hub.ingestion.repository import IngestionRepository
from oms_hub.repositories import CatalogRepository
from oms_hub.routing import expanded_path


class ArtifactRole(StrEnum):
    PPTX = "pptx"
    PDF = "pdf"
    RAW = "raw"
    CLEANED = "cleaned"


class ArtifactError(RuntimeError):
    pass


class ArtifactNotFound(ArtifactError):
    pass


class ArtifactConflict(ArtifactError):
    pass


class ArtifactRecoveryError(ArtifactError):
    """Rollback did not complete; retained paths can be used for recovery."""

    def __init__(
        self,
        message: str,
        *,
        backup_paths: tuple[Path, ...],
        recovery_journal_path: Path | None,
        original_error: BaseException,
        restore_error: BaseException,
        journal_error: BaseException | None = None,
    ) -> None:
        super().__init__(message)
        self.backup_paths = backup_paths
        self.recovery_journal_path = recovery_journal_path
        self.original_error = original_error
        self.restore_error = restore_error
        self.journal_error = journal_error


@dataclass(frozen=True, slots=True)
class ResolvedArtifact:
    revision_id: int
    role: ArtifactRole
    path: Path
    media_type: str
    disposition: str
    text: bool


class ArtifactService:
    def __init__(self, database: Database, settings: Settings):
        self.settings = settings
        self.repository = IngestionRepository(database)
        self.catalog = CatalogRepository(database)

    def resolve(
        self,
        revision_id: int,
        role: ArtifactRole,
    ) -> ResolvedArtifact:
        try:
            revision = self.repository.get_study_revision(revision_id)
        except KeyError as error:
            raise ArtifactNotFound("artifact revision was not found") from error
        path, expected_sha256, media_type, disposition, text = (
            self._artifact_details(revision, role)
        )
        root = (
            expanded_path(self.settings.study_root)
            if revision.current
            and role
            in {
                ArtifactRole.PPTX,
                ArtifactRole.PDF,
                ArtifactRole.CLEANED,
            }
            else self._immutable_root()
        )
        self._validate_file(path, expected_sha256, root)
        return ResolvedArtifact(
            revision_id,
            role,
            path,
            media_type,
            disposition,
            text,
        )

    def approve(self, revision_id: int) -> StudyRevision:
        revision = self.repository.get_study_revision(revision_id)
        if revision.state not in {"proposed", "promoting"}:
            raise ArtifactConflict("revision is not awaiting approval")
        pairs = self._approval_pairs(revision)
        if revision.state == "promoting":
            if self._recover_promotion(revision, pairs):
                self._complete_filing_progress(revision)
                return self.repository.get_study_revision(revision.id)
            revision = self.repository.get_study_revision(revision.id)
        self.repository.begin_study_promotion(revision.id)
        try:
            self._promote_with_rollback(
                pairs,
                revision.id,
                lambda: self.repository.promote_study_revision(revision.id),
            )
        except Exception:
            self.repository.reset_study_promotion(revision.id)
            raise
        self._complete_filing_progress(revision)
        return self.repository.get_study_revision(revision.id)

    def keep_current(self, revision_id: int) -> StudyRevision:
        try:
            return self.repository.keep_study_revision(revision_id)
        except ValueError as error:
            raise ArtifactConflict(str(error)) from error

    def _artifact_details(
        self,
        revision: StudyRevision,
        role: ArtifactRole,
    ) -> tuple[Path, str, str, str, bool]:
        if revision.kind is UploadKind.SLIDES:
            if role is ArtifactRole.PPTX:
                path = (
                    revision.canonical_source_path
                    if revision.current
                    else revision.immutable_source_path
                )
                if path is None:
                    raise ArtifactNotFound("PowerPoint artifact is unavailable")
                return (
                    path,
                    revision.source_sha256,
                    (
                        "application/vnd.openxmlformats-officedocument."
                        "presentationml.presentation"
                    ),
                    "attachment",
                    False,
                )
            if role is ArtifactRole.PDF:
                path = (
                    revision.canonical_derived_path
                    if revision.current
                    else revision.immutable_derived_path
                )
                if path is None or revision.derived_sha256 is None:
                    raise ArtifactNotFound("PDF artifact is unavailable")
                return (
                    path,
                    revision.derived_sha256,
                    "application/pdf",
                    "inline",
                    False,
                )
        elif revision.kind is UploadKind.TRANSCRIPTS:
            if role is ArtifactRole.RAW:
                return (
                    revision.immutable_source_path,
                    revision.source_sha256,
                    "text/plain; charset=utf-8",
                    "inline",
                    True,
                )
            if role is ArtifactRole.CLEANED:
                path = (
                    revision.canonical_derived_path
                    if revision.current
                    else revision.immutable_derived_path
                )
                if path is None or revision.derived_sha256 is None:
                    raise ArtifactNotFound(
                        "cleaned transcript is unavailable"
                    )
                return (
                    path,
                    revision.derived_sha256,
                    "text/plain; charset=utf-8",
                    "inline",
                    True,
                )
        raise ArtifactNotFound("artifact role is unavailable for this revision")

    def _approval_pairs(
        self,
        revision: StudyRevision,
    ) -> list[tuple[Path, Path]]:
        immutable_root = self._immutable_root()
        if revision.kind is UploadKind.SLIDES:
            if (
                revision.immutable_derived_path is None
                or revision.canonical_source_path is None
                or revision.canonical_derived_path is None
                or revision.icloud_path is None
                or revision.derived_sha256 is None
            ):
                raise ArtifactConflict("slide replacement is incomplete")
            self._validate_file(
                revision.immutable_source_path,
                revision.source_sha256,
                immutable_root,
            )
            self._validate_file(
                revision.immutable_derived_path,
                revision.derived_sha256,
                immutable_root,
            )
            validate_pdf(revision.immutable_derived_path)
            self._require_within(
                revision.canonical_source_path,
                expanded_path(self.settings.study_root),
            )
            self._require_within(
                revision.canonical_derived_path,
                expanded_path(self.settings.study_root),
            )
            if self.settings.icloud_staging_root is None:
                raise ArtifactConflict("iCloud staging root is not configured")
            self._require_within(
                revision.icloud_path,
                expanded_path(self.settings.icloud_staging_root),
            )
            return [
                (
                    revision.immutable_source_path,
                    revision.canonical_source_path,
                ),
                (
                    revision.immutable_derived_path,
                    revision.canonical_derived_path,
                ),
                (revision.immutable_derived_path, revision.icloud_path),
            ]
        if (
            revision.immutable_derived_path is None
            or revision.canonical_derived_path is None
            or revision.derived_sha256 is None
        ):
            raise ArtifactConflict("transcript replacement is incomplete")
        self._validate_file(
            revision.immutable_derived_path,
            revision.derived_sha256,
            immutable_root,
        )
        try:
            revision.immutable_derived_path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as error:
            raise ArtifactConflict(
                "cleaned transcript is not valid UTF-8"
            ) from error
        self._require_within(
            revision.canonical_derived_path,
            expanded_path(self.settings.study_root),
        )
        return [
            (
                revision.immutable_derived_path,
                revision.canonical_derived_path,
            )
        ]

    def _validate_file(
        self,
        path: Path,
        expected_sha256: str,
        root: Path,
    ) -> None:
        self._require_within(path, root)
        if not path.is_file():
            raise ArtifactNotFound("artifact file is missing")
        if sha256_file(path) != expected_sha256:
            raise ArtifactConflict("artifact checksum does not match its record")

    def _immutable_root(self) -> Path:
        return (
            expanded_path(self.settings.data_dir)
            / "artifacts"
            / "v2"
        )

    @staticmethod
    def _require_within(path: Path, root: Path) -> None:
        if not path.resolve().is_relative_to(root.resolve()):
            raise ArtifactConflict(
                "artifact path is outside its approved storage root"
            )

    @staticmethod
    def _promote_with_rollback(
        pairs: list[tuple[Path, Path]],
        revision_id: int,
        commit: Callable[[], StudyRevision],
    ) -> None:
        backups: dict[Path, Path | None] = {}
        try:
            for _, destination in pairs:
                if destination.exists():
                    existing_backup = destination.with_name(
                        f".{destination.name}.oms-backup-{revision_id}"
                    )
                    verified_atomic_copy(destination, existing_backup)
                    backups[destination] = existing_backup
                else:
                    backups[destination] = None
            for source, destination in pairs:
                verified_atomic_copy(source, destination)
            commit()
        except Exception as promotion_error:
            journal_path: Path | None = None
            journal_error: Exception | None = None
            try:
                journal_path = ArtifactService._write_recovery_journal(
                    pairs,
                    backups,
                    revision_id,
                )
            except Exception as error:
                journal_error = error
            try:
                ArtifactService._restore_backups(backups)
            except Exception as restore_error:
                backup_paths = tuple(
                    path
                    for path in backups.values()
                    if path is not None and path.exists()
                )
                raise ArtifactRecoveryError(
                    "artifact promotion failed and verified rollback could not complete; "
                    "retain the recovery journal and backup paths",
                    backup_paths=backup_paths,
                    recovery_journal_path=journal_path,
                    original_error=promotion_error,
                    restore_error=restore_error,
                    journal_error=journal_error,
                ) from promotion_error
            for saved_path in backups.values():
                if saved_path is not None:
                    saved_path.unlink(missing_ok=True)
            if journal_path is not None:
                journal_path.unlink(missing_ok=True)
            if journal_error is not None:
                raise promotion_error from journal_error
            raise
        else:
            for saved_path in backups.values():
                if saved_path is not None:
                    saved_path.unlink(missing_ok=True)

    @staticmethod
    def _restore_backups(backups: dict[Path, Path | None]) -> None:
        """Restore every destination without consuming a backup until all verify."""
        for destination, saved_path in backups.items():
            if saved_path is not None:
                if not saved_path.is_file():
                    raise OSError(f"artifact backup is missing: {saved_path}")
                expected_sha256 = sha256_file(saved_path)
                verified_atomic_copy(saved_path, destination)
                if not destination.is_file() or sha256_file(destination) != expected_sha256:
                    raise OSError(f"artifact backup restore verification failed: {destination}")
            else:
                destination.unlink(missing_ok=True)
                if destination.exists():
                    raise OSError(f"artifact destination remained after rollback: {destination}")

    @staticmethod
    def _write_recovery_journal(
        pairs: list[tuple[Path, Path]],
        backups: dict[Path, Path | None],
        revision_id: int,
    ) -> Path:
        if not pairs:
            raise OSError("artifact promotion has no destinations")
        journal_path = pairs[0][1].with_name(
            f".oms-promotion-{revision_id}.recovery.json"
        )
        entries = [
            {
                "backup_path": str(backups[destination])
                if backups.get(destination) is not None
                else None,
                "backup_created": destination in backups,
                "destination_path": str(destination),
            }
            for _source, destination in pairs
        ]
        temporary = journal_path.with_name(f".{journal_path.name}.partial")
        journal_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            temporary.write_text(
                json.dumps(
                    {
                        "entries": entries,
                        "revision_id": revision_id,
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                )
                + "\n",
                encoding="utf-8",
            )
            os.replace(temporary, journal_path)
        finally:
            temporary.unlink(missing_ok=True)
        return journal_path

    def _recover_promotion(
        self,
        revision: StudyRevision,
        pairs: list[tuple[Path, Path]],
    ) -> bool:
        if all(
            destination.is_file()
            and sha256_file(destination) == sha256_file(source)
            for source, destination in pairs
        ):
            self.repository.promote_study_revision(revision.id)
            self._remove_promotion_backups(pairs, revision.id)
            return True
        for source, destination in pairs:
            backup = self._backup_path(destination, revision.id)
            if backup.exists():
                os.replace(backup, destination)
            elif (
                destination.is_file()
                and sha256_file(destination) == sha256_file(source)
            ):
                destination.unlink()
        self.repository.reset_study_promotion(revision.id)
        return False

    @classmethod
    def _remove_promotion_backups(
        cls,
        pairs: list[tuple[Path, Path]],
        revision_id: int,
    ) -> None:
        for _, destination in pairs:
            cls._backup_path(destination, revision_id).unlink(
                missing_ok=True
            )

    @staticmethod
    def _backup_path(destination: Path, revision_id: int) -> Path:
        return destination.with_name(
            f".{destination.name}.oms-backup-{revision_id}"
        )

    def _complete_filing_progress(
        self,
        revision: StudyRevision,
    ) -> None:
        steps = (
            (
                V2StepName.SLIDES_FILED,
                V2StepName.ICLOUD_PDF_STAGED,
            )
            if revision.kind is UploadKind.SLIDES
            else (V2StepName.TRANSCRIPT_FILED,)
        )
        for step in steps:
            self.catalog.set_step_status(
                revision.lecture_id,
                step,
                StepStatus.COMPLETE,
                "Replacement approved and filed",
            )
