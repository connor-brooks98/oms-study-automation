from dataclasses import dataclass
from pathlib import Path

import pytest

from oms_hub.artifacts import (
    ArtifactGenerationContext,
    ArtifactKind,
    ArtifactRecipeRegistry,
)
from oms_hub.artifacts.recipes import build_recipe_registry
from oms_hub.providers import AuthorityClass


@dataclass(frozen=True)
class LegacyRequest:
    lecture_id: str
    prompt: str


class RecordingGenerator:
    def __init__(self, result: object) -> None:
        self.result = result
        self.calls: list[object] = []

    def __call__(self, request: object) -> object:
        self.calls.append(request)
        return self.result


def build_context() -> ArtifactGenerationContext:
    return ArtifactGenerationContext(
        request=LegacyRequest("lecture-synthetic", "legacy prompt unchanged"),
        prompt_version="legacy-prompt-sha256",
        schema_version="legacy-output-v1",
        storage_path=Path("data/artifacts/synthetic/quiz.json"),
        route="/lectures/42/quiz",
        metadata={"lecture_id": "lecture-synthetic", "private": False},
    )


def test_registry_exposes_stable_recipe_ids() -> None:
    registry = build_recipe_registry()

    assert registry.recipe_ids == (
        "board-question-v1",
        "custom-quiz-current",
        "lecture-outline-current",
        "lecture-quiz-current",
    )


def test_current_lecture_quiz_recipe_delegates_without_prompt_change() -> None:
    generated = {"title": "Synthetic lecture quiz", "questions": []}
    fake_current_generator = RecordingGenerator(generated)
    registry = build_recipe_registry(
        lecture_quiz_generator=fake_current_generator,
    )

    result = registry.get("lecture-quiz-current").generate(build_context())

    assert fake_current_generator.calls == [build_context().request]
    assert result.payload == generated
    assert result.recipe_id == "lecture-quiz-current"
    assert result.recipe_version == "current-v1"
    assert result.prompt_version == "legacy-prompt-sha256"
    assert result.schema_version == "legacy-output-v1"
    assert result.storage_path == Path("data/artifacts/synthetic/quiz.json")
    assert result.route == "/lectures/42/quiz"


@pytest.mark.parametrize(
    ("recipe_id", "generator_keyword", "kind"),
    [
        ("lecture-outline-current", "outline_generator", ArtifactKind.LECTURE_OUTLINE),
        ("custom-quiz-current", "custom_quiz_generator", ArtifactKind.CUSTOM_QUIZ),
        ("board-question-v1", "board_question_generator", ArtifactKind.BOARD_QUESTION),
    ],
)
def test_recipe_adapters_pass_the_legacy_request_unchanged(
    recipe_id: str,
    generator_keyword: str,
    kind: ArtifactKind,
) -> None:
    generated = {"recipe": recipe_id}
    fake_generator = RecordingGenerator(generated)
    registry = build_recipe_registry(**{generator_keyword: fake_generator})

    result = registry.get(recipe_id).generate(build_context())

    assert fake_generator.calls == [build_context().request]
    assert result.payload == generated
    assert result.kind is kind


def test_generated_artifact_is_not_authority() -> None:
    fake_generator = RecordingGenerator({"title": "Synthetic outline"})
    result = build_recipe_registry(
        outline_generator=fake_generator,
    ).get("lecture-outline-current").generate(build_context())

    assert result.authority_class is AuthorityClass.GENERATED_ARTIFACT


def test_registry_rejects_duplicate_recipe_ids() -> None:
    registry = ArtifactRecipeRegistry()
    recipe = build_recipe_registry().get("lecture-outline-current")
    registry.register(recipe)

    with pytest.raises(ValueError, match="lecture-outline-current"):
        registry.register(recipe)
