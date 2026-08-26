"""Immutable metadata and execution contracts for generated Study Hub artifacts."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from types import MappingProxyType
from typing import Protocol

from oms_hub.providers.contracts import AuthorityClass


class ArtifactKind(StrEnum):
    OUTLINE = "outline"
    LECTURE_OUTLINE = "outline"
    LECTURE_QUIZ = "lecture_quiz"
    QUIZ = "lecture_quiz"
    CUSTOM_QUIZ = "custom_quiz"
    BOARD_QUESTION = "board_question"


class ArtifactGenerator(Protocol):
    def __call__(self, request: object) -> object: ...


@dataclass(frozen=True, slots=True)
class ArtifactGenerationContext:
    """The unchanged legacy request plus metadata for a recipe run.

    ``legacy_request`` is accepted as a compatibility spelling so callers can
    make the preservation boundary explicit. Both fields resolve to the same
    object and the injected generator receives that object unchanged.
    """

    request: object | None = None
    legacy_request: object | None = None
    prompt_version: str | None = None
    schema_version: str | None = None
    storage_path: Path | str | None = None
    route: str | None = None
    source_revision_ids: tuple[str, ...] = ()
    evidence_ids: tuple[str, ...] = ()
    metadata: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        request = self.request
        if request is None:
            request = self.legacy_request
        elif self.legacy_request is not None and self.legacy_request != request:
            raise ValueError("request and legacy_request must match")
        if request is None:
            raise ValueError("an artifact generation request is required")
        object.__setattr__(self, "request", request)
        object.__setattr__(self, "legacy_request", request)
        object.__setattr__(self, "source_revision_ids", tuple(self.source_revision_ids))
        object.__setattr__(self, "evidence_ids", tuple(self.evidence_ids))
        object.__setattr__(self, "metadata", MappingProxyType(dict(self.metadata)))


@dataclass(frozen=True, slots=True)
class ArtifactGenerationResult:
    """A generated payload with recipe and legacy-flow provenance."""

    recipe_id: str
    recipe_version: str
    kind: ArtifactKind
    payload: object
    authority_class: AuthorityClass = AuthorityClass.GENERATED_ARTIFACT
    prompt_version: str | None = None
    schema_version: str | None = None
    storage_path: Path | str | None = None
    route: str | None = None
    source_revision_ids: tuple[str, ...] = ()
    evidence_ids: tuple[str, ...] = ()
    metadata: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.authority_class != AuthorityClass.GENERATED_ARTIFACT:
            raise ValueError("generated artifacts cannot be authority sources")
        object.__setattr__(self, "source_revision_ids", tuple(self.source_revision_ids))
        object.__setattr__(self, "evidence_ids", tuple(self.evidence_ids))
        object.__setattr__(self, "metadata", MappingProxyType(dict(self.metadata)))

    @property
    def version(self) -> str:
        return self.recipe_version

    @property
    def output(self) -> object:
        return self.payload


@dataclass(frozen=True, slots=True)
class ArtifactRecipe:
    """Versioned adapter around one existing or new artifact generator."""

    recipe_id: str
    kind: ArtifactKind
    version: str
    generator: Callable[[object], object]
    prompt_version: str | None = None
    schema_version: str | None = None

    def __post_init__(self) -> None:
        if not self.recipe_id.strip():
            raise ValueError("recipe id must not be blank")
        if not self.version.strip():
            raise ValueError("recipe version must not be blank")
        if not callable(self.generator):
            raise TypeError("recipe generator must be callable")
        if not isinstance(self.kind, ArtifactKind):
            object.__setattr__(self, "kind", ArtifactKind(self.kind))

    @property
    def id(self) -> str:
        return self.recipe_id

    def generate(self, context: ArtifactGenerationContext) -> ArtifactGenerationResult:
        request = context.request
        if request is None:  # guarded by ArtifactGenerationContext.__post_init__
            raise ValueError("an artifact generation request is required")
        payload = self.generator(request)
        return ArtifactGenerationResult(
            recipe_id=self.recipe_id,
            recipe_version=self.version,
            kind=self.kind,
            payload=payload,
            prompt_version=self.prompt_version or context.prompt_version,
            schema_version=self.schema_version or context.schema_version,
            storage_path=context.storage_path,
            route=context.route,
            source_revision_ids=context.source_revision_ids,
            evidence_ids=context.evidence_ids,
            metadata=context.metadata,
        )


class ArtifactRecipeRegistry:
    """Instance-local registry of immutable recipe adapters."""

    def __init__(self, recipes: tuple[ArtifactRecipe, ...] | list[ArtifactRecipe] = ()) -> None:
        self._recipes: dict[str, ArtifactRecipe] = {}
        for recipe in recipes:
            self.register(recipe)

    def register(self, recipe: ArtifactRecipe) -> None:
        if recipe.recipe_id in self._recipes:
            raise ValueError(f"artifact recipe already registered: {recipe.recipe_id}")
        self._recipes[recipe.recipe_id] = recipe

    def get(self, recipe_id: str) -> ArtifactRecipe:
        try:
            return self._recipes[recipe_id]
        except KeyError:
            registered = ", ".join(self.recipe_ids) or "none"
            raise KeyError(
                f"artifact recipe not registered: {recipe_id}; registered: {registered}"
            ) from None

    def generate(
        self,
        recipe_id: str,
        context: ArtifactGenerationContext,
    ) -> ArtifactGenerationResult:
        return self.get(recipe_id).generate(context)

    @property
    def recipe_ids(self) -> tuple[str, ...]:
        return tuple(sorted(self._recipes))

    @property
    def recipes(self) -> Mapping[str, ArtifactRecipe]:
        return MappingProxyType(self._recipes)


ArtifactResult = ArtifactGenerationResult
GeneratedArtifact = ArtifactGenerationResult
