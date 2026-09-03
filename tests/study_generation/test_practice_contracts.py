import pytest
from pydantic import ValidationError

from oms_hub.document_processing.domain import (
    DocumentLocator,
    ParsedAsset,
    ParsedDocument,
    ParsedSegment,
    SegmentKind,
)
from oms_hub.study_generation.practice_contracts import (
    AssetCitation,
    ExtractedAnswer,
    ExtractedMatchingAnswer,
    ExtractedMatchingAnswerRow,
    ExtractedMatchingPrompt,
    ExtractedMatchingQuestion,
    ExtractedQuestion,
    ExtractionPayload,
    SegmentCitation,
    validate_source_references,
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


def test_matching_extraction_contract_preserves_group_rows_and_zero_based_indexes() -> None:
    payload = ExtractionPayload.model_validate(
        {
            "questions": [
                {
                    "kind": "matching",
                    "original_identifier": "1",
                    "stem": "Match each prompt.",
                    "prompts": [
                        {
                            "original_identifier": "A",
                            "text": "A. Alpha description",
                            "supplied_correct_index": None,
                        },
                        {
                            "original_identifier": "B",
                            "text": "B) Beta description",
                            "supplied_correct_index": 0,
                        },
                    ],
                    "choices": ["1. First term", "2) Second term"],
                    "rationale": None,
                    "source_segments": [
                        {"source_id": "source-1", "segment_key": "question-1"}
                    ],
                    "candidate_assets": [],
                    "confidence": 0.99,
                }
            ],
            "answers": [
                {
                    "kind": "matching",
                    "original_identifier": "1",
                    "matches": [
                        {
                            "prompt_identifier": "A",
                            "correct_index": 1,
                            "rationale": None,
                            "source_segments": [
                                {"source_id": "source-1", "segment_key": "answer-a"}
                            ],
                        },
                        {
                            "prompt_identifier": "B",
                            "correct_index": 0,
                            "rationale": "Source explanation.",
                            "source_segments": [
                                {"source_id": "source-1", "segment_key": "answer-b"}
                            ],
                        },
                    ],
                }
            ],
        }
    )

    question = payload.questions[0]
    answer = payload.answers[0]
    assert isinstance(question, ExtractedMatchingQuestion)
    assert isinstance(question.prompts[0], ExtractedMatchingPrompt)
    assert tuple(prompt.text for prompt in question.prompts) == (
        "Alpha description",
        "Beta description",
    )
    assert question.choices == ("First term", "Second term")
    assert isinstance(answer, ExtractedMatchingAnswer)
    assert isinstance(answer.matches[0], ExtractedMatchingAnswerRow)
    assert answer.matches[0].correct_index == 1


@pytest.mark.parametrize(
    "question",
    [
        {
            "kind": "matching",
            "stem": "Match.",
            "prompts": [
                {"original_identifier": "A", "text": "Alpha", "supplied_correct_index": None}
            ],
            "choices": ["First", "Second"],
            "source_segments": [{"source_id": "source-1", "segment_key": "question-1"}],
            "candidate_assets": [],
            "confidence": 0.9,
        },
        {
            "kind": "matching",
            "stem": "Match.",
            "prompts": [
                {"original_identifier": "A", "text": "Alpha", "supplied_correct_index": None},
                {"original_identifier": "B", "text": "Beta", "supplied_correct_index": None},
            ],
            "choices": [str(index) for index in range(9)],
            "source_segments": [{"source_id": "source-1", "segment_key": "question-1"}],
            "candidate_assets": [],
            "confidence": 0.9,
        },
        {
            "kind": "matching",
            "stem": "Match.",
            "prompts": [
                {"original_identifier": " ", "text": "Alpha", "supplied_correct_index": None},
                {"original_identifier": "B", "text": " ", "supplied_correct_index": None},
            ],
            "choices": ["First", "Second"],
            "source_segments": [{"source_id": "source-1", "segment_key": "question-1"}],
            "candidate_assets": [],
            "confidence": 0.9,
        },
        {
            "kind": "matching",
            "stem": "Match.",
            "prompts": [
                {"original_identifier": "A", "text": "Alpha", "supplied_correct_index": None},
                {"original_identifier": "B", "text": "Beta", "supplied_correct_index": None},
            ],
            "choices": ["1. First", "2) first"],
            "source_segments": [{"source_id": "source-1", "segment_key": "question-1"}],
            "candidate_assets": [],
            "confidence": 0.9,
        },
        {"kind": "unknown"},
    ],
)
def test_payload_rejects_invalid_matching_question_shapes(question: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        ExtractionPayload.model_validate({"questions": [question], "answers": []})


def test_matching_payload_leaves_pairing_diagnostics_schema_valid() -> None:
    payload = ExtractionPayload.model_validate(
        {
            "questions": [
                {
                    "kind": "matching",
                    "stem": "Match.",
                    "prompts": [
                        {
                            "original_identifier": "A",
                            "text": "Alpha",
                            "supplied_correct_index": None,
                        },
                        {
                            "original_identifier": "A",
                            "text": "Beta",
                            "supplied_correct_index": None,
                        },
                    ],
                    "choices": ["First", "Second"],
                    "source_segments": [
                        {"source_id": "source-1", "segment_key": "question-1"}
                    ],
                    "candidate_assets": [],
                    "confidence": 0.9,
                }
            ],
            "answers": [
                {
                    "kind": "matching",
                    "matches": [
                        {
                            "correct_index": 7,
                            "source_segments": [
                                {"source_id": "source-1", "segment_key": "answer-1"}
                            ],
                        }
                    ],
                }
            ],
        }
    )

    assert isinstance(payload.questions[0], ExtractedMatchingQuestion)
    assert payload.answers[0].matches[0].correct_index == 7


def test_matching_source_references_validate_question_assets_and_answer_rows(tmp_path) -> None:
    payload = ExtractionPayload.model_validate(
        {
            "questions": [
                {
                    "kind": "matching",
                    "stem": "Match.",
                    "prompts": [
                        {
                            "original_identifier": "A",
                            "text": "Alpha",
                            "supplied_correct_index": None,
                        },
                        {
                            "original_identifier": "B",
                            "text": "Beta",
                            "supplied_correct_index": None,
                        },
                    ],
                    "choices": ["First", "Second"],
                    "source_segments": [{"source_id": "source-1", "segment_key": "question-1"}],
                    "candidate_assets": [{"source_id": "source-1", "asset_key": "figure-1"}],
                    "confidence": 0.9,
                }
            ],
            "answers": [
                {
                    "kind": "matching",
                    "matches": [
                        {
                            "correct_index": 0,
                            "source_segments": [
                                {"source_id": "source-1", "segment_key": "answer-1"}
                            ],
                        }
                    ],
                }
            ],
        }
    )
    asset_path = tmp_path / "figure.png"
    asset_path.write_bytes(b"image")
    document = ParsedDocument(
        source_id="source-1",
        source_sha256="a" * 64,
        source_format="txt",
        parser_name="fixture",
        parser_version="1",
        segments=(
            ParsedSegment(
                "question-1", SegmentKind.PARAGRAPH, "Question", DocumentLocator("page 1")
            ),
            ParsedSegment("answer-1", SegmentKind.PARAGRAPH, "Answer", DocumentLocator("page 1")),
        ),
        assets=(
            ParsedAsset(
                "figure-1", asset_path, "image/png", "b" * 64, DocumentLocator("page 1")
            ),
        ),
        warnings=(),
    )

    validate_source_references(payload, documents=(document,))


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
