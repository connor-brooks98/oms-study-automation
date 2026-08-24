"""Artifact services plus the grounded-learning recipe contracts.

The repository already has ``oms_hub/artifacts.py`` for file promotion. A
package is required for the recipe submodules, so its implementation is
loaded into this package namespace to preserve every existing import and
monkeypatch target while the legacy module remains untouched.
"""

from __future__ import annotations

from pathlib import Path as _Path
from typing import TYPE_CHECKING

from oms_hub.artifacts.models import (
    ArtifactGenerationContext,
    ArtifactGenerationResult,
    ArtifactGenerator,
    ArtifactKind,
    ArtifactRecipe,
    ArtifactRecipeRegistry,
    ArtifactResult,
    GeneratedArtifact,
)
from oms_hub.artifacts.recipes import build_default_registry, build_recipe_registry

if TYPE_CHECKING:
    from collections.abc import Callable
    from dataclasses import dataclass
    from enum import StrEnum
    from pathlib import Path

    from oms_hub.config import Settings
    from oms_hub.db import Database
    from oms_hub.ingestion.domain import StudyRevision
    from oms_hub.ingestion.repository import IngestionRepository
    from oms_hub.repositories import CatalogRepository

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

    class ArtifactRecoveryState(StrEnum):
        COMMITTED_CLEANUP_REQUIRED = "committed_cleanup_required"
        ROLLED_BACK_CLEANUP_REQUIRED = "rolled_back_cleanup_required"
        ROLLBACK_INCOMPLETE = "rollback_incomplete"

    class ArtifactPromotionError(ArtifactError):
        original_error: BaseException

        def __init__(self, message: str, *, original_error: BaseException) -> None: ...

    class ArtifactCleanupError(ArtifactError):
        backup_paths: tuple[Path, ...]
        recovery_journal_path: Path | None
        original_error: BaseException
        recovery_state: ArtifactRecoveryState

        def __init__(
            self,
            message: str,
            *,
            backup_paths: tuple[Path, ...],
            recovery_journal_path: Path | None,
            original_error: BaseException,
            recovery_state: ArtifactRecoveryState,
        ) -> None: ...

    class ArtifactRecoveryError(ArtifactError):
        backup_paths: tuple[Path, ...]
        recovery_journal_path: Path | None
        original_error: BaseException
        restore_error: BaseException
        journal_error: BaseException | None
        recovery_state: ArtifactRecoveryState

        def __init__(
            self,
            message: str,
            *,
            backup_paths: tuple[Path, ...],
            recovery_journal_path: Path | None,
            original_error: BaseException,
            restore_error: BaseException,
            journal_error: BaseException | None = None,
            recovery_state: ArtifactRecoveryState,
        ) -> None: ...

    @dataclass(frozen=True, slots=True)
    class ArtifactOperatorDiagnostic:
        code: str
        message: str
        recovery_state: str
        backup_paths: tuple[str, ...]
        recovery_journal_path: str | None

        def as_detail(self) -> dict[str, object]: ...

    def artifact_operator_diagnostic(
        error: ArtifactCleanupError | ArtifactRecoveryError,
    ) -> ArtifactOperatorDiagnostic: ...

    @dataclass(frozen=True, slots=True)
    class ResolvedArtifact:
        revision_id: int
        role: ArtifactRole
        path: Path
        media_type: str
        disposition: str
        text: bool

    class ArtifactService:
        settings: Settings
        repository: IngestionRepository
        catalog: CatalogRepository

        def __init__(self, database: Database, settings: Settings) -> None: ...

        def resolve(self, revision_id: int, role: ArtifactRole) -> ResolvedArtifact: ...

        def approve(self, revision_id: int) -> StudyRevision: ...

        def keep_current(self, revision_id: int) -> StudyRevision: ...

        @staticmethod
        def _backup_path(destination: Path, revision_id: int) -> Path: ...

        @staticmethod
        def _write_recovery_journal(
            pairs: list[tuple[Path, Path]],
            backups: dict[Path, Path | None],
            revision_id: int,
        ) -> Path: ...

        @staticmethod
        def _promote_with_rollback(
            pairs: list[tuple[Path, Path]],
            revision_id: int,
            commit: Callable[[], StudyRevision],
        ) -> None: ...

        def _recover_promotion(
            self,
            revision: StudyRevision,
            pairs: list[tuple[Path, Path]],
        ) -> bool: ...

__all__ = (
    "ArtifactGenerationContext",
    "ArtifactGenerationResult",
    "ArtifactGenerator",
    "ArtifactKind",
    "ArtifactRecipe",
    "ArtifactRecipeRegistry",
    "ArtifactResult",
    "GeneratedArtifact",
    "build_default_registry",
    "build_recipe_registry",
    "ArtifactCleanupError",
    "ArtifactConflict",
    "ArtifactError",
    "ArtifactNotFound",
    "ArtifactOperatorDiagnostic",
    "ArtifactPromotionError",
    "ArtifactRecoveryError",
    "ArtifactRecoveryState",
    "ArtifactRole",
    "ArtifactService",
    "ResolvedArtifact",
    "artifact_operator_diagnostic",
)


_legacy_artifacts_path = _Path(__file__).resolve().parent.parent / "artifacts.py"
exec(  # noqa: S102 - fixed, repository-owned compatibility source
    compile(
        _legacy_artifacts_path.read_text(encoding="utf-8"),
        str(_legacy_artifacts_path),
        "exec",
    ),
    globals(),
)
