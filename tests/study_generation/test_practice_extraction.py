import json
from dataclasses import dataclass
from pathlib import Path

import pytest

from oms_hub.document_processing.domain import (
    DocumentLocator,
    ParsedAsset,
    ParsedDocument,
    ParsedSegment,
    SegmentKind,
)
from oms_hub.llm.domain import GeneratedText, LLMTask, ProviderName
from oms_hub.study_generation.practice_contracts import ExtractedAnswer, SegmentCitation
from oms_hub.study_generation.practice_extraction import (
    ExtractionError,
    ExtractionResult,
    PracticeQuestionExtractor,
    SourceDocument,
)
from oms_hub.study_generation.practice_matching import pair_supplied_answers


@dataclass(frozen=True)
class Request:
    instruction: str
    input_text: str
    output_schema: dict[str, object]


class StructuredGenerator:
    def __init__(self, responses: list[str]) -> None:
        self.responses = responses
        self.requests: list[Request] = []

    def generate_text_for_task(
        self,
        task: LLMTask,
        instruction: str,
        input_text: str,
        *,
        output_schema: dict[str, object],
    ) -> GeneratedText:
        assert task is LLMTask.QUIZ_EXTRACTION
        self.requests.append(Request(instruction, input_text, output_schema))
        return GeneratedText(
            text=self.responses.pop(0),
            provider=ProviderName.GEMINI,
            model="extractor",
            request_id=f"request-{len(self.requests)}",
            input_tokens=11,
            output_tokens=7,
            cost_microusd=3,
        )


def _document(
    tmp_path: Path,
    *,
    source_id: str = "source-1",
    segments: tuple[ParsedSegment, ...] | None = None,
) -> ParsedDocument:
    image = tmp_path / f"{source_id}.png"
    image.write_bytes(b"image")
    return ParsedDocument(
        source_id=source_id,
        source_sha256="a" * 64,
        source_format="txt",
        parser_name="fixture",
        parser_version="1",
        segments=segments
        or (
            ParsedSegment(
                "questions-1",
                SegmentKind.PARAGRAPH,
                "1. Which muscle flexes the elbow? A. Biceps B. Triceps",
                DocumentLocator("page 1", page_number=1),
                ("figure-1",),
            ),
        ),
        assets=(
            ParsedAsset(
                "figure-1",
                image,
                "image/png",
                "b" * 64,
                DocumentLocator("page 1 image 1", page_number=1),
            ),
        ),
        warnings=(),
    )


def valid_extraction_json(source_id: str = "source-1") -> str:
    return json.dumps(
        {
            "questions": [
                {
                    "original_identifier": "1",
                    "stem": "Which muscle flexes the elbow?",
                    "choices": ["Biceps", "Triceps"],
                    "supplied_correct_index": 0,
                    "rationale": None,
                    "source_segments": [
                        {"source_id": source_id, "segment_key": "questions-1"}
                    ],
                    "candidate_assets": [{"source_id": source_id, "asset_key": "figure-1"}],
                    "confidence": 0.95,
                },
                {
                    "original_identifier": "2",
                    "stem": "Which muscle extends the elbow?",
                    "choices": ["Biceps", "Triceps"],
                    "supplied_correct_index": None,
                    "rationale": None,
                    "source_segments": [
                        {"source_id": source_id, "segment_key": "questions-1"}
                    ],
                    "candidate_assets": [],
                    "confidence": 0.85,
                },
            ],
            "answers": [],
        }
    )


def valid_answer_only_json(source_id: str = "answers") -> str:
    return json.dumps(
        {
            "questions": [],
            "answers": [
                {
                    "original_identifier": "1",
                    "correct_index": 0,
                    "rationale": None,
                    "source_segments": [
                        {"source_id": source_id, "segment_key": "questions-1"}
                    ],
                }
            ],
        }
    )


