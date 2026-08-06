"""Bounded, provenance-preserving extraction of imported practice questions."""

import json
import re
from dataclasses import dataclass
from typing import Protocol

from pydantic import ValidationError

from oms_hub.document_processing.domain import ParsedDocument, ParsedSegment, SegmentKind
from oms_hub.llm.domain import GeneratedText, LLMTask, ProviderName
from oms_hub.study_generation.practice_contracts import (
    ExtractedAnswer,
    ExtractedQuestion,
    ExtractionPayload,
    SegmentCitation,
    validate_source_references,
)
from oms_hub.study_generation.practice_domain import (
    DiagnosticSeverity,
    DraftDiagnostic,
    QuestionSourceRef,
)
from oms_hub.study_generation.practice_matching import normalize_identifier

_DEFAULT_MAX_INPUT_CHARACTERS = 60_000
_EXTRACTION_INSTRUCTION = """Extract supplied practice questions and answer-key entries.
Return only JSON matching the provided schema. Preserve source wording, cite every
question and answer with document-qualified source_segments, and cite only
document-qualified candidate_assets present in the input. Do not invent
questions, answers, references, or assets."""


class ExtractionTextGenerator(Protocol):
    def generate_text_for_task(
        self,
        task: LLMTask,
        instruction: str,
        input_text: str,
        *,
        output_schema: dict[str, object],
    ) -> GeneratedText: ...


@dataclass(frozen=True, slots=True)
class SourceDocument:
    """Canonical document plus the import-facing title and role for prompt context."""

    document: ParsedDocument
    title: str
    role: str

    def __post_init__(self) -> None:
        if not self.title.strip() or not self.role.strip():
            raise ValueError("source title and role must not be blank")


@dataclass(frozen=True, slots=True)
class ExtractionProviderMetadata:
    provider: ProviderName
    model: str
    request_id: str
    input_tokens: int
    output_tokens: int
    cost_microusd: int
    cache_creation_input_tokens: int
    cache_read_input_tokens: int

    @classmethod
    def from_generated(cls, generated: GeneratedText) -> "ExtractionProviderMetadata":
        return cls(
            generated.provider,
            generated.model,
            generated.request_id,
            generated.input_tokens,
            generated.output_tokens,
            generated.cost_microusd,
            generated.cache_creation_input_tokens,
            generated.cache_read_input_tokens,
        )


@dataclass(frozen=True, slots=True)
class ExtractionResult:
    questions: tuple[ExtractedQuestion, ...]
    answers: tuple[ExtractedAnswer, ...]
    question_source_refs: tuple[tuple[QuestionSourceRef, ...], ...]
    provider_metadata: tuple[ExtractionProviderMetadata, ...]
    diagnostics: tuple[DraftDiagnostic, ...]

    @property
    def raw_provider_metadata(self) -> tuple[ExtractionProviderMetadata, ...]:
        """Persistence-oriented alias for metadata emitted by each provider request."""
        return self.provider_metadata


class ExtractionError(ValueError):
    """A provider response remained invalid after the one allowed schema retry."""

    def __init__(
        self,
        message: str,
        *,
        raw_responses: tuple[str, ...],
        provider_metadata: tuple[ExtractionProviderMetadata, ...],
    ) -> None:
        super().__init__(message)
        self.raw_responses = raw_responses
        self.provider_metadata = provider_metadata


