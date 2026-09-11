from __future__ import annotations

import json
from dataclasses import dataclass, replace

from oms_hub.question_bank.contracts import ImportPreview, QuestionKey
from oms_hub.question_bank.repository import BankRepository
from oms_hub.study_generation.domain import QuizQuestion
from oms_hub.study_generation.native_quiz import parse_native_quiz
from oms_hub.study_generation.practice_domain import (
    AnswerProvenance,
    QuestionDraft,
    QuestionSourceRef,
)
from oms_hub.study_generation.studio_repository import StudioRepository


@dataclass(frozen=True)
class BankReviewRef:
    run_id: str
    review_url: str
    original_keys: tuple[tuple[str, QuestionKey], ...]


def reviewable_drafts(preview: ImportPreview, import_id: str) -> dict[int, QuestionDraft]:
    drafts = {}
    duplicates = {issue.row for issue in preview.issues if issue.code == "duplicate_row"}
    for number, row in enumerate(preview.envelope.rows, 1):
        body = row.question
        if body is None or "kind" in body or number in duplicates:
            continue
        index = body.get("correct_index")
        choices = body.get("choices")
        answer = (
            index
            if (type(index) is int and isinstance(choices, list) and 0 <= index < len(choices))
            else None
        )
        rationale = body.get("rationale")
        rationale = rationale.strip() if isinstance(rationale, str) and rationale.strip() else None
        # Validate structure with placeholders, then discard them; missing answers remain None.
        structural = body | {
            "correct_index": answer if answer is not None else 0,
            "rationale": rationale or "Pending source rationale",
        }
        try:
            question = parse_native_quiz(
                json.dumps({"title": "Imported question", "questions": [structural]})
            ).questions[0]
        except ValueError:
            continue
        if not isinstance(question, QuizQuestion):
            continue
        drafts[number] = QuestionDraft(
            question_id=f"q{number}",
            original_identifier=f"{preview.envelope.source}/{preview.envelope.product}/{row.question_id}",
            stem=question.stem,
            choices=tuple(choice.text for choice in question.choices),
            correct_index=answer,
            rationale=rationale,
            image_ref=question.image_ref,
            source_refs=(QuestionSourceRef(import_id, f"row:{number}", f"Import row {number}"),),
            answer_provenance=AnswerProvenance.PROVIDED_BY_SOURCE if answer is not None else None,
            extraction_confidence=1.0,
            diagnostics=(),
            verification_required=True,
            verified_at=None,
        )
    return drafts


def stage_native_review(
    bank: BankRepository,
    studio: StudioRepository,
    *,
    import_id: str,
    learner_id: str,
    subject: str,
    exam_number: int,
    label: str,
    row_numbers: tuple[int, ...] | None = None,
) -> BankReviewRef:
    preview, _ = bank.get_import(import_id=import_id, learner_id=learner_id)
    if preview.envelope.provenance.kind != "authorized_question_export" or not any(
        row.question is not None for row in preview.envelope.rows
    ):
        raise ValueError("Import has no authorized question content")
    available = reviewable_drafts(preview, import_id)
    selected = tuple(available) if row_numbers is None else row_numbers
    if (
        not selected
        or len(selected) > 500
        or len(set(selected)) != len(selected)
        or any(type(number) is not int or number not in available for number in selected)
    ):
        raise ValueError("Select 1-500 distinct reviewable original rows")
    selected = tuple(sorted(selected))
    drafts = tuple(
        replace(available[number], question_id=f"q{position}")
        for position, number in enumerate(selected, 1)
    )
    run = studio.create_bank_import_review(
        bank_import_id=import_id,
        learner_id=learner_id,
        subject=subject,
        exam_number=exam_number,
        label=label,
        drafts=drafts,
    )
    keys = tuple(
        (
            draft.question_id,
            QuestionKey(
                source=preview.envelope.source,
                product=preview.envelope.product,
                question_id=preview.envelope.rows[number - 1].question_id,
            ),
        )
        for draft, number in zip(drafts, selected, strict=True)
    )
    return BankReviewRef(run.id, f"/studio/runs/{run.id}/review", keys)
