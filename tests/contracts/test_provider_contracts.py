"""Shared grounded-learning provider contract tests."""

import asyncio
from dataclasses import FrozenInstanceError

import pytest

from oms_hub.providers import (
    AnswerEvent,
    AnswerEventType,
    AuthorityClass,
    EvidenceRef,
    FakeGroundedAnswerProvider,
    FakeRetrievalProvider,
    GroundedAnswerProvider,
    GroundedAnswerRequest,
    ProviderHealth,
    ProviderRegistry,
    RetrievalProvider,
    RetrievalRequest,
    RetrievalResult,
    RetrievalScope,
    TruthMode,
)


class _AnswerRequest:
    pass


def _retrieval_result(identifier: str) -> RetrievalResult:
    return RetrievalResult((), identifier, False)


def _retrieval_provider(provider: RetrievalProvider) -> RetrievalProvider:
    return provider


def _answer_provider(provider: GroundedAnswerProvider) -> GroundedAnswerProvider:
    return provider


def test_enum_values_are_exact() -> None:
    assert [item.value for item in AuthorityClass] == [
        "course_material",
        "published_journal",
        "generated_artifact",
        "question_style_reference",
    ]
    assert [item.value for item in TruthMode] == [
        "course_only",
        "course_and_literature",
        "literature_only",
    ]
    assert [item.value for item in AnswerEventType] == [
        "status",
        "delta",
        "citations",
        "done",
        "error",
    ]


def test_retrieval_contracts_are_hash_stable_and_positional() -> None:
    scope = RetrievalScope("heme", "e2", ("l13",), TruthMode.COURSE_ONLY)
    same_scope = RetrievalScope("heme", "e2", ("l13",), TruthMode.COURSE_ONLY)
    request = RetrievalRequest("why is PTT prolonged?", scope)

    assert scope.source_revision_ids == ()
    assert scope == same_scope
    assert hash(scope) == hash(same_scope)
    assert request.maximum_evidence == 12
    assert scope.truth_mode.value == "course_only"


def test_evidence_ref_and_provider_health_preserve_fields() -> None:
    evidence = EvidenceRef(
        "ev_abc",
        "sr_abc",
        AuthorityClass.COURSE_MATERIAL,
        "slide",
        "42",
        "Factor VIII is in the intrinsic pathway.",
        "sha256:abc",
    )
    health = ProviderHealth("fake", True, "ready", "1970-01-01T00:00:00+00:00")

    assert evidence.authority_class is AuthorityClass.COURSE_MATERIAL
    assert (evidence.locator_kind, evidence.locator_value, evidence.excerpt, evidence.checksum) == (
        "slide",
        "42",
        "Factor VIII is in the intrinsic pathway.",
        "sha256:abc",
    )
    assert health == ProviderHealth("fake", True, "ready", "1970-01-01T00:00:00+00:00")


def test_contract_dataclasses_are_frozen() -> None:
    scope = RetrievalScope("heme", None, (), TruthMode.COURSE_ONLY)

    with pytest.raises(FrozenInstanceError):
        scope.course_id = "new-course"  # type: ignore[misc]


def test_answer_event_serializes_as_str_enum() -> None:
    event = AnswerEvent(AnswerEventType.DELTA, {"text": "HIT"})

    assert event.event_type == "delta"
    assert event.payload == {"text": "HIT"}


def test_public_imports_and_protocol_conformance() -> None:
    retrieval = _retrieval_provider(FakeRetrievalProvider([_retrieval_result("one")]))
    answer = _answer_provider(FakeGroundedAnswerProvider.from_events())
    request: GroundedAnswerRequest = _AnswerRequest()

    assert callable(retrieval.retrieve)
    assert callable(answer.stream_answer)
    assert isinstance(request, _AnswerRequest)


def test_registry_separates_provider_categories() -> None:
    registry = ProviderRegistry()
    retrieval = FakeRetrievalProvider([_retrieval_result("one")])
    answer = FakeGroundedAnswerProvider.from_events()

    registry.register_retrieval("shared", retrieval)
    registry.register_answer("shared", answer)

    assert registry.get_retrieval("shared") is retrieval
    assert registry.get_answer("shared") is answer


@pytest.mark.parametrize("name", ["", " \t "])
def test_registry_rejects_blank_names(name: str) -> None:
    registry = ProviderRegistry()

    with pytest.raises(ValueError, match="provider name"):
        registry.register_retrieval(name, FakeRetrievalProvider([]))


def test_registry_rejects_duplicate_registration_in_each_category() -> None:
    registry = ProviderRegistry()
    registry.register_retrieval("fake", FakeRetrievalProvider([]))
    registry.register_answer("fake", FakeGroundedAnswerProvider.from_events())

    with pytest.raises(ValueError, match="fake"):
        registry.register_retrieval("fake", FakeRetrievalProvider([]))
    with pytest.raises(ValueError, match="fake"):
        registry.register_answer("fake", FakeGroundedAnswerProvider.from_events())


def test_registry_unknown_errors_are_category_specific_and_sorted() -> None:
    registry = ProviderRegistry()
    registry.register_retrieval("zeta", FakeRetrievalProvider([]))
    registry.register_retrieval("alpha", FakeRetrievalProvider([]))
    registry.register_answer("beta", FakeGroundedAnswerProvider.from_events())
    registry.register_answer("alpha", FakeGroundedAnswerProvider.from_events())

    with pytest.raises(KeyError, match="missing") as retrieval_error:
        registry.get_retrieval("missing")
    with pytest.raises(KeyError, match="missing") as answer_error:
        registry.get_answer("missing")

    assert "alpha, zeta" in str(retrieval_error.value)
    assert "alpha, beta" in str(answer_error.value)


def test_registry_providers_are_callable() -> None:
    provider = FakeRetrievalProvider([_retrieval_result("one")])
    registry = ProviderRegistry()
    registry.register_retrieval("fake", provider)
    request = RetrievalRequest("query", RetrievalScope("course", None, (), TruthMode.COURSE_ONLY))

    result = asyncio.run(registry.get_retrieval("fake").retrieve(request))

    assert result.provider_request_id == "one"
