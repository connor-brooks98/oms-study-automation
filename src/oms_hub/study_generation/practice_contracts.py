"""Strict structured-output contracts for imported practice questions."""

from collections.abc import Collection

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class ExtractedQuestion(BaseModel):
    """One question as reported by the extraction provider."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    original_identifier: str | None = Field(default=None, max_length=100)
    stem: str = Field(min_length=1, max_length=10_000)
    choices: tuple[str, ...] = Field(min_length=2, max_length=8)
    supplied_correct_index: int | None = Field(default=None, ge=0, le=7)
    rationale: str | None = Field(default=None, max_length=20_000)
    source_segment_keys: tuple[str, ...] = Field(min_length=1, max_length=50)
    candidate_asset_keys: tuple[str, ...] = Field(default=(), max_length=50)
    confidence: float = Field(ge=0.0, le=1.0)

    @field_validator("choices", "source_segment_keys", "candidate_asset_keys", mode="before")
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
    source_segment_keys: tuple[str, ...] = Field(min_length=1, max_length=50)

    @field_validator("source_segment_keys", mode="before")
    @classmethod
    def lists_become_immutable_tuples(cls, values: object) -> object:
        return tuple(values) if isinstance(values, list) else values


class ExtractionPayload(BaseModel):
    """The complete response requested from the extraction provider."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    questions: tuple[ExtractedQuestion, ...] = Field(min_length=1, max_length=500)
    answers: tuple[ExtractedAnswer, ...] = Field(default=(), max_length=500)

    @field_validator("questions", "answers", mode="before")
    @classmethod
    def lists_become_immutable_tuples(cls, values: object) -> object:
        return tuple(values) if isinstance(values, list) else values


def validate_source_references(
    payload: ExtractionPayload,
    *,
    segment_keys: Collection[str],
    asset_keys: Collection[str],
) -> None:
    """Reject citations to source material absent from the canonical input."""

    known_segments = set(segment_keys)
    known_assets = set(asset_keys)
    for question in payload.questions:
        _validate_known_keys(question.source_segment_keys, known_segments, "source segment")
        _validate_known_keys(question.candidate_asset_keys, known_assets, "candidate asset")
    for answer in payload.answers:
        _validate_known_keys(answer.source_segment_keys, known_segments, "source segment")


def _validate_known_keys(
    values: tuple[str, ...], known_values: set[str], label: str
) -> None:
    unknown = sorted(set(values) - known_values)
    if unknown:
        joined = ", ".join(repr(value) for value in unknown)
        raise ValueError(f"{label} reference is unknown: {joined}")
