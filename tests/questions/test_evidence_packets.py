from __future__ import annotations

import asyncio
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path
from typing import Any, cast

import pytest
from sqlalchemy import text as sql_text

from oms_hub.db import Database
from oms_hub.knowledge.ids import sha256_text
from oms_hub.knowledge.ids import source_revision_id as make_source_revision_id
from oms_hub.knowledge.models import (
    EvidenceLocator,
    EvidenceLocatorKind,
    EvidenceUnit,
    SourceRevision,
    SourceRevisionState,
)
from oms_hub.knowledge.repository import KnowledgeRepository
from oms_hub.providers.contracts import (
    AuthorityClass,
    EvidenceRef,
    ProviderHealth,
    RetrievalRequest,
    RetrievalResult,
    RetrievalScope,
    TruthMode,
)
from oms_hub.questions.evidence_packets import (
    QuestionEvidenceError,
    QuestionEvidencePacket,
    QuestionEvidencePacketBuilder,
    QuestionGenerationRequest,
    QuestionObjective,
)
from oms_hub.questions.models import QuestionMode


class FakeRetrievalProvider:
    def __init__(
        self,
        default: RetrievalResult,
        *,
        by_query: dict[str, RetrievalResult] | None = None,
    ) -> None:
        self.default = default
        self.by_query = by_query or {}
        self.requests: list[RetrievalRequest] = []

    async def retrieve(self, request: RetrievalRequest) -> RetrievalResult:
        self.requests.append(request)
        return self.by_query.get(request.query, self.default)

    async def health(self) -> ProviderHealth:
        return ProviderHealth(
            provider="synthetic",
            ready=True,
            detail="offline fixture",
            checked_at_iso="2026-08-25T12:00:00+00:00",
        )


@pytest.fixture
def repository(tmp_path: Path) -> Iterator[KnowledgeRepository]:
    database = Database(f"sqlite:///{tmp_path / 'questions.db'}")
    repository = KnowledgeRepository(database)
    repository.initialize()
    yield repository
    database.close()


def _scope(*, truth_mode: TruthMode = TruthMode.COURSE_ONLY) -> RetrievalScope:
    return RetrievalScope(
        course_id="course-1",
        exam_id="exam-1",
        lecture_ids=("lecture-1",),
        truth_mode=truth_mode,
    )


def _request(
    *,
    objectives: tuple[QuestionObjective, ...] = (
        QuestionObjective("obj-a", "Objective A"),
    ),
    mode: QuestionMode = QuestionMode.BOARD_STYLE,
    scope: RetrievalScope | None = None,
    prior: tuple[str, ...] = ("prior-concept",),
    forbidden: tuple[str, ...] = ("forbidden-concept",),
) -> QuestionGenerationRequest:
    return QuestionGenerationRequest(
        objectives=objectives,
        mode=mode,
        difficulty=3,
        scope=scope or _scope(),
        correct_answer_concept="Factor VIII deficiency",
        correct_answer_concept_signature="factor-viii-deficiency",
        prior_tested_concept_signatures=prior,
        forbidden_repeat_signatures=forbidden,
        style_constraints=("single best answer", "clinical vignette"),
    )


def _add_evidence(
    repository: KnowledgeRepository,
    *,
    evidence_id: str,
    text: str = "Factor VIII deficiency causes hemophilia A.",
    authority: AuthorityClass = AuthorityClass.COURSE_MATERIAL,
    revision_state: SourceRevisionState = SourceRevisionState.READY,
    retired_at: str | None = None,
    source_priority: int = 0,
    course_id: str | None = "course-1",
    exam_id: str | None = "exam-1",
    lecture_id: str | None = "lecture-1",
    locator_kind: EvidenceLocatorKind = EvidenceLocatorKind.SLIDE,
    locator_value: str | None = None,
) -> EvidenceRef:
    source_document_id = f"source-{evidence_id}"
    file_sha256 = sha256_text(f"file-{evidence_id}")
    revision_id = make_source_revision_id(source_document_id, file_sha256)
    repository.create_source(source_document_id, authority)
    stored_state = (
        SourceRevisionState.READY
        if revision_state is SourceRevisionState.RETIRED
        else revision_state
    )
    repository.create_revision(
        SourceRevision(
            source_document_id=source_document_id,
            source_revision_id=revision_id,
            file_sha256=file_sha256,
            state=stored_state,
        )
    )
    unit = EvidenceUnit(
        evidence_id=evidence_id,
        source_revision_id=revision_id,
        authority_class=authority,
        course_id=course_id,
        exam_id=exam_id,
        lecture_id=lecture_id,
        locator=EvidenceLocator(locator_kind, locator_value or evidence_id),
        normalized_text=text,
        content_sha256=sha256_text(text),
        source_priority=source_priority,
        retired_at=retired_at,
    )
    repository.put_evidence_units(revision_id, (unit,))
    if revision_state is SourceRevisionState.RETIRED:
        with repository.database.engine.begin() as connection:
            connection.execute(
                sql_text("UPDATE source_revisions SET state = 'retired' WHERE id = :id"),
                {"id": revision_id},
            )
    return EvidenceRef(
        evidence_id=evidence_id,
        source_revision_id=revision_id,
        authority_class=authority,
        locator_kind=unit.locator.kind.value,
        locator_value=unit.locator.value,
        excerpt=text,
        checksum=f"sha256:{unit.content_sha256}",
    )