class PracticeQuestionExtractor:
    def __init__(
        self,
        generator: ExtractionTextGenerator,
        *,
        max_input_characters: int = _DEFAULT_MAX_INPUT_CHARACTERS,
    ) -> None:
        if not 1 <= max_input_characters <= _DEFAULT_MAX_INPUT_CHARACTERS:
            raise ValueError("max_input_characters must be between 1 and 60000")
        self.generator = generator
        self.max_input_characters = max_input_characters

    def extract(
        self, documents: tuple[ParsedDocument | SourceDocument, ...]
    ) -> ExtractionResult:
        sources = tuple(_source_document(document) for document in documents)
        if not sources:
            raise ValueError("at least one parsed document is required")
        chunks = _chunks(sources, self.max_input_characters)
        canonical_documents = tuple(source.document for source in sources)
        metadata: list[ExtractionProviderMetadata] = []
        diagnostics: list[DraftDiagnostic] = []
        merged_questions: list[ExtractedQuestion] = []
        question_source_refs: list[tuple[QuestionSourceRef, ...]] = []
        questions_by_composite: dict[
            tuple[str | None, tuple[SegmentCitation, ...]], list[ExtractedQuestion]
        ] = {}
        source_refs_by_identifier: dict[str, set[tuple[SegmentCitation, ...]]] = {}
        identifiers_by_source_refs: dict[tuple[SegmentCitation, ...], set[str | None]] = {}
        answers: list[ExtractedAnswer] = []

        for chunk in chunks:
            payload, attempts = self._extract_chunk(chunk, canonical_documents)
            metadata.extend(attempts)
            for question in payload.questions:
                normalized_identifier = _normalized_identifier(question.original_identifier)
                citations = tuple(sorted(question.source_segments, key=_citation_sort_key))
                composite = (normalized_identifier, citations)
                existing = questions_by_composite.get(composite, [])
                if any(item == question for item in existing):
                    continue
                if existing:
                    diagnostics.append(
                        DraftDiagnostic(
                            "conflicting-duplicate-question",
                            "conflicting duplicate question was extracted; review is required",
                            DiagnosticSeverity.BLOCKER,
                        )
                    )
                if (
                    normalized_identifier is not None
                    and citations
                    not in source_refs_by_identifier.get(normalized_identifier, set())
                    and normalized_identifier in source_refs_by_identifier
                ):
                    diagnostics.append(
                        DraftDiagnostic(
                            "conflicting-question-identifier",
                            "question identifier cites different source references; "
                            "review is required",
                            DiagnosticSeverity.BLOCKER,
                        )
                    )
                prior_identifiers = identifiers_by_source_refs.get(citations, set())
                if prior_identifiers and normalized_identifier not in prior_identifiers:
                    diagnostics.append(
                        DraftDiagnostic(
                            "conflicting-question-source-reference",
                            "source reference identifies different questions; review is required",
                            DiagnosticSeverity.BLOCKER,
                        )
                    )
                questions_by_composite.setdefault(composite, []).append(question)
                if normalized_identifier is not None:
                    source_refs_by_identifier.setdefault(normalized_identifier, set()).add(
                        citations
                    )
                identifiers_by_source_refs.setdefault(citations, set()).add(normalized_identifier)
                merged_questions.append(question)
                question_source_refs.append(
                    _resolve_question_source_refs(question, canonical_documents)
                )
            for answer in payload.answers:
                if answer not in answers:
                    answers.append(answer)

        expected_identifiers = _sequential_question_identifiers(sources)
        extracted_identifiers = {
            identifier
            for question in merged_questions
            if (identifier := _normalized_identifier(question.original_identifier)) is not None
        }
        missing_identifiers = tuple(
            identifier
            for identifier in expected_identifiers
            if identifier not in extracted_identifiers
        )
        if missing_identifiers:
            diagnostics.append(
                DraftDiagnostic(
                    "incomplete-sequential-question-extraction",
                    "source contains explicitly numbered questions "
                    f"{_identifier_range(expected_identifiers)} but extraction did not return "
                    f"{_identifier_range(missing_identifiers)}; review is required",
                    DiagnosticSeverity.BLOCKER,
                )
            )

        return ExtractionResult(
            tuple(merged_questions),
            tuple(answers),
            tuple(question_source_refs),
            tuple(metadata),
            tuple(diagnostics),
        )

    def _extract_chunk(
        self, chunk: str, documents: tuple[ParsedDocument, ...]
    ) -> tuple[ExtractionPayload, tuple[ExtractionProviderMetadata, ...]]:
        metadata: list[ExtractionProviderMetadata] = []
        responses: list[str] = []
        instruction = _EXTRACTION_INSTRUCTION
        for attempt in range(2):
            generated = self.generator.generate_text_for_task(
                LLMTask.QUIZ_EXTRACTION,
                instruction,
                chunk,
                output_schema=ExtractionPayload.model_json_schema(),
            )
            metadata.append(ExtractionProviderMetadata.from_generated(generated))
            responses.append(generated.text)
            try:
                payload = _parse_payload(generated.text)
                validate_source_references(payload, documents=documents)
                return payload, tuple(metadata)
            except (json.JSONDecodeError, ValidationError, TypeError, ValueError) as error:
                if attempt == 1:
                    raise ExtractionError(
                        "extraction response failed schema validation after one retry",
                        raw_responses=tuple(responses),
                        provider_metadata=tuple(metadata),
                    ) from error
                instruction = (
                    f"{_EXTRACTION_INSTRUCTION}\n\nThe previous response failed schema validation: "
                    f"{error}. Correct the JSON and return only a schema-valid response."
                )
        raise AssertionError("unreachable")


def _parse_payload(text: str) -> ExtractionPayload:
    return ExtractionPayload.model_validate(json.loads(text))


def _source_document(document: ParsedDocument | SourceDocument) -> SourceDocument:
    if isinstance(document, SourceDocument):
        return document
    return SourceDocument(document, document.source_id, "unspecified")