def matching_extraction_json() -> str:
    labels = tuple("ABCDEFG")
    mapping = (5, 4, 1, 0, 2, 6, 3)
    return json.dumps(
        {
            "questions": [
                {
                    "kind": "matching",
                    "original_identifier": "1",
                    "stem": "Match each neutral description with its neutral term.",
                    "prompts": [
                        {
                            "original_identifier": label,
                            "text": f"{label}. Description {label}",
                            "supplied_correct_index": None,
                        }
                        for label in labels
                    ],
                    "choices": [f"{number}. Term {number}" for number in range(1, 8)],
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
                            "prompt_identifier": label,
                            "correct_index": correct_index,
                            "rationale": None,
                            "source_segments": [
                                {
                                    "source_id": "source-1",
                                    "segment_key": f"answer-{label.lower()}",
                                }
                            ],
                        }
                        for label, correct_index in zip(labels, mapping, strict=True)
                    ],
                }
            ],
        }
    )


def test_extractor_keeps_a_seven_row_matching_set_grouped_and_resolves_answer_refs(
    tmp_path: Path,
) -> None:
    segments = (
        ParsedSegment(
            "question-1",
            SegmentKind.PARAGRAPH,
            "1. Match A through G to 1 through 7",
            DocumentLocator("page 1", page_number=1),
        ),
        *(
            ParsedSegment(
                f"answer-{label.lower()}",
                SegmentKind.PARAGRAPH,
                f"{label} maps to the source choice",
                DocumentLocator("page 4", page_number=4),
            )
            for label in "ABCDEFG"
        ),
    )
    generator = StructuredGenerator([matching_extraction_json()])

    result = PracticeQuestionExtractor(generator).extract(
        (_document(tmp_path, segments=segments),)
    )

    assert len(result.questions) == len(result.answers) == 1
    assert len(result.questions[0].prompts) == len(result.questions[0].choices) == 7
    assert tuple(prompt.text for prompt in result.questions[0].prompts) == tuple(
        f"Description {label}" for label in "ABCDEFG"
    )
    assert result.questions[0].choices == tuple(f"Term {number}" for number in range(1, 8))
    assert tuple(ref.segment_key for ref in result.answer_source_refs[0]) == tuple(
        f"answer-{label.lower()}" for label in "ABCDEFG"
    )
    assert "one grouped matching question" in generator.requests[0].instruction
    assert "zero-based" in generator.requests[0].instruction


def test_extraction_result_rejects_misaligned_answer_source_refs() -> None:
    answer = ExtractedAnswer(
        original_identifier="1",
        correct_index=0,
        rationale=None,
        source_segments=(SegmentCitation(source_id="source-1", segment_key="answer-1"),),
    )

    with pytest.raises(ValueError, match="answer_source_refs must align with answers"):
        ExtractionResult((), (answer,), (), (), (), ())


def test_extractor_merges_disjoint_matching_answer_rows_with_aligned_refs(
    tmp_path: Path,
) -> None:
    first = json.loads(matching_extraction_json())
    second = json.loads(matching_extraction_json())
    first["answers"][0]["matches"] = first["answers"][0]["matches"][:3]
    second["answers"][0]["matches"] = second["answers"][0]["matches"][3:]
    second["questions"] = first["questions"]
    for match in second["answers"][0]["matches"]:
        match["source_segments"][0]["source_id"] = "source-2"
    first_segments = (
        ParsedSegment(
            "question-1", SegmentKind.PARAGRAPH, "1. Match entries", DocumentLocator("p1")
        ),
        *(
            ParsedSegment(
                f"answer-{label.lower()}",
                SegmentKind.PARAGRAPH,
                f"{label} maps to term",
                DocumentLocator("p4"),
            )
            for label in "ABCDEFG"
            if label in "ABC"
        ),
    )
    second_segments = tuple(
        ParsedSegment(
            f"answer-{label.lower()}",
            SegmentKind.PARAGRAPH,
            f"{label} maps to term",
            DocumentLocator("p4"),
        )
        for label in "DEFG"
    )
    result = PracticeQuestionExtractor(
        StructuredGenerator([json.dumps(first), json.dumps(second)]), max_input_characters=500
    ).extract(
        (
            _document(tmp_path, source_id="source-1", segments=first_segments),
            _document(tmp_path, source_id="source-2", segments=second_segments),
        ),
    )

    assert len(result.questions) == 1
    assert len(result.answers) == len(result.answer_source_refs) == 2
    assert tuple(ref.segment_key for ref in result.answer_source_refs[0]) == (
        "answer-a",
        "answer-b",
        "answer-c",
    )
    assert tuple(ref.segment_key for ref in result.answer_source_refs[1]) == (
        "answer-d",
        "answer-e",
        "answer-f",
        "answer-g",
    )


