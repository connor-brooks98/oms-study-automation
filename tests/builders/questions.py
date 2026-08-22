"""Deterministic test payload builders for future question contracts."""

from __future__ import annotations

from collections.abc import Iterable
from typing import TypedDict


class QuestionOptionPayload(TypedDict):
    option_id: str
    text: str
    rationale: str
    evidence_ids: list[str]


class QuestionClaimPayload(TypedDict):
    claim_id: str
    role: str
    text: str
    evidence_ids: list[str]


class BoardQuestionDraftPayload(TypedDict):
    stem: str
    lead_in: str
    options: list[QuestionOptionPayload]
    correct_option_id: str
    objective_ids: list[str]
    difficulty: int
    blueprint_tags: list[str]
    claims: list[QuestionClaimPayload]


_OPTION_BANK: tuple[tuple[str, str, str], ...] = (
    (
        "A",
        "Factor VII deficiency",
        "This does not match the synthetic course association with factor VIII.",
    ),
    (
        "B",
        "Factor VIII deficiency",
        "The synthetic course source identifies factor VIII deficiency as hemophilia A.",
    ),
    (
        "C",
        "Factor IX deficiency",
        "This is not the factor identified for hemophilia A in the synthetic course source.",
    ),
    (
        "D",
        "Factor XI deficiency",
        "This is not the factor identified for hemophilia A in the synthetic course source.",
    ),
    (
        "E",
        "Factor XIII deficiency",
        "This is not the factor identified for hemophilia A in the synthetic course source.",
    ),
)


def build_board_question_draft(
    *,
    option_count: int = 4,
    correct_option_id: str = "B",
    duplicate_option_id: bool = False,
    objective_ids: Iterable[str] = ("obj_fixture_hemophilia_a",),
    difficulty: int = 3,
    blueprint_tags: Iterable[str] = ("clinical_presentation:bleeding_disorder",),
    evidence_ids: Iterable[str] = (
        "ev_fixture_course_l13_hemophilia_a",
        "ev_fixture_course_l13_ptt",
    ),
) -> BoardQuestionDraftPayload:
    if option_count < 0:
        raise ValueError("option_count cannot be negative")
    if option_count > len(_OPTION_BANK):
        raise ValueError(
            f"option_count {option_count} exceeds supported maximum {len(_OPTION_BANK)}"
        )
    if duplicate_option_id and option_count < 2:
        raise ValueError("duplicate_option_id requires at least two options")

    copied_evidence_ids = list(evidence_ids)
    options: list[QuestionOptionPayload] = [
        {
            "option_id": option_id,
            "text": text,
            "rationale": rationale,
            "evidence_ids": list(copied_evidence_ids),
        }
        for option_id, text, rationale in _OPTION_BANK[:option_count]
    ]
    if duplicate_option_id:
        options[-1]["option_id"] = options[0]["option_id"]

    claims: list[QuestionClaimPayload] = [
        {
            "claim_id": "claim_fixture_stem_labs",
            "role": "stem",
            "text": "Hemophilia A can present with a normal PT and prolonged PTT.",
            "evidence_ids": list(copied_evidence_ids),
        },
        {
            "claim_id": "claim_fixture_correct_support",
            "role": "correct_support",
            "text": "Hemophilia A is caused by factor VIII deficiency.",
            "evidence_ids": list(copied_evidence_ids),
        },
        {
            "claim_id": "claim_fixture_teaching_point",
            "role": "teaching_point",
            "text": "The intrinsic-pathway abnormality accounts for the prolonged PTT.",
            "evidence_ids": list(copied_evidence_ids),
        },
    ]
    return {
        "stem": (
            "A child has recurrent hemarthroses. Laboratory testing shows a normal PT and a "
            "prolonged PTT."
        ),
        "lead_in": "Which coagulation-factor deficiency best explains these findings?",
        "options": options,
        "correct_option_id": correct_option_id,
        "objective_ids": list(objective_ids),
        "difficulty": difficulty,
        "blueprint_tags": list(blueprint_tags),
        "claims": claims,
    }
