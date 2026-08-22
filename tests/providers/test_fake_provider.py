"""Behavioral tests for deterministic grounded-learning provider fakes."""

import asyncio

import pytest

from oms_hub.providers import (
    AnswerEvent,
    AnswerEventType,
    FakeGroundedAnswerProvider,
    FakeRetrievalProvider,
    ProviderHealth,
    RetrievalRequest,
    RetrievalResult,
    RetrievalScope,
    TruthMode,
)


class _Request:
    pass


def _request(query: str = "Is HIT prothrombotic?") -> RetrievalRequest:
    return RetrievalRequest(query, RetrievalScope("heme", "e2", ("l13",), TruthMode.COURSE_ONLY))


def _events(provider: FakeGroundedAnswerProvider, request: _Request) -> list[AnswerEvent]:
    async def collect() -> list[AnswerEvent]:
        return [event async for event in provider.stream_answer(request)]

    return asyncio.run(collect())


def test_retrieval_fake_returns_evidence_and_captures_exact_request() -> None:
    provider = FakeRetrievalProvider.from_text("ev_1", "sr_1", "HIT is prothrombotic.")
    request = _request()

    result = asyncio.run(provider.retrieve(request))

    assert [item.evidence_id for item in result.evidence] == ["ev_1"]
    assert result.insufficient_evidence is False
    assert provider.requests == [request]
    assert provider.requests[0] is request


def test_retrieval_fake_consumes_results_fifo_and_records_exhaustion() -> None:
    provider = FakeRetrievalProvider(
        [RetrievalResult((), "one", False), RetrievalResult((), "two", True)]
    )
    first, second, exhausted = _request("first"), _request("second"), _request("third")

    assert asyncio.run(provider.retrieve(first)).provider_request_id == "one"
    assert asyncio.run(provider.retrieve(second)).provider_request_id == "two"
    with pytest.raises(AssertionError, match="fake retrieval response queue exhausted"):
        asyncio.run(provider.retrieve(exhausted))
    assert provider.requests == [first, second, exhausted]


def test_retrieval_from_text_checksum_and_health_are_deterministic_and_configurable() -> None:
    provider = FakeRetrievalProvider.from_text("ev_1", "sr_1", "HIT is prothrombotic.")
    health = ProviderHealth("custom", False, "paused", "1970-01-02T00:00:00+00:00")
    custom = FakeRetrievalProvider([], health=health)

    assert provider.responses[0].evidence[0].checksum == (
        "sha256:07a6e6bb6f8cdc84f403d079b95a503d3bc62d903dbacf38823e67f92cc2b92d"
    )
    assert asyncio.run(provider.health()) == ProviderHealth(
        "fake", True, "configured fake retrieval provider", "1970-01-01T00:00:00+00:00"
    )
    assert asyncio.run(custom.health()) is health


def test_retrieval_from_text_preserves_an_explicit_empty_checksum() -> None:
    provider = FakeRetrievalProvider.from_text("ev_1", "sr_1", "HIT is prothrombotic.", checksum="")

    assert provider.responses[0].evidence[0].checksum == ""


def test_answer_fake_streams_events_fifo_and_captures_exact_request() -> None:
    first = AnswerEvent(AnswerEventType.STATUS, {"message": "retrieving"})
    second = AnswerEvent(AnswerEventType.DELTA, {"text": "HIT"})
    provider = FakeGroundedAnswerProvider(((first,), (second,)))
    first_request, second_request = _Request(), _Request()

    assert _events(provider, first_request) == [first]
    assert _events(provider, second_request) == [second]
    assert provider.requests == [first_request, second_request]
    assert provider.requests[0] is first_request


def test_answer_fake_exhaustion_occurs_at_invocation_and_records_request() -> None:
    provider = FakeGroundedAnswerProvider()
    request = _Request()

    with pytest.raises(AssertionError, match="fake answer response queue exhausted"):
        provider.stream_answer(request)
    assert provider.requests == [request]


def test_answer_fakes_copy_shared_configured_sequences_and_request_lists() -> None:
    event = AnswerEvent(AnswerEventType.DONE, {})
    configured = ((event,),)
    first = FakeGroundedAnswerProvider(configured)
    second = FakeGroundedAnswerProvider(configured)
    first_request, second_request = _Request(), _Request()

    assert _events(first, first_request) == [event]
    assert first.responses == []
    assert second.responses == [(event,)]
    assert first.requests == [first_request]
    assert second.requests == []
    assert first.responses is not second.responses
    assert first.requests is not second.requests
    assert _events(second, second_request) == [event]
    assert second.requests == [second_request]


def test_fake_instances_and_configured_inputs_do_not_share_queues() -> None:
    responses = [RetrievalResult((), "one", False)]
    first = FakeRetrievalProvider(responses)
    second = FakeRetrievalProvider(responses)
    responses.clear()
    answer_events = [AnswerEvent(AnswerEventType.DONE, {})]
    answer = FakeGroundedAnswerProvider((answer_events,))
    answer_events.clear()

    assert asyncio.run(first.retrieve(_request())).provider_request_id == "one"
    assert asyncio.run(second.retrieve(_request())).provider_request_id == "one"
    assert _events(answer, _Request()) == [AnswerEvent(AnswerEventType.DONE, {})]
    assert first.requests is not second.requests