def test_matching_duplicate_identity_uses_matching_specific_diagnostic(tmp_path: Path) -> None:
    first = json.loads(matching_extraction_json())
    second = json.loads(matching_extraction_json())
    second["questions"][0]["stem"] = "Conflicting matching wording."
    first["answers"] = []
    second["answers"] = []
    first_segments = (
        ParsedSegment(
            "question-1", SegmentKind.PARAGRAPH, "1. Match entries", DocumentLocator("p1")
        ),
    )
    second_segments = (
        ParsedSegment("extra", SegmentKind.PARAGRAPH, "Answer key", DocumentLocator("p2")),
    )
    result = PracticeQuestionExtractor(
        StructuredGenerator([json.dumps(first), json.dumps(second)]), max_input_characters=230
    ).extract(
        (
            _document(tmp_path, source_id="source-1", segments=first_segments),
            _document(tmp_path, source_id="source-2", segments=second_segments),
        )
    )

    assert {item.code for item in result.diagnostics} == {
        "conflicting-matching-question-identifier"
    }


def test_extractor_retries_schema_failure_once(tmp_path: Path) -> None:
    structured_generator = StructuredGenerator(["not-json", valid_extraction_json()])
    result = PracticeQuestionExtractor(structured_generator).extract((_document(tmp_path),))

    assert len(result.questions) == 2
    assert len(structured_generator.requests) == 2
    assert "color, highlighting" in structured_generator.requests[0].instruction
    assert (
        "previous response failed schema validation"
        in structured_generator.requests[1].instruction
    )
    assert result.provider_metadata[-1].request_id == "request-2"


def test_extractor_accepts_an_answer_only_chunk(tmp_path: Path) -> None:
    questions = _document(tmp_path, source_id="questions")
    answers = _document(tmp_path, source_id="answers")
    generator = StructuredGenerator(
        [
            valid_extraction_json("questions"),
            valid_answer_only_json(),
            valid_answer_only_json(),
        ]
    )

    result = PracticeQuestionExtractor(generator, max_input_characters=230).extract(
        (questions, answers)
    )

    assert len(generator.requests) == 2
    assert len(result.questions) == 2
    assert len(result.answers) == 1


def test_extractor_rejects_an_import_with_no_questions(tmp_path: Path) -> None:
    generator = StructuredGenerator([valid_answer_only_json(), valid_answer_only_json()])

    with pytest.raises(ValueError, match="no questions"):
        PracticeQuestionExtractor(generator).extract(
            (SourceDocument(_document(tmp_path, source_id="answers"), "Answers", "answer_key"),)
        )


@pytest.mark.parametrize("reference_field", ["source_segments", "candidate_assets"])
def test_extractor_rejects_invented_source_references(
    tmp_path: Path, reference_field: str
) -> None:
    payload = json.loads(valid_extraction_json())
    key = "segment_key" if reference_field == "source_segments" else "asset_key"
    payload["questions"][0][reference_field] = [{"source_id": "source-1", key: "invented"}]
    extractor = PracticeQuestionExtractor(
        StructuredGenerator([json.dumps(payload), json.dumps(payload)])
    )

    with pytest.raises(ExtractionError, match="schema validation") as error:
        extractor.extract((_document(tmp_path),))

    assert len(error.value.raw_responses) == 2
    assert len(error.value.provider_metadata) == 2
    assert (
        "previous response failed schema validation"
        in extractor.generator.requests[1].instruction
    )


