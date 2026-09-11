"""Validated chat inputs and immutable repository views."""

from dataclasses import dataclass as plain_dataclass
from typing import Annotated, Literal
from uuid import UUID

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    model_validator,
)
from pydantic.dataclasses import dataclass

from oms_hub.anki.sources import SourcePassage

ChatMode = Literal["lecture", "medical_reference", "general"]
RequestState = Literal["pending", "running", "completed", "failed", "interrupted"]
OwnerId = Annotated[
    str, StringConstraints(strict=True, min_length=1, max_length=320, pattern=r"^\S(?:.*\S)?$")
]


def _uuid(value: str) -> str:
    if str(UUID(value)) != value:
        raise ValueError("ID must be a canonical UUID")
    return value


UuidId = Annotated[str, StringConstraints(strict=True), AfterValidator(_uuid)]
RevisionIds = Annotated[tuple[Annotated[int, Field(strict=True, gt=0)], ...], Field(max_length=100)]
_TEXT = ConfigDict(extra="forbid")


@dataclass(config=_TEXT, frozen=True)
class ChatRequest:
    request_id: UuidId
    owner_id: OwnerId
    conversation_id: UuidId
    mode: ChatMode
    question: Annotated[
        str, StringConstraints(strict=True, min_length=1, max_length=20000, pattern=r"\S")
    ]
    revision_ids: RevisionIds

    @model_validator(mode="after")
    def valid_scope(self) -> "ChatRequest":
        if len(set(self.revision_ids)) != len(self.revision_ids):
            raise ValueError("duplicate source revision IDs")
        if bool(self.revision_ids) != (self.mode == "lecture"):
            raise ValueError("revision IDs must be present only for lecture mode")
        return self


@dataclass(config=_TEXT, frozen=True)
class ChatAnswer:
    status: Literal["answered", "no_support", "unavailable"]
    text: Annotated[
        str, StringConstraints(strict=True, min_length=1, max_length=24000, pattern=r"\S")
    ]
    citation_ids: Annotated[
        tuple[Annotated[str, StringConstraints(strict=True, min_length=1, max_length=200)], ...],
        Field(max_length=100),
    ]


class SourceSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    revision_id: int = Field(gt=0)
    lecture_id: int = Field(gt=0)
    subject: str
    exam_number: int
    kind: Literal["slides", "transcripts"]
    source_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    derived_sha256: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")


def validate_answer(
    answer: ChatAnswer, allowed_ids: set[str], *, mode: ChatMode = "lecture"
) -> None:
    citations = set(answer.citation_ids)
    if len(citations) != len(answer.citation_ids) or not citations <= allowed_ids:
        raise ValueError("invalid citation membership")
    if answer.status != "answered" and citations:
        raise ValueError("unsupported answer cannot carry citations")
    if answer.status == "answered" and mode == "lecture" and not citations:
        raise ValueError("lecture answer requires a citation")
    if mode == "general" and citations:
        raise ValueError("general answer cannot carry source citations")


@plain_dataclass(frozen=True)
class Conversation:
    id: str
    owner_id: str
    mode: ChatMode
    sources: tuple[SourceSnapshot, ...]
    cleared_at: str | None


@plain_dataclass(frozen=True)
class StoredRequest:
    request: ChatRequest
    model: str
    evidence: tuple[SourcePassage, ...]
    history_request_ids: tuple[str, ...]
    state: RequestState
    provider_phase: str | None
    thread_id: str | None
    turn_id: str | None
    answer: ChatAnswer | None
    error_code: str | None


@plain_dataclass(frozen=True)
class BeginResult:
    record: StoredRequest
    created: bool
