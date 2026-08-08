import pytest
from pydantic import ValidationError

from oms_hub.study_generation.practice_contracts import (
    AssetCitation,
    ExtractedAnswer,
    ExtractedQuestion,
    ExtractionPayload,
    SegmentCitation,
)


def _question(**overrides: object) -> dict[str, object]:
    return {
        "original_identifier": "Q1",
        "stem": "Which muscle flexes the elbow?",
        "choices": ["Biceps", "Triceps"],
        "supplied_correct_index": 0,
        "rationale": "Biceps flexes the elbow.",
        "source_segments": [{"source_id": "source-1", "segment_key": "questions-1"}],
        "candidate_assets": [{"source_id": "source-1", "asset_key": "figure-1"}],
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
            "questions": [_question(candidate_assets=[])],
            "answers": [
                {
                    "original_identifier": "1.",
                    "correct_index": 0,
                    "source_segments": [{"source_id": "source-1", "segment_key": "answers-1"}],
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
            source_segments=(SegmentCitation(source_id="source-1", segment_key="answers-1"),),
        ),
    )


def test_citations_are_strict_immutable_structured_values() -> None:
    question = ExtractedQuestion.model_validate(_question())

    assert question.source_segments == (
        SegmentCitation(source_id="source-1", segment_key="questions-1"),
    )
    assert question.candidate_assets == (
        AssetCitation(source_id="source-1", asset_key="figure-1"),
    )
    with pytest.raises(ValidationError, match="extra_forbidden"):
        SegmentCitation.model_validate(
            {"source_id": "source-1", "segment_key": "questions-1", "locator": "fake"}
        )
