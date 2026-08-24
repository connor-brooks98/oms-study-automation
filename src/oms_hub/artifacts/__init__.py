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
    from typing import Any

    ArtifactCleanupError: Any
    ArtifactConflict: Any
    ArtifactError: Any
    ArtifactNotFound: Any
    ArtifactOperatorDiagnostic: Any
    ArtifactPromotionError: Any
    ArtifactRecoveryError: Any
    ArtifactRecoveryState: Any
    ArtifactRole: Any
    ArtifactService: Any
    ResolvedArtifact: Any

    def artifact_operator_diagnostic(error: Any) -> Any: ...

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