def test_extractor_merges_identical_chunk_duplicates_and_blocks_conflicts(tmp_path: Path) -> None:
    first = json.loads(valid_extraction_json())
    second = json.loads(valid_extraction_json())
    second["questions"] = [second["questions"][0]]
    second["questions"][0]["stem"] = "Conflicting wording"
    segments = (
        ParsedSegment("questions-1", SegmentKind.HEADING, "Set one", DocumentLocator("p1")),
        ParsedSegment("heading-2", SegmentKind.HEADING, "Set two", DocumentLocator("p2")),
    )
    document = _document(tmp_path, segments=segments)
    extractor = PracticeQuestionExtractor(
        StructuredGenerator([json.dumps(first), json.dumps(second)]), max_input_characters=200
    )

    result = extractor.extract((document,))

    assert len(result.questions) == 3
    assert any("conflicting duplicate question" in item.message for item in result.diagnostics)
    assert all(len(request.input_text) < 180 for request in extractor.generator.requests)


def test_prompt_keeps_source_order_and_source_context(tmp_path: Path) -> None:
    document = _document(
        tmp_path,
        segments=(
            ParsedSegment("heading", SegmentKind.HEADING, "Chapter", DocumentLocator("page 1")),
            ParsedSegment(
                "questions-1", SegmentKind.PARAGRAPH, "1. Question", DocumentLocator("page 1")
            ),
        ),
    )
    generator = StructuredGenerator([valid_extraction_json()])
    extractor = PracticeQuestionExtractor(generator)

    extractor.extract((SourceDocument(document, "Professor packet", "questions"),))

    prompt = generator.requests[0].input_text
    assert "source_title: Professor packet" in prompt
    assert "source_role: questions" in prompt
    assert prompt.index("segment_key: heading") < prompt.index("segment_key: questions-1")


def test_prompt_includes_answer_formatting_sidecar_without_rewriting_text(
    tmp_path: Path,
) -> None:
    document = _document(
        tmp_path,
        segments=(
            ParsedSegment(
                "questions-1",
                SegmentKind.PARAGRAPH,
                "C) Correct answer",
                DocumentLocator("slide 2", slide_number=2),
                style_metadata=("bold: C) Correct answer", "color #FF0000: C) Correct answer"),
            ),
        ),
    )
    generator = StructuredGenerator([valid_extraction_json()])

    PracticeQuestionExtractor(generator).extract((document,))

    prompt = generator.requests[0].input_text
    assert "text: C) Correct answer" in prompt
    assert (
        "source_style_metadata: bold: C) Correct answer; color #FF0000: C) Correct answer"
        in prompt
    )


@pytest.mark.parametrize(
    "cue",
    (
        "bold: B) Correct",
        "italic: B) Correct",
        "underline: B) Correct",
        "highlighted: B) Correct",
        "color #FF0000: B) Correct",
    ),
)
def test_extractor_applies_unique_styled_option_on_following_slide(
    tmp_path: Path, cue: str
) -> None:
    document = _document(
        tmp_path,
        segments=(
            ParsedSegment(
                "question",
                SegmentKind.PARAGRAPH,
                "Which answer? A) Wrong B) Correct",
                DocumentLocator("slide 1", slide_number=1),
            ),
            ParsedSegment(
                "answer",
                SegmentKind.PARAGRAPH,
                "Which answer? A) Wrong B) Correct",
                DocumentLocator("slide 2", slide_number=2),
                style_metadata=(cue,),
            ),
        ),
    )
    payload = {
        "questions": [
            {
                "original_identifier": None,
                "stem": "Which answer?",
                "choices": ["Wrong", "Correct"],
                "supplied_correct_index": None,
                "rationale": None,
                "source_segments": [
                    {"source_id": "source-1", "segment_key": "question"}
                ],
                "candidate_assets": [],
                "confidence": 0.9,
            }
        ],
        "answers": [],
    }

    result = PracticeQuestionExtractor(
        StructuredGenerator([json.dumps(payload)])
    ).extract((document,))

    assert result.questions[0].supplied_correct_index == 1
    assert result.questions[0].rationale == "Source-marked correct answer: Correct"
    assert result.questions[0].source_segments[-1].segment_key == "answer"


