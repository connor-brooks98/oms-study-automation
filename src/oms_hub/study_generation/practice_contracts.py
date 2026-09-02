"""Strict structured-output contracts for imported practice questions."""

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from oms_hub.document_processing.domain import ParsedDocument


class SegmentCitation(BaseModel):
    """A document-qualified citation to a canonical parsed segment."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    source_id: str = Field(min_length=1, max_length=200)
    segment_key: str = Field(min_length=1, max_length=500)


class AssetCitation(BaseModel):
    """A document-qualified citation to a canonical parsed asset."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    source_id: str = Field(min_length=1, max_length=200)
    asset_key: str = Field(min_length=1, max_length=500)


class ExtractedQuestion(BaseModel):
    """One question as reported by the extraction provider."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    original_identifier: str | None = Field(default=None, max_length=100)
    stem: str = Field(min_length=1, max_length=10_000)
    choices: tuple[str, ...] = Field(min_length=2, max_length=8)
    supplied_correct_index: int | None = Field(default=None, ge=0, le=7)
    rationale: str | None = Field(default=None, max_length=20_000)
    source_segments: tuple[SegmentCitation, ...] = Field(min_length=1, max_length=50)
    candidate_assets: tuple[AssetCitation, ...] = Field(default=(), max_length=50)
    confidence: float = Field(ge=0.0, le=1.0)

    @field_validator("choices", "source_segments", "candidate_assets", mode="before")
    @classmethod
    def lists_become_immutable_tuples(cls, values: object) -> object:
        return tuple(values) if isinstance(values, list) else values

    @field_validator("choices")
    @classmethod
    def choices_are_distinct(cls, choices: tuple[str, ...]) -> tuple[str, ...]:
        if len({choice.casefold() for choice in choices}) != len(choices):
            raise ValueError("choices must be distinct after case-folding")
        return choices

    @model_validator(mode="after")
    def supplied_correct_index_is_in_choices(self) -> "ExtractedQuestion":
        if self.supplied_correct_index is not None and self.supplied_correct_index >= len(
            self.choices
        ):
            raise ValueError("supplied_correct_index must identify an available choice")
        return self


class ExtractedAnswer(BaseModel):
    """One answer-key entry as reported by the extraction provider."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    original_identifier: str | None = Field(default=None, max_length=100)
    correct_index: int = Field(ge=0, le=7)
    rationale: str | None = Field(default=None, max_length=20_000)
    source_segments: tuple[SegmentCitation, ...] = Field(min_length=1, max_length=50)

    @field_validator("source_segments", mode="before")
    @classmethod
    def lists_become_immutable_tuples(cls, values: object) -> object:
        return tuple(values) if isinstance(values, list) else values


class ExtractionPayload(BaseModel):
    """The complete response requested from the extraction provider."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    questions: tuple[ExtractedQuestion, ...] = Field(max_length=500)
    answers: tuple[ExtractedAnswer, ...] = Field(default=(), max_length=500)

    @field_validator("questions", "answers", mode="before")
    @classmethod
    def lists_become_immutable_tuples(cls, values: object) -> object:
        return tuple(values) if isinstance(values, list) else values


def validate_source_references(
    payload: ExtractionPayload,
    *,
    documents: tuple[ParsedDocument, ...],
) -> None:
    """Reject citations absent from the cited canonical document.

    Segment and asset keys are intentionally resolved only after their source ID,
    so repeated local keys from multiple files can never validate each other.
    """

    documents_by_id = {document.source_id: document for document in documents}
    if len(documents_by_id) != len(documents):
        raise ValueError("canonical documents must have unique source IDs")
    for question in payload.questions:
        _validate_segments(question.source_segments, documents_by_id)
        _validate_assets(question.candidate_assets, documents_by_id)
    for answer in payload.answers:
        _validate_segments(answer.source_segments, documents_by_id)


def _validate_segments(
    citations: tuple[SegmentCitation, ...], documents: dict[str, ParsedDocument]
) -> None:
    for citation in citations:
        document = documents.get(citation.source_id)
        if document is None or citation.segment_key not in {
            segment.key for segment in document.segments
        }:
            raise ValueError(
                "source segment reference is unknown in cited document: "
                f"{citation.source_id!r}/{citation.segment_key!r}"
            )


def _validate_assets(
    citations: tuple[AssetCitation, ...], documents: dict[str, ParsedDocument]
) -> None:
    for citation in citations:
        document = documents.get(citation.source_id)
        if document is None or citation.asset_key not in {asset.key for asset in document.assets}:
            raise ValueError(
                "candidate asset reference is unknown in cited document: "
                f"{citation.source_id!r}/{citation.asset_key!r}"
            )