def _result(*refs: EvidenceRef, insufficient: bool = False) -> RetrievalResult:
    return RetrievalResult(
        evidence=tuple(refs),
        provider_request_id="synthetic-request",
        insufficient_evidence=insufficient,
    )


def _build(
    builder: QuestionEvidencePacketBuilder,
    request: QuestionGenerationRequest,
) -> QuestionEvidencePacket:
    return asyncio.run(builder.build(request))


def test_builds_policy_checked_packet_with_required_metadata(
    repository: KnowledgeRepository,
) -> None:
    ref = _add_evidence(repository, evidence_id="ev-course")
    provider = FakeRetrievalProvider(_result(ref))

    packet = _build(QuestionEvidencePacketBuilder(provider, repository), _request())

    assert packet.objective_ids == ("obj-a",)
    assert packet.objective_display_names == ("Objective A",)
    assert packet.mode is QuestionMode.BOARD_STYLE
    assert packet.difficulty == 3
    assert packet.prior_tested_concept_signatures == ("prior-concept",)
    assert packet.forbidden_repeat_signatures == ("forbidden-concept",)
    assert packet.style_constraints == ("single best answer", "clinical vignette")
    assert packet.omitted_evidence_ids == ()
    assert len(packet.source_snapshot_hash) == 64
    assert len(packet.evidence) == 1
    assert packet.evidence[0].evidence_ids == ("ev-course",)
    assert packet.evidence[0].locators[0].authority_class is AuthorityClass.COURSE_MATERIAL
    assert packet.evidence[0].locators[0].locator_kind == "slide"
    assert [request.maximum_evidence for request in provider.requests] == [16, 16]


@pytest.mark.parametrize("insufficient", [False, True])
def test_refuses_when_correct_answer_concept_has_no_evidence(
    repository: KnowledgeRepository,
    insufficient: bool,
) -> None:
    objective_ref = _add_evidence(repository, evidence_id="ev-objective")
    provider = FakeRetrievalProvider(
        _result(objective_ref),
        by_query={
            "Factor VIII deficiency": _result(insufficient=insufficient),
            "Objective A": _result(objective_ref),
        },
    )

    with pytest.raises(QuestionEvidenceError, match="correct-answer concept"):
        _build(QuestionEvidencePacketBuilder(provider, repository), _request())


def test_refuses_empty_packet(repository: KnowledgeRepository) -> None:
    provider = FakeRetrievalProvider(_result())

    with pytest.raises(QuestionEvidenceError, match="no evidence"):
        _build(QuestionEvidencePacketBuilder(provider, repository), _request())


def test_integrated_item_requires_evidence_for_every_objective(
    repository: KnowledgeRepository,
) -> None:
    ref = _add_evidence(repository, evidence_id="ev-a")
    provider = FakeRetrievalProvider(
        _result(ref),
        by_query={
            "Factor VIII deficiency": _result(ref),
            "Objective A": _result(ref),
            "Objective B": _result(),
        },
    )
    request = _request(
        objectives=(
            QuestionObjective("obj-a", "Objective A"),
            QuestionObjective("obj-b", "Objective B"),
        ),
        mode=QuestionMode.INTEGRATED_BOARD_STYLE,
    )

    with pytest.raises(QuestionEvidenceError, match="obj-b"):
        _build(QuestionEvidencePacketBuilder(provider, repository), request)