def test_extractor_does_not_guess_when_multiple_options_are_styled(tmp_path: Path) -> None:
    document = _document(
        tmp_path,
        segments=(
            ParsedSegment(
                "question",
                SegmentKind.PARAGRAPH,
                "Which answer? A) One B) Two",
                DocumentLocator("slide 1", slide_number=1),
            ),
            ParsedSegment(
                "answer",
                SegmentKind.PARAGRAPH,
                "Which answer? A) One B) Two",
                DocumentLocator("slide 2", slide_number=2),
                style_metadata=("bold: A) One", "bold: B) Two"),
            ),
        ),
    )
    payload = json.loads(valid_extraction_json())
    payload["questions"] = [
        {
            "original_identifier": None,
            "stem": "Which answer?",
            "choices": ["One", "Two"],
            "supplied_correct_index": None,
            "rationale": None,
            "source_segments": [
                {"source_id": "source-1", "segment_key": "question"}
            ],
            "candidate_assets": [],
            "confidence": 0.9,
        }
    ]

    result = PracticeQuestionExtractor(
        StructuredGenerator([json.dumps(payload)])
    ).extract((document,))

    assert result.questions[0].supplied_correct_index is None


def test_extractor_does_not_apply_next_questions_styled_option(
    tmp_path: Path,
) -> None:
    document = _document(
        tmp_path,
        segments=(
            ParsedSegment(
                "question-one",
                SegmentKind.PARAGRAPH,
                "What is the first diagnosis? A) Shared B) Other",
                DocumentLocator("slide 1", slide_number=1),
            ),
            ParsedSegment(
                "question-two",
                SegmentKind.PARAGRAPH,
                "What is the second diagnosis? A) Shared B) Different",
                DocumentLocator("slide 2", slide_number=2),
                style_metadata=("bold: A) Shared",),
            ),
        ),
    )
    payload = {
        "questions": [
            {
                "original_identifier": None,
                "stem": "What is the first diagnosis?",
                "choices": ["Shared", "Other"],
                "supplied_correct_index": None,
                "rationale": None,
                "source_segments": [
                    {"source_id": "source-1", "segment_key": "question-one"}
                ],
                "candidate_assets": [],
                "confidence": 0.9,
            }
        ],
        "answers": [],
    }

    result = PracticeQuestionExtractor(
        StructuredGenerator([json.dumps(payload)])
    ).extract((document,))

    assert result.questions[0].supplied_correct_index is None


def test_extractor_accepts_repeated_slide_with_minor_stem_wording_change(
    tmp_path: Path,
) -> None:
    document = _document(
        tmp_path,
        segments=(
            ParsedSegment(
                "question",
                SegmentKind.PARAGRAPH,
                "He develops flushing during his vancomycin infusion. What explains this? "
                "A) Allergy B) Histamine release",
                DocumentLocator("slide 1", slide_number=1),
            ),
            ParsedSegment(
                "answer",
                SegmentKind.PARAGRAPH,
                "He develops flushing during his antibiotic infusion. What explains this? "
                "A) Allergy B) Histamine release",
                DocumentLocator("slide 2", slide_number=2),
                style_metadata=("bold: B) Histamine release",),
            ),
        ),
    )
    payload = {
        "questions": [
            {
                "original_identifier": None,
                "stem": "He develops flushing during his vancomycin infusion. What explains this?",
                "choices": ["Allergy", "Histamine release"],
                "supplied_correct_index": None,
                "rationale": None,
                "source_segments": [
                    {"source_id": "source-1", "segment_key": "question"}
                ],
                "candidate_assets": [],
                "confidence": 0.9,
            }
        ],
        "answers": [],
    }

    result = PracticeQuestionExtractor(
        StructuredGenerator([json.dumps(payload)])
    ).extract((document,))

    assert result.questions[0].supplied_correct_index == 1


