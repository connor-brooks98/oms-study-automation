"""Stable adapters for current Study Hub generators."""

from __future__ import annotations

from collections.abc import Callable

from oms_hub.artifacts.models import (
    ArtifactKind,
    ArtifactRecipe,
    ArtifactRecipeRegistry,
)


def _unconfigured(recipe_id: str) -> Callable[[object], object]:
    def generate(_request: object) -> object:
        raise RuntimeError(f"artifact recipe requires an injected generator: {recipe_id}")

    return generate


def build_recipe_registry(
    *,
    outline_generator: Callable[[object], object] | None = None,
    lecture_quiz_generator: Callable[[object], object] | None = None,
    custom_quiz_generator: Callable[[object], object] | None = None,
    board_question_generator: Callable[[object], object] | None = None,
) -> ArtifactRecipeRegistry:
    """Build the four stable recipes without changing legacy generator code."""

    return ArtifactRecipeRegistry(
        [
            ArtifactRecipe(
                "lecture-outline-current",
                ArtifactKind.LECTURE_OUTLINE,
                "current-v1",
                outline_generator or _unconfigured("lecture-outline-current"),
            ),
            ArtifactRecipe(
                "lecture-quiz-current",
                ArtifactKind.LECTURE_QUIZ,
                "current-v1",
                lecture_quiz_generator or _unconfigured("lecture-quiz-current"),
            ),
            ArtifactRecipe(
                "custom-quiz-current",
                ArtifactKind.CUSTOM_QUIZ,
                "current-v1",
                custom_quiz_generator or _unconfigured("custom-quiz-current"),
            ),
            ArtifactRecipe(
                "board-question-v1",
                ArtifactKind.BOARD_QUESTION,
                "v1",
                board_question_generator or _unconfigured("board-question-v1"),
            ),
        ]
    )


build_default_registry = build_recipe_registry
