from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Annotated, Literal, Self

from pydantic import (
    AfterValidator,
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    StrictStr,
    field_validator,
    model_validator,
)

Source = Literal["uworld", "truelearn", "amboss", "study_hub", "user"]
Result = Literal["correct", "incorrect", "omitted", "unknown"]
Axis = Literal["system", "discipline", "topic", "exam", "course", "lecture", "objective"]


def _identity(value: str) -> str:
    if not value.strip() or value != value.strip():
        raise ValueError("Identity must be nonblank without surrounding whitespace")
    return value


ExternalId = Annotated[StrictStr, Field(min_length=1, max_length=200), AfterValidator(_identity)]
Product = Annotated[StrictStr, Field(min_length=1, max_length=100), AfterValidator(_identity)]
Label = Annotated[StrictStr, Field(min_length=1, max_length=300)]


class _Input(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True, allow_inf_nan=False)


class QuestionKey(_Input):
    source: Source
    product: Product
    question_id: ExternalId


class TopicLabel(_Input):
    axis: Axis
    label: Label
    canonical_id: ExternalId | None = None
    method: Literal["unmapped", "exact", "user", "model"] = "unmapped"
    confidence: Annotated[float, Field(ge=0, le=1)] | None = None
    review_state: Literal["pending", "accepted", "rejected"] = "pending"


class ImportRow(_Input):
    question_id: ExternalId
    attempt_id: ExternalId | None = None
    result: Result = "unknown"
    occurred_at: AwareDatetime | None = None
    elapsed_ms: Annotated[int, Field(ge=0)] | None = None
    user_note: Annotated[StrictStr, Field(max_length=20_000)] = ""
    tags: tuple[Label, ...] = ()
    topics: tuple[TopicLabel, ...] = ()
    question: dict[str, object] | None = None

    @field_validator("question")
    @classmethod
    def body_has_finite_numbers(cls, value: dict[str, object] | None) -> dict[str, object] | None:
        # Validate before Pydantic's JSON dump can replace nonfinite values with null.
        json.dumps(value, allow_nan=False)
        return value

    @model_validator(mode="after")
    def attempt_identity_required(self) -> Self:
        if self.attempt_id is None and (
            self.result != "unknown" or self.occurred_at is not None or self.elapsed_ms is not None
        ):
            raise ValueError("Attempt facts require attempt_id")
        return self


class Provenance(_Input):
    kind: Literal["user_results", "user_notes", "authorized_question_export"]
    description: StrictStr


class ImportEnvelope(_Input):
    schema_version: Literal[1]
    source: Source
    product: Product
    export_id: ExternalId
    provenance: Provenance
    rows: Annotated[tuple[ImportRow, ...], Field(min_length=1, max_length=10_000)]

    @field_validator("schema_version", mode="before")
    @classmethod
    def version_is_integer(cls, value: object) -> object:
        if type(value) is not int:
            raise ValueError("schema_version must be the integer 1")
        return value

    @model_validator(mode="after")
    def content_provenance(self) -> Self:
        if self.provenance.kind != "authorized_question_export" and any(
            row.question is not None for row in self.rows
        ):
            raise ValueError("Question bodies require authorized_question_export provenance")
        return self


@dataclass(frozen=True)
class RowIssue:
    row: int  # One-based original row number.
    code: str
    detail: str


@dataclass(frozen=True)
class ImportPreview:
    digest: str
    envelope: ImportEnvelope
    issues: tuple[RowIssue, ...]
    ready_question_rows: tuple[int, ...]


@dataclass(frozen=True)
class ImportReceipt:
    import_id: str
    inserted_questions: int
    inserted_attempts: int
    duplicate_rows: int
    conflicts: tuple[RowIssue, ...]


@dataclass(frozen=True)
class AttemptFact:
    id: int
    learner_id: str
    key: QuestionKey
    attempt_id: str
    result: Result
    occurred_at: datetime | None
    elapsed_ms: int | None
    imported_at: datetime
    import_id: str
    topics: tuple[TopicLabel, ...]