def test_extractor_blocks_partial_results_when_source_has_a_sequential_question_set(
    tmp_path: Path,
) -> None:
    segments = tuple(
        ParsedSegment(
            f"question-{number}",
            SegmentKind.PARAGRAPH,
            f"{number}. Question {number}? A. One B. Two",
            DocumentLocator(f"block {number}"),
        )
        for number in range(1, 17)
    )
    document = _document(tmp_path, segments=segments)
    partial = json.loads(valid_extraction_json())
    partial["questions"] = [partial["questions"][0]]
    partial["questions"][0]["source_segments"] = [
        {"source_id": "source-1", "segment_key": "question-1"}
    ]

    result = PracticeQuestionExtractor(StructuredGenerator([json.dumps(partial)])).extract(
        (document,)
    )

    assert any(
        diagnostic.code == "incomplete-sequential-question-extraction"
        and diagnostic.severity.value == "blocker"
        and "2 through 16" in diagnostic.message
        for diagnostic in result.diagnostics
    )


def test_heading_section_stays_together_when_the_section_fits_the_bound(tmp_path: Path) -> None:
    document = _document(
        tmp_path,
        segments=(
            ParsedSegment("heading", SegmentKind.HEADING, "Chapter", DocumentLocator("page 1")),
            ParsedSegment(
                "questions-1", SegmentKind.PARAGRAPH, "1. Question", DocumentLocator("page 1")
            ),
        ),
    )
    generator = StructuredGenerator([valid_extraction_json(), valid_extraction_json()])

    PracticeQuestionExtractor(generator, max_input_characters=260).extract((document,))

    assert len(generator.requests) == 1


def test_document_scoped_citations_preserve_real_question_source_refs(tmp_path: Path) -> None:
    questions = _document(tmp_path, source_id="questions")
    answers = _document(tmp_path, source_id="answers")
    generator = StructuredGenerator(
        [valid_extraction_json("questions"), valid_extraction_json("answers")]
    )
    result = PracticeQuestionExtractor(generator, max_input_characters=230).extract(
        (questions, answers)
    )

    drafts = pair_supplied_answers(
        result.questions,
        result.answers,
        question_source_refs=result.question_source_refs,
    ).drafts

    assert result.question_source_refs[0][0].source_id == "questions"
    assert result.question_source_refs[0][0].segment_key == "questions-1"
    assert result.question_source_refs[0][0].locator == "page 1"
    assert drafts[0].source_refs == result.question_source_refs[0]


def test_wrong_document_citation_retries_then_retains_failure_evidence(tmp_path: Path) -> None:
    questions = _document(tmp_path, source_id="questions")
    answers = _document(
        tmp_path,
        source_id="answers",
        segments=(
            ParsedSegment("answers-1", SegmentKind.PARAGRAPH, "1. A", DocumentLocator("page 2")),
        ),
    )
    wrong = json.loads(valid_extraction_json("questions"))
    wrong["questions"][0]["source_segments"][0]["source_id"] = "answers"
    generator = StructuredGenerator([json.dumps(wrong), json.dumps(wrong)])

    with pytest.raises(ExtractionError) as error:
        PracticeQuestionExtractor(generator).extract((questions, answers))

    assert len(generator.requests) == 2
    assert len(error.value.raw_responses) == 2
    assert [item.request_id for item in error.value.provider_metadata] == ["request-1", "request-2"]


@pytest.mark.parametrize(
    ("second_identifier", "second_source_id"),
    [
        ("1", "source-2"),
        ("2", "source-1"),
    ],
)
def test_merge_blocks_one_axis_question_identity_conflicts(
    tmp_path: Path,
    second_identifier: str,
    second_source_id: str,
) -> None:
    first = json.loads(valid_extraction_json("source-1"))
    second = json.loads(valid_extraction_json("source-2"))
    second_question = second["questions"][0]
    second_question["original_identifier"] = second_identifier
    second_question["source_segments"][0]["source_id"] = second_source_id
    documents = (
        _document(tmp_path, source_id="source-1"),
        _document(tmp_path, source_id="source-2"),
    )
    generator = StructuredGenerator([json.dumps(first), json.dumps(second)])

    result = PracticeQuestionExtractor(generator, max_input_characters=230).extract(documents)

    assert len(result.questions) >= 2
    assert any(item.severity.value == "blocker" for item in result.diagnostics)
