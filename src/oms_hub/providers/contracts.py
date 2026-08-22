"""Shared contracts for grounded-learning providers."""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol


class AuthorityClass(StrEnum):
    COURSE_MATERIAL = "course_material"
    PUBLISHED_JOURNAL = "published_journal"
    GENERATED_ARTIFACT = "generated_artifact"
    QUESTION_STYLE_REFERENCE = "question_style_reference"


class TruthMode(StrEnum):
    COURSE_ONLY = "course_only"
    COURSE_AND_LITERATURE = "course_and_literature"
    LITERATURE_ONLY = "literature_only"


@dataclass(frozen=True, slots=True)
class RetrievalScope:
    course_id: str
    exam_id: str | None
    lecture_ids: tuple[str, ...]
    truth_mode: TruthMode
    source_revision_ids: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class EvidenceRef:
    evidence_id: str
    source_revision_id: str
    authority_class: AuthorityClass
    locator_kind: str
    locator_value: str
    excerpt: str
    checksum: str


@dataclass(frozen=True, slots=True)
class RetrievalRequest:
    query: str
    scope: RetrievalScope
    maximum_evidence: int = 12


@dataclass(frozen=True, slots=True)
class RetrievalResult:
    evidence: tuple[EvidenceRef, ...]
    provider_request_id: str
    insufficient_evidence: bool


@dataclass(frozen=True, slots=True)
class ProviderHealth:
    provider: str
    ready: bool
    detail: str
    checked_at_iso: str


class RetrievalProvider(Protocol):
    async def retrieve(self, request: RetrievalRequest) -> RetrievalResult: ...

    async def health(self) -> ProviderHealth: ...


class GroundedAnswerRequest(Protocol):
    """Marker boundary for a concrete grounded-answer request.

    Field-level Ask request contracts are introduced by the Ask workstream.
    """


class AnswerEventType(StrEnum):
    STATUS = "status"
    DELTA = "delta"
    CITATIONS = "citations"
    DONE = "done"
    ERROR = "error"


@dataclass(frozen=True, slots=True)
class AnswerEvent:
    event_type: AnswerEventType
    payload: dict[str, object]


class GroundedAnswerProvider(Protocol):
    def stream_answer(self, request: GroundedAnswerRequest) -> AsyncIterator[AnswerEvent]: ...