def test_refuses_more_than_four_integrated_objectives(
    repository: KnowledgeRepository,
) -> None:
    ref = _add_evidence(repository, evidence_id="ev-a")
    provider = FakeRetrievalProvider(_result(ref))
    request = _request(
        objectives=tuple(
            QuestionObjective(f"obj-{index}", f"Objective {index}")
            for index in range(5)
        ),
        mode=QuestionMode.INTEGRATED_BOARD_STYLE,
    )

    with pytest.raises(QuestionEvidenceError, match="at most 4"):
        _build(QuestionEvidencePacketBuilder(provider, repository), request)
    assert provider.requests == []


def test_refuses_generated_artifacts_as_authority(
    repository: KnowledgeRepository,
) -> None:
    ref = _add_evidence(
        repository,
        evidence_id="ev-generated",
        authority=AuthorityClass.GENERATED_ARTIFACT,
        course_id=None,
        exam_id=None,
        lecture_id=None,
    )

    with pytest.raises(QuestionEvidenceError, match="generated artifact"):
        _build(
            QuestionEvidencePacketBuilder(FakeRetrievalProvider(_result(ref)), repository),
            _request(),
        )


def test_course_only_refuses_literature(repository: KnowledgeRepository) -> None:
    ref = _add_evidence(
        repository,
        evidence_id="ev-journal",
        authority=AuthorityClass.PUBLISHED_JOURNAL,
        course_id=None,
        exam_id=None,
        lecture_id=None,
        locator_kind=EvidenceLocatorKind.ARTICLE_PAGE,
    )

    with pytest.raises(QuestionEvidenceError, match="course_only"):
        _build(
            QuestionEvidencePacketBuilder(FakeRetrievalProvider(_result(ref)), repository),
            _request(),
        )


@pytest.mark.parametrize("state", [SourceRevisionState.STALE, SourceRevisionState.RETIRED])
def test_refuses_non_ready_source_lifecycle(
    repository: KnowledgeRepository,
    state: SourceRevisionState,
) -> None:
    ref = _add_evidence(repository, evidence_id=f"ev-{state.value}", revision_state=state)

    with pytest.raises(QuestionEvidenceError, match="READY"):
        _build(
            QuestionEvidencePacketBuilder(FakeRetrievalProvider(_result(ref)), repository),
            _request(),
        )


def test_refuses_retired_evidence(repository: KnowledgeRepository) -> None:
    ref = _add_evidence(
        repository,
        evidence_id="ev-retired",
        retired_at="2026-08-25T12:00:00+00:00",
    )

    with pytest.raises(QuestionEvidenceError, match="retired"):
        _build(
            QuestionEvidencePacketBuilder(FakeRetrievalProvider(_result(ref)), repository),
            _request(),
        )


def test_refuses_unknown_evidence(repository: KnowledgeRepository) -> None:
    known = _add_evidence(repository, evidence_id="ev-known")
    unknown = replace(known, evidence_id="ev-unknown")

    with pytest.raises(QuestionEvidenceError, match="unknown evidence"):
        _build(
            QuestionEvidencePacketBuilder(FakeRetrievalProvider(_result(unknown)), repository),
            _request(),
        )


@pytest.mark.parametrize(
    ("field_name", "field_value", "message"),
    [
        ("checksum", "0" * 64, "checksum"),
        ("checksum", f"sha256:{'0' * 64}", "checksum"),
        ("locator_value", "wrong-locator", "locator"),
        ("authority_class", AuthorityClass.PUBLISHED_JOURNAL, "authority"),
    ],
)
def test_refuses_provider_canonical_integrity_mismatch(
    repository: KnowledgeRepository,
    field_name: str,
    field_value: object,
    message: str,
) -> None:
    canonical = _add_evidence(repository, evidence_id="ev-course")
    if field_name == "authority_class":
        assert isinstance(field_value, AuthorityClass)
        mismatched = replace(canonical, authority_class=field_value)
    elif field_name == "locator_value":
        assert isinstance(field_value, str)
        mismatched = replace(canonical, locator_value=field_value)
    else:
        assert field_name == "checksum"
        assert isinstance(field_value, str)
        mismatched = replace(canonical, checksum=field_value)

    with pytest.raises(QuestionEvidenceError, match=message):
        _build(
            QuestionEvidencePacketBuilder(
                FakeRetrievalProvider(_result(mismatched)), repository
            ),
            _request(),
        )


