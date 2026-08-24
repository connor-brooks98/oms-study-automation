from dataclasses import dataclass
from pathlib import Path
from typing import Literal, assert_type

import pytest

from oms_hub.artifacts import (
    ArtifactGenerationContext,
    ArtifactKind,
    ArtifactRecipeRegistry,
    ArtifactRole,
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


class FalsyRecordingGenerator(RecordingGenerator):
    def __bool__(self) -> bool:
        return False


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


def test_recipe_passes_the_exact_context_request_object() -> None:
    request = object()
    fake_current_generator = RecordingGenerator({"request": "preserved"})
    context = ArtifactGenerationContext(request=request)

    build_recipe_registry(
        lecture_quiz_generator=fake_current_generator,
    ).get("lecture-quiz-current").generate(context)

    assert fake_current_generator.calls[0] is request


def test_recipe_accepts_a_valid_falsy_generator() -> None:
    generated = {"title": "Falsy callable output"}
    fake_current_generator = FalsyRecordingGenerator(generated)
    context = build_context()

    result = build_recipe_registry(
        lecture_quiz_generator=fake_current_generator,
    ).get("lecture-quiz-current").generate(context)

    assert fake_current_generator.calls[0] is context.request
    assert result.payload == generated


def test_legacy_artifact_exports_retain_static_types() -> None:
    assert_type(ArtifactRole.PDF, Literal[ArtifactRole.PDF])


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
