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
from oms_hub.study_generation.practice_extraction import (
    PracticeQuestionExtractor,
    SourceDocument,
)


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


def valid_extraction_json() -> str:
    return json.dumps(
        {
            "questions": [
                {
                    "original_identifier": "1",
                    "stem": "Which muscle flexes the elbow?",
                    "choices": ["Biceps", "Triceps"],
                    "supplied_correct_index": 0,
                    "rationale": None,
                    "source_segment_keys": ["questions-1"],
                    "candidate_asset_keys": ["figure-1"],
                    "confidence": 0.95,
                },
                {
                    "original_identifier": "2",
                    "stem": "Which muscle extends the elbow?",
                    "choices": ["Biceps", "Triceps"],
                    "supplied_correct_index": None,
                    "rationale": None,
                    "source_segment_keys": ["questions-1"],
                    "candidate_asset_keys": [],
                    "confidence": 0.85,
                },
            ],
            "answers": [],
        }
    )


def test_extractor_retries_schema_failure_once(tmp_path: Path) -> None:
    structured_generator = StructuredGenerator(["not-json", valid_extraction_json()])
    result = PracticeQuestionExtractor(structured_generator).extract((_document(tmp_path),))

    assert len(result.questions) == 2
    assert len(structured_generator.requests) == 2
    assert (
        "previous response failed schema validation"
        in structured_generator.requests[1].instruction
    )
    assert result.provider_metadata[-1].request_id == "request-2"


@pytest.mark.parametrize("reference_field", ["source_segment_keys", "candidate_asset_keys"])
def test_extractor_rejects_invented_source_references(
    tmp_path: Path, reference_field: str
) -> None:
    payload = json.loads(valid_extraction_json())
    payload["questions"][0][reference_field] = ["invented"]
    extractor = PracticeQuestionExtractor(StructuredGenerator([json.dumps(payload)]))

    with pytest.raises(ValueError, match="unknown"):
        extractor.extract((_document(tmp_path),))


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

    assert len(result.questions) == 2
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