def test_refuses_out_of_scope_canonical_evidence(
    repository: KnowledgeRepository,
) -> None:
    ref = _add_evidence(repository, evidence_id="ev-other-course", course_id="course-2")

    with pytest.raises(QuestionEvidenceError, match="scope"):
        _build(
            QuestionEvidencePacketBuilder(FakeRetrievalProvider(_result(ref)), repository),
            _request(),
        )


def test_deduplicates_normalized_claim_but_retains_every_locator(
    repository: KnowledgeRepository,
) -> None:
    pdf = _add_evidence(
        repository,
        evidence_id="ev-pdf",
        text="Factor VIII deficiency causes hemophilia A.",
        locator_kind=EvidenceLocatorKind.PAGE,
    )
    markdown = _add_evidence(
        repository,
        evidence_id="ev-markdown",
        text=" factor viii DEFICIENCY causes hemophilia A ",
        locator_kind=EvidenceLocatorKind.SECTION,
    )

    packet = _build(
        QuestionEvidencePacketBuilder(
            FakeRetrievalProvider(_result(markdown, pdf)), repository
        ),
        _request(),
    )

    assert len(packet.evidence) == 1
    assert packet.evidence[0].evidence_ids == ("ev-markdown", "ev-pdf")
    assert {locator.locator_kind for locator in packet.evidence[0].locators} == {
        "page",
        "section",
    }


