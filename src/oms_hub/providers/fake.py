"""Deterministic test doubles for grounded-learning provider contracts."""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterable
from hashlib import sha256

from oms_hub.providers.contracts import (
    AnswerEvent,
    AuthorityClass,
    GroundedAnswerRequest,
    ProviderHealth,
    RetrievalRequest,
    RetrievalResult,
)


class FakeRetrievalProvider:
    """FIFO retrieval fake with exact request capture."""

    def __init__(
        self,
        responses: Iterable[RetrievalResult],
        *,
        health: ProviderHealth | None = None,
    ) -> None:
        self.responses = list(responses)
        self.requests: list[RetrievalRequest] = []
        self._health = health or ProviderHealth(
            provider="fake",
            ready=True,
            detail="configured fake retrieval provider",
            checked_at_iso="1970-01-01T00:00:00+00:00",
        )

    @classmethod
    def from_text(
        cls,
        evidence_id: str,
        source_revision_id: str,
        text: str,
        *,
        authority_class: AuthorityClass = AuthorityClass.COURSE_MATERIAL,
        locator_kind: str = "text",
        locator_value: str = "1",
        provider_request_id: str = "fake-retrieval-1",
        checksum: str | None = None,
    ) -> FakeRetrievalProvider:
        from oms_hub.providers.contracts import EvidenceRef

        evidence = EvidenceRef(
            evidence_id=evidence_id,
            source_revision_id=source_revision_id,
            authority_class=authority_class,
            locator_kind=locator_kind,
            locator_value=locator_value,
            excerpt=text,
            checksum=checksum or f"sha256:{sha256(text.encode('utf-8')).hexdigest()}",
        )
        return cls((RetrievalResult((evidence,), provider_request_id, False),))

    async def retrieve(self, request: RetrievalRequest) -> RetrievalResult:
        self.requests.append(request)
        if not self.responses:
            raise AssertionError("fake retrieval response queue exhausted")
        return self.responses.pop(0)

    async def health(self) -> ProviderHealth:
        return self._health


class FakeGroundedAnswerProvider:
    """FIFO answer-stream fake with invocation-time exhaustion failures."""

    def __init__(self, responses: Iterable[Iterable[AnswerEvent]] = ()) -> None:
        self.responses = [tuple(events) for events in responses]
        self.requests: list[GroundedAnswerRequest] = []

    @classmethod
    def from_events(cls, *events: AnswerEvent) -> FakeGroundedAnswerProvider:
        return cls((events,))

    def stream_answer(self, request: GroundedAnswerRequest) -> AsyncIterator[AnswerEvent]:
        self.requests.append(request)
        if not self.responses:
            raise AssertionError("fake answer response queue exhausted")
        events = self.responses.pop(0)

        async def stream() -> AsyncIterator[AnswerEvent]:
            for event in events:
                yield event

        return stream()
