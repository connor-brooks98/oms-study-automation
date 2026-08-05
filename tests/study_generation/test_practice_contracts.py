import pytest
from pydantic import ValidationError

from oms_hub.study_generation.practice_contracts import (
    ExtractedAnswer,
    ExtractedQuestion,
    ExtractionPayload,
)


def _question(**overrides: object) -> dict[str, object]:
    return {
        "original_identifier": "Q1",
        "stem": "Which muscle flexes the elbow?",
        "choices": ["Biceps", "Triceps"],
        "supplied_correct_index": 0,
        "rationale": "Biceps flexes the elbow.",
        "source_segment_keys": ["questions-1"],
        "candidate_asset_keys": ["figure-1"],
        "confidence": 0.9,
        **overrides,
    }


def test_question_rejects_casefold_duplicate_choices() -> None:
    with pytest.raises(ValidationError, match="choices must be distinct"):
        ExtractedQuestion.model_validate(_question(choices=["Biceps", "biceps"]))


def test_question_rejects_supplied_answer_outside_its_choices() -> None:
    with pytest.raises(ValidationError, match="available choice"):
        ExtractedQuestion.model_validate(_question(supplied_correct_index=2))


def test_payload_rejects_unrecognized_fields_and_preserves_tuple_collections() -> None:
    with pytest.raises(ValidationError, match="extra_forbidden"):
        ExtractionPayload.model_validate(
            {"questions": [_question()], "answers": [], "invented": True}
        )

    payload = ExtractionPayload.model_validate(
        {
            "questions": [_question(candidate_asset_keys=[])],
            "answers": [
                {
                    "original_identifier": "1.",
                    "correct_index": 0,
                    "source_segment_keys": ["answers-1"],
                }
            ],
        }
    )

    assert payload.questions[0].choices == ("Biceps", "Triceps")
    assert payload.answers == (
        ExtractedAnswer(
            original_identifier="1.",
            correct_index=0,
            rationale=None,
            source_segment_keys=("answers-1",),
        ),
    )