def test_refuses_provider_result_over_sixteen_refs_before_repository_reads(
    repository: KnowledgeRepository,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    refs = tuple(
        _add_evidence(
            repository,
            evidence_id=f"ev-{index:02d}",
            text=f"Distinct claim number {index}.",
            source_priority=index,
        )
        for index in range(17)
    )
    revision_reads = 0
    original_get_revision = repository.get_revision

    def counted_get_revision(revision_id: str) -> SourceRevision | None:
        nonlocal revision_reads
        revision_reads += 1
        return original_get_revision(revision_id)

    monkeypatch.setattr(repository, "get_revision", counted_get_revision)

    with pytest.raises(QuestionEvidenceError, match="at most 16"):
        _build(
            QuestionEvidencePacketBuilder(
                FakeRetrievalProvider(_result(*reversed(refs))), repository
            ),
            _request(),
        )
    assert revision_reads == 0


def test_canonical_evidence_ids_remain_bounded_after_claim_deduplication(
    repository: KnowledgeRepository,
) -> None:
    concept_refs = tuple(
        _add_evidence(
            repository,
            evidence_id=f"ev-concept-{priority:02d}",
            text="Equivalent bounded claim.",
            source_priority=priority,
        )
        for priority in range(16)
    )
    objective_refs = tuple(
        _add_evidence(
            repository,
            evidence_id=f"ev-objective-{priority:02d}",
            text=" equivalent BOUNDED claim ",
            source_priority=priority,
        )
        for priority in range(16, 32)
    )
    provider = FakeRetrievalProvider(
        _result(),
        by_query={
            "Factor VIII deficiency": _result(*reversed(concept_refs)),
            "Objective A": _result(*reversed(objective_refs)),
        },
    )

    packet = _build(QuestionEvidencePacketBuilder(provider, repository), _request())

    retained_ids = {
        evidence_id for unit in packet.evidence for evidence_id in unit.evidence_ids
    }
    assert len(retained_ids) == 16
    assert "ev-concept-15" in retained_ids
    assert retained_ids.issuperset(
        {f"ev-objective-{priority:02d}" for priority in range(17, 32)}
    )
    assert packet.omitted_evidence_ids == tuple(
        sorted(
            {f"ev-concept-{priority:02d}" for priority in range(15)}
            | {"ev-objective-16"}
        )
    )


def test_character_limit_omits_whole_unit_without_slicing(
    repository: KnowledgeRepository,
) -> None:
    kept_text = f"{'A' * 9_998}."
    omitted_text = f"{'B' * 9_998}."
    kept = _add_evidence(
        repository,
        evidence_id="ev-kept",
        text=kept_text,
        source_priority=2,
    )
    omitted = _add_evidence(
        repository,
        evidence_id="ev-omitted",
        text=omitted_text,
        source_priority=1,
    )

    packet = _build(
        QuestionEvidencePacketBuilder(
            FakeRetrievalProvider(_result(omitted, kept)), repository
        ),
        _request(),
    )

    assert tuple(unit.normalized_text for unit in packet.evidence) == (kept_text,)
    assert packet.omitted_evidence_ids == ("ev-omitted",)


def test_snapshot_hash_is_independent_of_provider_order(
    repository: KnowledgeRepository,
) -> None:
    first = _add_evidence(repository, evidence_id="ev-a", text="Claim A.")
    second = _add_evidence(repository, evidence_id="ev-b", text="Claim B.")

    packet_a = _build(
        QuestionEvidencePacketBuilder(
            FakeRetrievalProvider(_result(first, second)), repository
        ),
        _request(),
    )
    packet_b = _build(
        QuestionEvidencePacketBuilder(
            FakeRetrievalProvider(_result(second, first)), repository
        ),
        _request(),
    )

    assert packet_a.source_snapshot_hash == packet_b.source_snapshot_hash
    assert packet_a.evidence == packet_b.evidence


def test_forbidden_correct_answer_signature_is_refused_before_retrieval(
    repository: KnowledgeRepository,
) -> None:
    ref = _add_evidence(repository, evidence_id="ev-course")
    provider = FakeRetrievalProvider(_result(ref))
    request = replace(
        _request(),
        forbidden_repeat_signatures=("factor-viii-deficiency",),
    )

    with pytest.raises(QuestionEvidenceError, match="forbidden repeat"):
        _build(QuestionEvidencePacketBuilder(provider, repository), request)
    assert provider.requests == []


@pytest.mark.parametrize(
    ("first_text", "second_text"),
    [
        ("Sodium is < 135 mmol/L.", "Sodium is > 135 mmol/L."),
        ("The finding is + 2.", "The finding is - 2."),
        ("The finding is + 2.", "The finding is − 2."),
        ("Sodium is ≤ 135 mmol/L.", "Sodium is ≥ 135 mmol/L."),
    ],
)
def test_claim_signature_preserves_semantic_operator_differences(
    repository: KnowledgeRepository,
    first_text: str,
    second_text: str,
) -> None:
    first = _add_evidence(repository, evidence_id="ev-first", text=first_text)
    second = _add_evidence(repository, evidence_id="ev-second", text=second_text)

    packet = _build(
        QuestionEvidencePacketBuilder(
            FakeRetrievalProvider(_result(first, second)), repository
        ),
        _request(),
    )

    assert len(packet.evidence) == 2


@pytest.mark.parametrize(
    ("first_text", "second_text"),
    [
        ("Sodium is ≤ 135 mmol/L.", " sodium IS <= 135 mmol/L "),
        ("Sodium is ≥ 135 mmol/L.", "SODIUM is >= 135 mmol/L"),
        ("The change is − 2.", " the CHANGE is - 2 "),
    ],
)
def test_claim_signature_deduplicates_typographic_operator_equivalents(
    repository: KnowledgeRepository,
    first_text: str,
    second_text: str,
) -> None:
    first = _add_evidence(repository, evidence_id="ev-first", text=first_text)
    second = _add_evidence(repository, evidence_id="ev-second", text=second_text)

    packet = _build(
        QuestionEvidencePacketBuilder(
            FakeRetrievalProvider(_result(first, second)), repository
        ),
        _request(),
    )

    assert len(packet.evidence) == 1
    assert packet.evidence[0].evidence_ids == ("ev-first", "ev-second")


@pytest.mark.parametrize("corruption", ["crlf_excerpt", "wrong_digest"])
def test_provider_excerpt_and_digest_must_exactly_match_canonical_evidence(
    repository: KnowledgeRepository,
    corruption: str,
) -> None:
    canonical = _add_evidence(
        repository,
        evidence_id="ev-lines",
        text="Line one.\nLine two.",
    )
    if corruption == "crlf_excerpt":
        corrupted = replace(canonical, excerpt="Line one.\r\nLine two.")
    else:
        corrupted = replace(canonical, checksum=f"sha256:{sha256_text('corrupted')}")

    with pytest.raises(QuestionEvidenceError, match="canonical"):
        _build(
            QuestionEvidencePacketBuilder(
                FakeRetrievalProvider(_result(corrupted)), repository
            ),
            _request(),
        )


@pytest.mark.parametrize("mutation", ["stale_revision", "retired_evidence"])
def test_final_packet_revalidation_catches_lifecycle_interleaving(
    repository: KnowledgeRepository,
    monkeypatch: pytest.MonkeyPatch,
    mutation: str,
) -> None:
    ref = _add_evidence(repository, evidence_id="ev-interleaved")
    original_list_evidence = repository.list_evidence
    list_calls = 0

    def interleaving_list_evidence(revision_id: str) -> tuple[EvidenceUnit, ...]:
        nonlocal list_calls
        rows = original_list_evidence(revision_id)
        list_calls += 1
        if list_calls == 2:
            with repository.database.engine.begin() as connection:
                if mutation == "stale_revision":
                    connection.execute(
                        sql_text(
                            "UPDATE source_revisions SET state = 'stale' WHERE id = :id"
                        ),
                        {"id": revision_id},
                    )
                else:
                    connection.execute(
                        sql_text(
                            "UPDATE evidence_units SET retired_at = :retired_at "
                            "WHERE id = :id"
                        ),
                        {
                            "id": ref.evidence_id,
                            "retired_at": "2026-08-25T13:00:00+00:00",
                        },
                    )
        return rows

    monkeypatch.setattr(repository, "list_evidence", interleaving_list_evidence)

    with pytest.raises(QuestionEvidenceError, match="READY|retired"):
        _build(
            QuestionEvidencePacketBuilder(FakeRetrievalProvider(_result(ref)), repository),
            _request(),
        )


def test_required_cover_finds_feasible_small_combination(
    repository: KnowledgeRepository,
) -> None:
    oversized = _add_evidence(
        repository,
        evidence_id="ev-oversized-concept",
        text=f"{'H' * 17_998}.",
        source_priority=100,
    )
    compact_concept = _add_evidence(
        repository,
        evidence_id="ev-compact-concept",
        text="Compact concept evidence.",
        source_priority=1,
    )
    compact_objective = _add_evidence(
        repository,
        evidence_id="ev-compact-objective",
        text="Compact objective evidence.",
        source_priority=90,
    )
    provider = FakeRetrievalProvider(
        _result(),
        by_query={
            "Factor VIII deficiency": _result(oversized, compact_concept),
            "Objective A": _result(compact_objective),
        },
    )

    packet = _build(QuestionEvidencePacketBuilder(provider, repository), _request())

    assert {
        evidence_id for unit in packet.evidence for evidence_id in unit.evidence_ids
    } == {"ev-compact-concept", "ev-compact-objective"}
    assert packet.omitted_evidence_ids == ("ev-oversized-concept",)


@pytest.mark.parametrize(
    ("invalid_field", "message"),
    [
        ("evidence_list", "tuple"),
        ("non_bool_insufficiency", "bool"),
        ("blank_request_id", "request id"),
        ("non_string_request_id", "request id"),
    ],
)
def test_retrieval_result_shape_is_validated_before_repository_reads(
    repository: KnowledgeRepository,
    monkeypatch: pytest.MonkeyPatch,
    invalid_field: str,
    message: str,
) -> None:
    ref = _add_evidence(repository, evidence_id="ev-shape")
    if invalid_field == "evidence_list":
        result = RetrievalResult(
            evidence=cast(Any, [ref]),
            provider_request_id="synthetic-request",
            insufficient_evidence=False,
        )
    elif invalid_field == "non_bool_insufficiency":
        result = RetrievalResult(
            evidence=(ref,),
            provider_request_id="synthetic-request",
            insufficient_evidence=cast(Any, 0),
        )
    elif invalid_field == "blank_request_id":
        result = RetrievalResult(
            evidence=(ref,),
            provider_request_id=" ",
            insufficient_evidence=False,
        )
    else:
        result = RetrievalResult(
            evidence=(ref,),
            provider_request_id=cast(Any, None),
            insufficient_evidence=False,
        )
    revision_reads = 0
    original_get_revision = repository.get_revision

    def counted_get_revision(revision_id: str) -> SourceRevision | None:
        nonlocal revision_reads
        revision_reads += 1
        return original_get_revision(revision_id)

    monkeypatch.setattr(repository, "get_revision", counted_get_revision)

    with pytest.raises(QuestionEvidenceError, match=message):
        _build(
            QuestionEvidencePacketBuilder(FakeRetrievalProvider(result), repository),
            _request(),
        )
    assert revision_reads == 0
