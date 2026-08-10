import json
import os
import threading
from collections.abc import Callable, Mapping
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


class ArtifactPromotionError(ArtifactError):
    """A promotion failed without leaving destinations in an ambiguous state."""

    def __init__(self, message: str, *, original_error: BaseException) -> None:
        super().__init__(message)
        self.original_error = original_error


class ArtifactCleanupError(ArtifactError):
    """Promotion state is durable, but recovery-file cleanup needs attention."""

    def __init__(
        self,
        message: str,
        *,
        backup_paths: tuple[Path, ...],
        recovery_journal_path: Path | None,
        original_error: BaseException,
    ) -> None:
        super().__init__(message)
        self.backup_paths = backup_paths
        self.recovery_journal_path = recovery_journal_path
        self.original_error = original_error


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
class ArtifactOperatorDiagnostic:
    """Safe, actionable recovery data for the private operator route."""

    code: str
    message: str
    recovery_state: str
    backup_paths: tuple[str, ...]
    recovery_journal_path: str | None

    def as_detail(self) -> dict[str, object]:
        return {
            "code": self.code,
            "message": self.message,
            "recovery_state": self.recovery_state,
            "backup_paths": list(self.backup_paths),
            "recovery_journal_path": self.recovery_journal_path,
        }


def artifact_operator_diagnostic(
    error: ArtifactCleanupError | ArtifactRecoveryError,
) -> ArtifactOperatorDiagnostic:
    if isinstance(error, ArtifactCleanupError):
        code = "artifact_cleanup_required"
        recovery_state = (
            "Promotion committed; retained recovery files require operator cleanup."
        )
    else:
        code = "artifact_recovery_required"
        recovery_state = "Promotion did not commit and rollback remains incomplete."
    return ArtifactOperatorDiagnostic(
        code=code,
        message=str(error),
        recovery_state=recovery_state,
        backup_paths=tuple(str(path) for path in error.backup_paths),
        recovery_journal_path=(
            str(error.recovery_journal_path)
            if error.recovery_journal_path is not None
            else None
        ),
    )


@dataclass(frozen=True, slots=True)
class ResolvedArtifact:
    revision_id: int
    role: ArtifactRole
    path: Path
    media_type: str
    disposition: str
    text: bool


