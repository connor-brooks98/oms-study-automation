"""Connect a durable text-only plan to bounded, selected-image generation batches."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from oms_hub.llm.codex_session import CodexSessionClient, SessionLifecycle
from oms_hub.study_generation.quiz_evidence import compact_evidence
from oms_hub.study_generation.quiz_plan import PlannedQuestion, plan_lecture_quiz

if TYPE_CHECKING:
    from oms_hub.document_processing.domain import ParsedAsset
    from oms_hub.study_generation.gpt_lecture import GeneratedLectureQuiz, LectureInputs


def prepare_selected_batches(
    client: CodexSessionClient, request_id: str, model: str, inputs: LectureInputs,
    manifest: dict[str, object], *, root: Path, cancelled: Callable[[], bool],
    on_lifecycle: Callable[[SessionLifecycle], None], resume: bool,
) -> list[tuple[LectureInputs, str, tuple[ParsedAsset, ...]]]:
    from oms_hub.study_generation.gpt_lecture import (
        MAX_BATCH_IMAGES,
        MAX_SOURCE_CHARACTERS,
        LectureGenerationError,
        _canonical,
        quiz_instruction_documents,
    )

    evidence = cast(dict[str, Any], compact_evidence(inputs))
    evidence["manifest_sha256"] = manifest["sha256"]
    plan = plan_lecture_quiz(
        client, request_id + ":plan", model, evidence, root=root / "selection",
        cancelled=cancelled, on_lifecycle=on_lifecycle, resume=resume,
    )
    inventory = {(image["source_id"], image["asset_key"]): image
                 for image in evidence["images"]}
    assets = {(document.source_id, asset.key): asset
              for document in quiz_instruction_documents(inputs) for asset in document.assets}
    # Sparse-image pages must be inspected even when the text-only planner did not
    # choose them for a question. Their pixels are never inferred from the inventory.
    previews = {key for key, image in inventory.items() if image.get("needs_preview")}

    def selected(questions: list[PlannedQuestion]) -> set[tuple[str, str]]:
        return previews | {(q.image.source_id, q.image.asset_key)
                           for q in questions if q.image is not None}

    def limit() -> LectureGenerationError:
        return LectureGenerationError("context_limit", counts={
            "required_preview_images": len(previews), "planned_questions": len(plan.questions),
        })

    questions = plan.questions
    packed: dict[int, tuple[tuple[LectureInputs, str, tuple[ParsedAsset, ...]], ...]] = {
        len(questions): (),
    }
    for start in range(len(questions) - 3, -1, -1):
        # Consider all legal boundaries together: question plans and images vary
        # in size, so pre-grouping can reject a lecture that fits uneven batches.
        for end in range(min(start + 25, len(questions)), start + 2, -1):
            if end not in packed:
                continue
            group = questions[start:end]
            image_keys = sorted(selected(group))
            if len(image_keys) > MAX_BATCH_IMAGES:
                continue
            objective_ids = {key for q in group for key in q.objective_ids}
            batch = replace(inputs, objectives=tuple(
                pair for pair in inputs.objectives if pair[0] in objective_ids
            ))
            source = _canonical({
                **evidence,
                "objectives": [{"id": key, "text": text} for key, text in batch.objectives],
                "question_plan": [q.model_dump(mode="json") for q in group],
                "images": [{**inventory[key], "input_index": index}
                           for index, key in enumerate(image_keys)],
            })
            if len(source) > MAX_SOURCE_CHARACTERS:
                continue
            packed[start] = ((batch, source, tuple(assets[key] for key in image_keys)),
                             *packed[end])
            break

    if 0 not in packed:
        raise limit()
    return list(packed[0])


def validate_planned_result(quiz: GeneratedLectureQuiz, source: str) -> None:
    """Reject extra questions or image references whose pixels were never supplied."""
    evidence = json.loads(source)
    planned = {question["id"]: question for question in evidence["question_plan"]}
    if len(quiz.questions) != len(planned) or {q.id for q in quiz.questions} != set(planned):
        raise ValueError("generated questions do not match the approved question plan")
    supplied = {(image["source_id"], image["asset_key"]) for image in evidence["images"]}
    for question in quiz.questions:
        if set(question.objective_ids) != set(planned[question.id]["objective_ids"]):
            raise ValueError("generated question changed its planned objective coverage")
        if question.image and (question.image.source_id, question.image.asset_key) not in supplied:
            raise ValueError("generated question uses an image that was not inspected")