def _resolve_question_source_refs(
    question: ExtractedQuestion, documents: tuple[ParsedDocument, ...]
) -> tuple[QuestionSourceRef, ...]:
    documents_by_id = {document.source_id: document for document in documents}
    references: list[QuestionSourceRef] = []
    for citation in question.source_segments:
        document = documents_by_id[citation.source_id]
        segment = next(
            segment for segment in document.segments if segment.key == citation.segment_key
        )
        references.append(
            QuestionSourceRef(citation.source_id, citation.segment_key, segment.locator.label)
        )
    return tuple(references)


def _citation_sort_key(citation: SegmentCitation) -> tuple[str, str]:
    return citation.source_id, citation.segment_key


def _chunks(sources: tuple[SourceDocument, ...], maximum: int) -> tuple[str, ...]:
    chunks: list[str] = []
    current = ""

    def add(serialized: str) -> None:
        nonlocal current
        if current and len(current) + 2 + len(serialized) >= maximum:
            chunks.append(current)
            current = serialized
        else:
            current = f"{current}\n\n{serialized}" if current else serialized

    for source in sources:
        header = _source_header(source)
        for section in _heading_sections(source.document.segments):
            serialized = f"{header}{''.join(_serialize_segment(segment) for segment in section)}"
            if len(serialized) < maximum:
                add(serialized)
                continue
            for segment in section:
                serialized = f"{header}{_serialize_segment(segment)}"
                if len(serialized) < maximum:
                    add(serialized)
                    continue
                if len(serialized) >= maximum:
                    if current:
                        chunks.append(current)
                        current = ""
                    chunks.extend(_split_oversized_segment(header, segment, maximum))
    if current:
        chunks.append(current)
    if not chunks:
        raise ValueError("parsed documents contain no segments")
    return tuple(chunks)


def _heading_sections(segments: tuple[ParsedSegment, ...]) -> tuple[tuple[ParsedSegment, ...], ...]:
    sections: list[tuple[ParsedSegment, ...]] = []
    current: list[ParsedSegment] = []
    for segment in segments:
        if segment.kind is SegmentKind.HEADING and current:
            sections.append(tuple(current))
            current = []
        current.append(segment)
    if current:
        sections.append(tuple(current))
    return tuple(sections)


def _source_header(source: SourceDocument) -> str:
    return (
        f"source_title: {source.title}\nsource_role: {source.role}\n"
        f"source_id: {source.document.source_id}\n"
    )


def _serialize_segment(segment: ParsedSegment, text: str | None = None) -> str:
    asset_keys = ", ".join(segment.asset_keys) if segment.asset_keys else "none"
    return (
        f"locator: {segment.locator.label}\nsegment_key: {segment.key}\n"
        f"nearby_asset_keys: {asset_keys}\ntext: {segment.text if text is None else text}\n"
    )


def _split_oversized_segment(header: str, segment: ParsedSegment, maximum: int) -> tuple[str, ...]:
    base = f"{header}{_serialize_segment(segment, '')}"
    room = maximum - len(base) - 1
    if room < 1:
        raise ValueError("max_input_characters is too small to serialize source metadata")
    return tuple(
        f"{header}{_serialize_segment(segment, segment.text[offset : offset + room])}"
        for offset in range(0, len(segment.text), room)
    )


def _normalized_identifier(value: str | None) -> str | None:
    if value is None:
        return None
    normalized_number = normalize_identifier(value)
    if normalized_number is not None:
        return normalized_number
    compact = " ".join(value.casefold().split())
    return compact.rstrip(".:") or None


_LEADING_QUESTION_NUMBER = re.compile(r"^\s*(\d{1,3})\s*[.)]\s+\S")


def _sequential_question_identifiers(sources: tuple[SourceDocument, ...]) -> tuple[str, ...]:
    """Return a conservative explicit-number sequence from canonical source text.

    This is a publication safety check, not a second parser: it only activates
    when at least three consecutive leading question numbers are visible.
    """
    numbers: set[int] = set()
    for source in sources:
        for segment in source.document.segments:
            match = _LEADING_QUESTION_NUMBER.match(segment.text)
            if match is not None:
                numbers.add(int(match.group(1)))
    longest: tuple[int, ...] = ()
    current: list[int] = []
    for number in sorted(numbers):
        if current and number != current[-1] + 1:
            if len(current) > len(longest):
                longest = tuple(current)
            current = []
        current.append(number)
    if len(current) > len(longest):
        longest = tuple(current)
    return tuple(str(number) for number in longest) if len(longest) >= 3 else ()


def _identifier_range(identifiers: tuple[str, ...]) -> str:
    if len(identifiers) == 1:
        return identifiers[0]
    if all(
        int(right) == int(left) + 1
        for left, right in zip(identifiers, identifiers[1:], strict=False)
    ):
        return f"{identifiers[0]} through {identifiers[-1]}"
    return ", ".join(identifiers)