class ArtifactService:
    # Promotion changes canonical files outside SQL. The application has one
    # process owner, so one process-wide lock prevents two approval requests
    # from interleaving their journals, backups, and canonical replacements.
    _promotion_lock = threading.RLock()

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
        with self._promotion_lock:
            return self._approve_serialized(revision_id)

    def _approve_serialized(self, revision_id: int) -> StudyRevision:
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
        except (ArtifactCleanupError, ArtifactRecoveryError):
            # Retained backups mean this revision is still in the recovery
            # state, or the promotion already committed before cleanup failed.
            # Resetting either case could replace the only verified recovery
            # copy or contradict the committed database state.
            raise
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
        planned_backups = {
            destination: (
                ArtifactService._backup_path(destination, revision_id)
                if destination.exists()
                else None
            )
            for _source, destination in pairs
        }
        # This journal must exist before the first filesystem effect.  A
        # process death is not catchable, so restart recovery needs the
        # original destination identity even when no backup was completed.
        try:
            journal_path = ArtifactService._write_recovery_journal(
                pairs,
                planned_backups,
                revision_id,
            )
        except Exception as journal_error:
            raise ArtifactPromotionError(
                "artifact promotion could not start; no destination was changed",
                original_error=journal_error,
            ) from journal_error
        backups: dict[Path, Path | None] = {}
        try:
            for _, destination in pairs:
                if destination.exists():
                    existing_backup = ArtifactService._backup_path(
                        destination,
                        revision_id,
                    )
                    verified_atomic_copy(destination, existing_backup)
                    backups[destination] = existing_backup
                else:
                    backups[destination] = None
            for source, destination in pairs:
                verified_atomic_copy(source, destination)
            commit()
        except Exception as promotion_error:
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
                ) from promotion_error
            try:
                ArtifactService._remove_promotion_artifacts(
                    backups,
                    journal_path,
                )
            except ArtifactCleanupError as cleanup_error:
                raise ArtifactRecoveryError(
                    "artifact promotion failed and original destinations were restored, "
                    "but recovery-file cleanup failed; retain the recovery paths",
                    backup_paths=cleanup_error.backup_paths,
                    recovery_journal_path=cleanup_error.recovery_journal_path,
                    original_error=promotion_error,
                    restore_error=cleanup_error,
                ) from promotion_error
            raise ArtifactPromotionError(
                "artifact promotion failed; original destinations were restored",
                original_error=promotion_error,
            ) from promotion_error
        else:
            ArtifactService._remove_promotion_artifacts(
                backups,
                journal_path,
            )

    @staticmethod
    def _remove_promotion_artifacts(
        backups: Mapping[Path, Path | None],
        journal_path: Path,
    ) -> None:
        try:
            for saved_path in backups.values():
                if saved_path is not None:
                    saved_path.unlink(missing_ok=True)
            journal_path.unlink(missing_ok=True)
        except OSError as cleanup_error:
            raise ArtifactCleanupError(
                "artifact promotion committed or recovered, but recovery-file cleanup failed; "
                "retain the reported recovery paths",
                backup_paths=tuple(
                    path
                    for path in backups.values()
                    if path is not None and path.is_file()
                ),
                recovery_journal_path=(
                    journal_path if journal_path.is_file() else None
                ),
                original_error=cleanup_error,
            ) from cleanup_error

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
                "destination_existed": destination.is_file(),
                "destination_path": str(destination),
                "destination_sha256": (
                    sha256_file(destination) if destination.is_file() else None
                ),
                "source_path": str(source),
                "source_sha256": sha256_file(source),
            }
            for source, destination in pairs
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
        try:
            journal = self._read_recovery_journal(pairs, revision.id)
        except Exception as journal_error:
            raise ArtifactRecoveryError(
                "interrupted artifact promotion has an invalid recovery journal; "
                "retain the journal and backup paths",
                backup_paths=self._existing_backup_paths(pairs, revision.id),
                recovery_journal_path=self._existing_journal_path(
                    pairs,
                    revision.id,
                ),
                original_error=ArtifactConflict(
                    "interrupted artifact promotion requires recovery"
                ),
                restore_error=journal_error,
            ) from journal_error
        if journal is None and not self._existing_backup_paths(pairs, revision.id):
            # ``begin_study_promotion`` commits before the recovery journal is
            # created. A hard process death in that narrow pre-journal window
            # cannot have changed a destination because the journal is the
            # first filesystem effect. Return the row to proposed safely.
            self.repository.reset_study_promotion(revision.id)
            return False
        destinations_match_sources = all(
            destination.is_file()
            and sha256_file(destination) == sha256_file(source)
            for source, destination in pairs
        )
        originals_already_matched = journal is not None and all(
            entry["destination_existed"]
            and entry["destination_sha256"] == entry["source_sha256"]
            for entry in journal.values()
        )
        if destinations_match_sources and not originals_already_matched:
            self.repository.promote_study_revision(revision.id)
            self._remove_promotion_backups(pairs, revision.id)
            return True
        try:
            self._restore_interrupted_promotion(
                pairs,
                revision.id,
                journal=journal,
            )
        except Exception as restore_error:
            raise ArtifactRecoveryError(
                "interrupted artifact promotion could not be verified and "
                "restored; retain the backup paths",
                backup_paths=self._existing_backup_paths(pairs, revision.id),
                recovery_journal_path=self._existing_journal_path(
                    pairs,
                    revision.id,
                ),
                original_error=ArtifactConflict(
                    "interrupted artifact promotion requires recovery"
                ),
                restore_error=restore_error,
            ) from restore_error
        self.repository.reset_study_promotion(revision.id)
        self._remove_promotion_backups(pairs, revision.id)
        return False

    @classmethod
    def _restore_interrupted_promotion(
        cls,
        pairs: list[tuple[Path, Path]],
        revision_id: int,
        *,
        journal: dict[Path, dict[str, object]] | None = None,
    ) -> None:
        """Restore an interrupted promotion without consuming its backups."""
        for source, destination in pairs:
            backup = cls._backup_path(destination, revision_id)
            entry = journal.get(destination) if journal is not None else None
            if entry is not None:
                original_existed = bool(entry["destination_existed"])
                original_sha256 = entry["destination_sha256"]
                if original_existed:
                    if not isinstance(original_sha256, str):
                        raise OSError(
                            "promotion journal lacks the original checksum for: "
                            f"{destination}"
                        )
                    if backup.is_file():
                        if sha256_file(backup) != original_sha256:
                            raise OSError(
                                "artifact backup does not match the promotion journal: "
                                f"{backup}"
                            )
                        verified_atomic_copy(backup, destination)
                    elif (
                        destination.is_file()
                        and sha256_file(destination) == original_sha256
                    ):
                        continue
                    else:
                        raise OSError(
                            "interrupted promotion has no verified original for: "
                            f"{destination}"
                        )
                    if (
                        not destination.is_file()
                        or sha256_file(destination) != original_sha256
                    ):
                        raise OSError(
                            "interrupted promotion restore verification failed: "
                            f"{destination}"
                        )
                    continue
                if not destination.exists():
                    continue
                if (
                    destination.is_file()
                    and sha256_file(destination) == str(entry["source_sha256"])
                ):
                    destination.unlink()
                    if destination.exists():
                        raise OSError(
                            "interrupted promotion destination remained: "
                            f"{destination}"
                        )
                    continue
                raise OSError(
                    "interrupted promotion created an unverified destination: "
                    f"{destination}"
                )
            if backup.is_file():
                expected_sha256 = sha256_file(backup)
                verified_atomic_copy(backup, destination)
                if (
                    not destination.is_file()
                    or sha256_file(destination) != expected_sha256
                ):
                    raise OSError(
                        "interrupted promotion restore verification failed: "
                        f"{destination}"
                    )
            elif destination.is_file() and sha256_file(destination) == sha256_file(source):
                destination.unlink()
                if destination.exists():
                    raise OSError(
                        "interrupted promotion destination remained: "
                        f"{destination}"
                    )
            elif destination.exists():
                raise OSError(
                    "interrupted promotion has no verified backup for: "
                    f"{destination}"
                )

    @classmethod
    def _read_recovery_journal(
        cls,
        pairs: list[tuple[Path, Path]],
        revision_id: int,
    ) -> dict[Path, dict[str, object]] | None:
        path = cls._existing_journal_path(pairs, revision_id)
        if path is None:
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            if payload.get("revision_id") != revision_id:
                raise ValueError("revision identity does not match")
            raw_entries = payload["entries"]
            entries = {
                Path(str(entry["destination_path"])): entry
                for entry in raw_entries
            }
            expected = {destination for _source, destination in pairs}
            if set(entries) != expected:
                raise ValueError("destination set does not match")
            for source, destination in pairs:
                entry = entries[destination]
                if entry.get("source_path") != str(source):
                    raise ValueError("source path does not match")
                if entry.get("source_sha256") != sha256_file(source):
                    raise ValueError("source checksum does not match")
                if not isinstance(entry.get("destination_existed"), bool):
                    raise ValueError("destination state is missing")
            return entries
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
            raise OSError(f"artifact promotion journal is invalid: {path}") from error

    @classmethod
    def _existing_backup_paths(
        cls,
        pairs: list[tuple[Path, Path]],
        revision_id: int,
    ) -> tuple[Path, ...]:
        return tuple(
            backup
            for _source, destination in pairs
            if (backup := cls._backup_path(destination, revision_id)).is_file()
        )

    @classmethod
    def _existing_journal_path(
        cls,
        pairs: list[tuple[Path, Path]],
        revision_id: int,
    ) -> Path | None:
        if not pairs:
            return None
        journal_path = pairs[0][1].with_name(
            f".oms-promotion-{revision_id}.recovery.json"
        )
        return journal_path if journal_path.is_file() else None

    @classmethod
    def _remove_promotion_backups(
        cls,
        pairs: list[tuple[Path, Path]],
        revision_id: int,
    ) -> None:
        backups = {
            destination: cls._backup_path(destination, revision_id)
            for _source, destination in pairs
        }
        journal_path = (
            pairs[0][1].with_name(f".oms-promotion-{revision_id}.recovery.json")
            if pairs
            else None
        )
        if journal_path is None:
            return
        cls._remove_promotion_artifacts(backups, journal_path)

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
