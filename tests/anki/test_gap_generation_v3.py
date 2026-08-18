import asyncio
import hashlib
import json
from datetime import UTC, datetime
from math import sqrt
from types import SimpleNamespace
from uuid import UUID

import pytest

import oms_hub.anki.stages as stages
from oms_hub.anki.classification_v3 import ESTIMATOR_VERSION
from oms_hub.anki.contracts import canonical_payload_sha256
from oms_hub.anki.cost_estimator import (
    CostEstimator,
    CostKind,
    CostLedgerEntry,
    FrozenRateTable,
    ModelRate,
    TokenUsage,
)
from oms_hub.anki.course_policy import CourseCurationPolicy
from oms_hub.anki.dedupe import DeduplicationService, V3DedupeProposal
from oms_hub.anki.domain import (
    CurationStage,
    PipelineContractVersion,
    ResolvedModelConfiguration,
    ResolvedStageModel,
)
from oms_hub.anki.gap_generation_v3 import (
    ANKING_NOTE_TYPE,
    R9GenerationService,
    V3Evidence,
    V3GenerationFact,
    V3GenerationRequest,
    _repair_document,
)
from oms_hub.anki.gaps import GapValidationError
from oms_hub.anki.normalize import NormalizedNote
from oms_hub.anki.provider_attempts import (
    ProviderAttemptBinding,
    ProviderEventEvidence,
    bind_provider_attempts,
    provider_cost_reservation,
)
from oms_hub.anki.scope_contracts import (
    LectureScope,
    ScopedConcept,
    ScopedFact,
    ScopeEvidenceReference,
)
from oms_hub.anki.stages import CurationServicesRunner
from oms_hub.llm.domain import GeneratedText, ProviderName
from oms_hub.llm.structured import StructuredTextService


def _add_r0_costs(r0: dict[str, object], *models: str) -> None:
    table = FrozenRateTable(
        tuple(ModelRate(model, 1, 0, 0, 1, 1) for model in sorted(set(models))),
        datetime(2026, 8, 17, tzinfo=UTC),
        "fixture",
    )
    policy = CourseCurationPolicy.model_validate(r0["policy"])
    r0.update(
        rate_table=table.document(),
        rate_table_sha256=table.rate_table_sha256,
        ordinary_cost_limit_microusd=policy.ordinary_cost_limit_microusd,
        hard_stop_cost_limit_microusd=policy.hard_stop_cost_limit_microusd,
        cost_ledger=[],
        cost_ledger_sha256=hashlib.sha256(b"[]").hexdigest(),
    )


def _reservation() -> dict[str, object]:
    table = FrozenRateTable(
        (ModelRate("fake", 1, 1, 1, 1, 1),), datetime(2026, 8, 17, tzinfo=UTC), "fixture"
    )
    estimator = CostEstimator(table)
    usage = TokenUsage(input_tokens=1)
    return CostLedgerEntry(
        call_id="a" * 64,
        stage="R9",
        modality="structured",
        model="fake",
        request_sha256="b" * 64,
        rate_table_sha256=table.rate_table_sha256,
        estimator_version=estimator.version,
        predicted=estimator.estimate(CostKind.PREDICTED, model="fake", usage=usage),
        reserved=estimator.estimate(CostKind.RESERVED, model="fake", usage=usage),
    ).document()


def _empty_ledger(payload: dict[str, object]) -> None:
    payload["cost_ledger"] = []
    payload["cost_ledger_sha256"] = hashlib.sha256(b"[]").hexdigest()


class _FakeGenerator:
    offline_replay_only = True

    def __init__(self, responses: list[object]) -> None:
        self.responses = iter(responses)
        self.calls: list[dict[str, object]] = []

    def generate_text(self, instruction: str, input_text: str, **kwargs: object) -> GeneratedText:
        self.calls.append({"instruction": instruction, "input": json.loads(input_text), **kwargs})
        response = next(self.responses)
        if isinstance(response, Exception):
            raise response
        return GeneratedText(
            text=json.dumps(response),
            provider=ProviderName.OPENAI,
            model="fake",
            request_id=f"request-{len(self.calls)}",
            input_tokens=1,
            output_tokens=1,
            cost_microusd=1,
        )


class _Embedder:
    offline_replay_only = True

    def __init__(self, rows: list[list[float]]) -> None:
        self.rows = rows
        self.calls: list[list[str]] = []

    async def embed(self, values: list[str], *, input_type: str) -> list[list[float]]:
        assert input_type == "document"
        self.calls.append(values)
        return self.rows


class _RaisingEmbedder:
    offline_replay_only = True

    async def embed(self, _values: list[str], *, input_type: str) -> list[list[float]]:
        raise AssertionError(f"R10 exact-only batch must not embed ({input_type})")


class _Companion:
    def __init__(self, notes: tuple[NormalizedNote, ...]) -> None:
        self.notes = {note.note_id: note for note in notes}

    def get_note(self, note_id: int) -> NormalizedNote | None:
        return self.notes.get(note_id)


class _PinnedVectors:
    embedder = SimpleNamespace(offline_replay_only=True)

    def __init__(self, vectors: dict[int, tuple[float, ...]]) -> None:
        self.vectors = vectors
        self.calls: list[tuple[tuple[int, ...], str]] = []

    async def pinned_document_vectors(
        self, *, note_ids: list[int], expected_generation: str
    ) -> dict[int, tuple[float, ...]]:
        self.calls.append((tuple(note_ids), expected_generation))
        return {note_id: self.vectors[note_id] for note_id in note_ids}


def _offline_runner(generator: _FakeGenerator) -> CurationServicesRunner:
    runner = object.__new__(CurationServicesRunner)
    runner.structured = StructuredTextService(generator)
    runner.embedder = SimpleNamespace(offline_replay_only=True)
    runner.semantic = SimpleNamespace(embedder=SimpleNamespace(offline_replay_only=True))
    return runner


def _request(*, allowed: bool = True) -> V3GenerationRequest:
    return V3GenerationRequest(
        policy_sha256="a" * 64,
        scope_sha256="b" * 64,
        style_profile="cloze",
        facts=(
            V3GenerationFact(
                fact_id="fact-1",
                statement="Fact statement",
                evidence=(V3Evidence(evidence_id="e1", text="only cited evidence"),),
                forbidden_cloze_targets=("forbidden",),
                generation_allowed=allowed,
            ),
        ),
    )


def _generated_response() -> dict[str, object]:
    return {
        "resolutions": [
            {
                "fact_id": "fact-1",
                "status": "generated",
                "text": "{{c1::answer}}",
                "extra": "",
                "note_type": ANKING_NOTE_TYPE,
                "evidence_ids": ["e1"],
                "split": False,
                "split_index": None,
            }
        ]
    }


def _repair_authorization(
    request: V3GenerationRequest, *, rate_table_sha256: str = "c" * 64
) -> dict[str, object]:
    repair = _repair_document(
        request.provider_document(),
        GapValidationError("R9 output does not partition requested facts"),
        SimpleNamespace(raw_text=json.dumps({"resolutions": []})),
    )
    values: dict[str, object] = {
        "policy_sha256": request.policy_sha256,
        "rate_table_sha256": rate_table_sha256,
        "estimator_version": ESTIMATOR_VERSION,
        "repair_request_sha256": canonical_payload_sha256(repair),
        "predicted_total_before_repair_microusd": 0,
        "predicted_repair_cost_microusd": 1,
        "predicted_total_after_repair_microusd": 1,
    }
    values["authorization_sha256"] = canonical_payload_sha256(values)
    return values


def test_r9_generates_only_projected_evidence_with_stable_split_contract() -> None:
    fake = _FakeGenerator(
        [
            {
                "resolutions": [
                    {
                        "fact_id": "fact-1",
                        "status": "generated",
                        "text": "{{c1::answer}}",
                        "extra": "",
                        "note_type": ANKING_NOTE_TYPE,
                        "evidence_ids": ["e1"],
                        "split": False,
                        "split_index": None,
                    }
                ]
            }
        ]
    )
    result, usage = R9GenerationService(StructuredTextService(fake)).generate(
        _request(), route=ResolvedStageModel("openai", "fake", thinking_mode="disabled")
    )
    assert result.blocking_error is None and usage is not None
    assert result.resolutions[0].status == "generated"
    assert fake.calls[0]["input"]["facts"][0]["evidence"] == [
        {"evidence_id": "e1", "text": "only cited evidence"}
    ]
    assert fake.calls[0]["options"].cacheable_source_prefix is None


def test_r9_disabled_fact_and_invalid_split_fail_closed_without_provider() -> None:
    fake = _FakeGenerator([])
    result, usage = R9GenerationService(StructuredTextService(fake)).generate(
        _request(allowed=False),
        route=ResolvedStageModel("openai", "fake", thinking_mode="disabled"),
    )
    assert usage is None and result.resolutions[0].status == "unresolved" and fake.calls == []
    with pytest.raises(ValueError, match="16384"):
        V3GenerationFact(
            fact_id="large",
            statement="x" * 10_000,
            evidence=(V3Evidence(evidence_id="e", text="e" * 10_000),),
            generation_allowed=True,
        )


def test_r9_repair_authorization_is_exact_bounded_and_one_time() -> None:
    request = _request()
    valid = _generated_response()
    fake = _FakeGenerator([{"resolutions": []}, valid, {"resolutions": []}])
    service = R9GenerationService(StructuredTextService(fake))
    result, usage = service.generate(
        request,
        route=ResolvedStageModel("openai", "fake", thinking_mode="disabled"),
        repair_authorization=_repair_authorization(request),
        rate_table_sha256="c" * 64,
        ordinary_limit_microusd=1,
        hard_limit_microusd=1,
    )
    assert result.resolutions[0].status == "generated" and usage is not None
    assert (
        len(fake.calls) == 2
        and fake.calls[1]["input"]["serialization_version"] == "gap-generation-r9-repair-v1"
    )
    second, _ = service.generate(
        request,
        route=ResolvedStageModel("openai", "fake", thinking_mode="disabled"),
        repair_authorization=_repair_authorization(request),
        rate_table_sha256="c" * 64,
        ordinary_limit_microusd=1,
        hard_limit_microusd=1,
    )
    assert second.blocking_error == "R9 repair already consumed" and len(fake.calls) == 3


def test_r9_stale_or_oversized_repair_and_transport_never_repair() -> None:
    route = ResolvedStageModel("openai", "fake", thinking_mode="disabled")
    stale = _FakeGenerator([{"resolutions": []}])
    bad_auth = _repair_authorization(_request())
    bad_auth["repair_request_sha256"] = "f" * 64
    result, _ = R9GenerationService(StructuredTextService(stale)).generate(
        _request(),
        route=route,
        repair_authorization=bad_auth,
        rate_table_sha256="c" * 64,
        ordinary_limit_microusd=1,
        hard_limit_microusd=1,
    )
    assert result.blocking_error == "R9 repair is not authorized" and len(stale.calls) == 1
    oversized = _FakeGenerator(["x" * 70_000])
    result, _ = R9GenerationService(StructuredTextService(oversized)).generate(
        _request(),
        route=route,
        repair_authorization=_repair_authorization(_request()),
        rate_table_sha256="c" * 64,
        ordinary_limit_microusd=1,
        hard_limit_microusd=1,
    )
    assert "exceeds 65536" in str(result.blocking_error) and len(oversized.calls) == 1
    transport = _FakeGenerator([RuntimeError("network")])
    result, _ = R9GenerationService(StructuredTextService(transport)).generate(
        _request(),
        route=route,
        repair_authorization=_repair_authorization(_request()),
        rate_table_sha256="c" * 64,
        ordinary_limit_microusd=1,
        hard_limit_microusd=1,
    )
    assert "transport" in str(result.blocking_error) and len(transport.calls) == 1


def test_r9_repair_transport_is_blocking_and_keeps_the_primary_lifecycle() -> None:
    request = _request()
    fake = _FakeGenerator([{"resolutions": []}, RuntimeError("repair network")])
    events: list[ProviderEventEvidence] = []
    with (
        bind_provider_attempts(
            ProviderAttemptBinding(
                job_id=UUID("12345678-1234-5678-1234-567812345678"),
                stage=CurationStage.V3_R9_GENERATION,
                stage_attempt=1,
                mode="canonical",
                recorder=events.append,
            )
        ),
        provider_cost_reservation(_reservation()),
    ):
        result, usage = R9GenerationService(StructuredTextService(fake)).generate(
            request,
            route=ResolvedStageModel("openai", "fake", thinking_mode="disabled"),
            repair_authorization=_repair_authorization(request),
            rate_table_sha256="c" * 64,
            ordinary_limit_microusd=1,
            hard_limit_microusd=1,
        )
    assert result.blocking_error == "R9 repair transport failure: repair network"
    assert [call["request_id"] for call in result.calls] == ["request-1"]
    assert usage is not None and usage.cost_microusd == 1 and len(fake.calls) == 2
    terminal_events = [
        event.event.event
        for event in events
        if event.event.event in {"contract_failed", "transport_failed"}
    ]
    assert terminal_events == [
        "contract_failed",
        "transport_failed",
    ]


def test_r10_exact_partial_vectors_overlap_and_generated_ties_are_stable() -> None:
    existing = (
        NormalizedNote(1, "Basic", "{{c1::same}}", "", {}, (), (), (), "a", "a" * 64, ("Deck",)),
        NormalizedNote(2, "Basic", "other", "", {}, (), (), (), "b", "b" * 64, ("Deck",)),
    )
    proposals = (
        V3DedupeProposal("card:fact-1:1", "fact-1", "{{c1::same}}", ""),
        V3DedupeProposal("card:fact-2:1", "fact-2", "{{c1::fresh}}", ""),
        V3DedupeProposal("card:fact-3:1", "fact-3", "{{c1::fresh}}", ""),
    )
    service = DeduplicationService(_Embedder([[0.0, 1.0]]))
    rows = asyncio.run(
        service.classify_v3_batch(proposals, existing, existing_document_vectors={1: (1.0, 0.0)})
    )
    assert [row.disposition for row in rows] == ["duplicate", "generated", "duplicate"]
    assert rows[0].duplicate_of == "note:1" and rows[2].duplicate_of == "card:fact-2:1"
    assert rows[1].missing_existing_vector_note_ids == (2,)
    assert len(service.embedder.calls) == 1


def test_r10_overlap_is_reviewable_not_accepted() -> None:
    existing = (
        NormalizedNote(1, "Basic", "existing", "", {}, (), (), (), "a", "a" * 64, ("Deck",)),
    )
    rows = asyncio.run(
        DeduplicationService(_Embedder([[0.9, 0.435]])).classify_v3_batch(
            (V3DedupeProposal("card:fact:1", "fact", "{{c1::new}}", ""),),
            existing,
            existing_document_vectors={1: (1.0, 0.0)},
        )
    )
    assert rows[0].disposition == "overlap" and rows[0].duplicate_of is None


def test_r10_all_exact_rows_skip_embedding() -> None:
    existing = (
        NormalizedNote(1, "Basic", "{{c1::same}}", "", {}, (), (), (), "a", "a" * 64, ("Deck",)),
    )
    rows = asyncio.run(
        DeduplicationService(_RaisingEmbedder()).classify_v3_batch(
            (V3DedupeProposal("card:fact:1", "fact", "{{c1::same}}", ""),),
            existing,
            existing_document_vectors={},
        )
    )
    assert rows[0].disposition == "duplicate" and rows[0].duplicate_of == "note:1"


def _r9_runner_artifacts(
    facts: tuple[ScopedFact, ...], records: list[dict[str, object]]
) -> tuple[
    SimpleNamespace,
    tuple[
        dict[str, object],
        LectureScope,
        dict[str, object],
        dict[str, object],
        dict[str, object],
        dict[str, object],
    ],
]:
    from hashlib import sha256

    from oms_hub.anki.classification_v3 import route_document
    from oms_hub.anki.course_policy import CourseCurationPolicy

    policy = CourseCurationPolicy(
        policy_id="p",
        revision=1,
        course_id="c",
        professor_label="p",
        scope_instruction="s",
        emphasis_mode="transcript_emphasis",
        missing_emphasis_fallback="block",
        tag_scope_mode="disabled",
        classification_strictness="strict",
        generation_style_profile="cloze",
        ordinary_cost_limit_microusd=10,
        hard_stop_cost_limit_microusd=10,
    )
    source_bundle = {
        "evidence": [
            {
                "evidence_id": "e",
                "normalized_text": "evidence",
                "content_sha256": sha256(b"evidence").hexdigest(),
            }
        ]
    }
    scope = LectureScope(
        scope_id="s",
        policy_sha256=policy.policy_sha256,
        source_bundle_sha256=canonical_payload_sha256(source_bundle),
        degraded_mode="none",
        evidence=(
            ScopeEvidenceReference(
                evidence_id="e",
                source_id="s",
                locator="l",
                content_sha256=sha256(b"evidence").hexdigest(),
            ),
        ),
        concepts=(
            ScopedConcept(
                concept_id="c",
                canonical_statement="c",
                primary_entity="e",
                depth_tier=1,
                priority=1,
                reason="r",
                facts=facts,
                source_evidence_ids=("e",),
                retrieval_queries=("q",),
            ),
        ),
    )
    route = ResolvedStageModel("openai", "fake", thinking_mode="disabled")
    config = ResolvedModelConfiguration("v3", route, route, route, route, generation_r9=route)
    r0 = {
        "policy": policy.model_dump(mode="json"),
        "policy_sha256": policy.policy_sha256,
        "policy_revision": 1,
        "model_config_sha256": "m" * 64,
        "generation_r9": route_document(route),
    }
    _add_r0_costs(r0, route.model, "embedding")
    r4, r5, r6, r7 = (
        {"verification_sha256": "r4"},
        {"artifact_sha256": "r5"},
        {"artifact_sha256": "r6"},
        {"artifact_sha256": "r7"},
    )
    for payload in (r5, r6, r7):
        _empty_ledger(payload)
    r8 = {
        "policy_sha256": policy.policy_sha256,
        "scope_sha256": scope.scope_sha256,
        "r4_verification_sha256": "r4",
        "r5_artifact_sha256": "r5",
        "r6_artifact_sha256": "r6",
        "r7_artifact_sha256": "r7",
        "records": records,
    }
    _empty_ledger(r8)
    r8["artifact_sha256"] = canonical_payload_sha256(r8)
    job = SimpleNamespace(
        pipeline_contract_version=PipelineContractVersion.CARD_CENTRIC_V3,
        id="generation-job",
        policy_sha256=policy.policy_sha256,
        model_config_sha256="m" * 64,
        resolved_model_config=config,
        offline_replay_only=True,
    )
    context = SimpleNamespace(
        job=job,
        stage=CurationStage.V3_R9_GENERATION,
        prior_payloads={
            CurationStage.V3_R0_PREFLIGHT: r0,
            CurationStage.V3_R3_SCOPE: {
                "scope": scope.model_dump(mode="json"),
                "source_bundle": source_bundle,
                "cost_ledger": [],
                "cost_ledger_sha256": hashlib.sha256(b"[]").hexdigest(),
            },
            CurationStage.V3_R4_INDEX_VERIFICATION: r4,
            CurationStage.V3_R5_RETRIEVAL: r5,
            CurationStage.V3_R6_CALIBRATION: r6,
            CurationStage.V3_R7_CLASSIFICATION: r7,
            CurationStage.V3_R8_GAP_CONFIRMATION: r8,
        },
    )
    return context, (r0, scope, r4, r5, r6, r7)


def _r9_facts(count: int, *, disabled_last: bool = False) -> tuple[ScopedFact, ...]:
    return tuple(
        ScopedFact(
            fact_id=f"f{index:02d}",
            statement=f"fact {index}",
            evidence_ids=("e",),
            generation_allowed=not (disabled_last and index == count),
        )
        for index in range(1, count + 1)
    )


def _r9_response(*fact_ids: str) -> dict[str, object]:
    response = _generated_response()
    response["resolutions"] = [
        {**response["resolutions"][0], "fact_id": fact_id, "evidence_ids": ["e"]}
        for fact_id in fact_ids
    ]
    return response


def test_r9_runner_dispatches_only_confirmed_allowed_and_checks_r8_partition(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    facts = _r9_facts(4, disabled_last=True)
    records = [
        {"fact_id": "f01", "generation_allowed": True, "state": "confirmed_missing"},
        {"fact_id": "f02", "generation_allowed": True, "state": "covered_initial"},
        {"fact_id": "f03", "generation_allowed": True, "state": "unresolved"},
        {"fact_id": "f04", "generation_allowed": False, "state": "confirmed_missing"},
    ]
    context, inputs = _r9_runner_artifacts(facts, records)
    monkeypatch.setattr(stages, "_v3_phase_f_inputs", lambda _context: inputs)
    fake = _FakeGenerator([_r9_response("f01")])
    runner = _offline_runner(fake)
    product = asyncio.run(runner.run(context))
    assert [item["fact_id"] for item in fake.calls[0]["input"]["facts"]] == ["f01"]
    assert product.payload["resolutions"][0]["card_id"] == "card:f01:1"
    assert product.payload["artifact_sha256"] == canonical_payload_sha256(
        {key: value for key, value in product.payload.items() if key != "artifact_sha256"}
    )
    r8 = context.prior_payloads[CurationStage.V3_R8_GAP_CONFIRMATION]
    r8["records"].append(dict(records[0]))
    r8["artifact_sha256"] = canonical_payload_sha256(
        {key: value for key, value in r8.items() if key != "artifact_sha256"}
    )
    with pytest.raises(Exception, match="partition"):
        asyncio.run(runner.run(context))


def test_r9_runner_binds_each_batch_and_aggregates_the_single_failed_repair(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    facts = _r9_facts(17)
    records = [
        {"fact_id": fact.fact_id, "generation_allowed": True, "state": "confirmed_missing"}
        for fact in facts
    ]
    context, inputs = _r9_runner_artifacts(facts, records)
    r0, scope, *_ = inputs
    first_batch = V3GenerationRequest(
        policy_sha256=scope.policy_sha256,
        scope_sha256=scope.scope_sha256,
        style_profile="cloze",
        facts=tuple(
            V3GenerationFact(
                fact_id=fact.fact_id,
                statement=fact.statement,
                evidence=(V3Evidence(evidence_id="e", text="evidence"),),
                forbidden_cloze_targets=fact.forbidden_cloze_targets,
                generation_allowed=True,
            )
            for fact in facts[:16]
        ),
    )
    r0["r9_repair_authorization"] = _repair_authorization(
        first_batch, rate_table_sha256=str(r0["rate_table_sha256"])
    )
    monkeypatch.setattr(stages, "_v3_phase_f_inputs", lambda _context: inputs)
    fake = _FakeGenerator([{"resolutions": []}, {"resolutions": []}, _r9_response("f17")])
    runner = _offline_runner(fake)
    events: list[ProviderEventEvidence] = []
    with bind_provider_attempts(
        ProviderAttemptBinding(
            job_id=UUID("12345678-1234-5678-1234-567812345678"),
            stage=CurationStage.V3_R9_GENERATION,
            stage_attempt=1,
            mode="canonical",
            recorder=events.append,
            replay_namespace="r9-runner-test",
        )
    ):
        product = asyncio.run(runner.run(context))
    identities = {event.event.identity for event in events}
    terminals = [event for event in events if event.event.event in {"accepted", "contract_failed"}]
    assert len(identities) == 3
    terminal_slots = [
        (event.event.identity.batch_index, event.event.identity.kind) for event in terminals
    ]
    assert terminal_slots == [
        (0, "primary"),
        (0, "repair"),
        (1, "primary"),
    ]
    assert [event.event.event for event in terminals] == [
        "contract_failed",
        "contract_failed",
        "accepted",
    ]
    assert all(event.event.identity.call_index for event in terminals)
    assert [call["request_id"] for call in product.payload["calls"]] == [
        "request-1",
        "request-2",
        "request-3",
    ]
    assert product.usage is not None and product.usage.cost_microusd == 3
    assert product.blocking_error is not None
    assert product.blocking_error.startswith("R9 repair failed")
    assert {item["fact_id"] for item in product.payload["resolutions"]} == {
        fact.fact_id for fact in facts
    }
    assert (
        next(item for item in product.payload["resolutions"] if item["fact_id"] == "f17")["status"]
        == "generated"
    )


@pytest.mark.parametrize(
    ("response", "expected", "call_count", "cost"),
    (
        (RuntimeError("network"), "R9 provider transport failure: network", 0, None),
        ({"resolutions": []}, "R9 repair is not authorized", 1, 1),
        (
            {"resolutions": [{"fact_id": "f01", "status": "unresolved", "reason": "defer"}]},
            None,
            1,
            1,
        ),
    ),
)
def test_r9_runner_marks_transport_and_unauthorized_repair_failures_blocking(
    monkeypatch: pytest.MonkeyPatch,
    response: object,
    expected: str | None,
    call_count: int,
    cost: int | None,
) -> None:
    facts = _r9_facts(1)
    context, inputs = _r9_runner_artifacts(
        facts,
        [{"fact_id": "f01", "generation_allowed": True, "state": "confirmed_missing"}],
    )
    monkeypatch.setattr(stages, "_v3_phase_f_inputs", lambda _context: inputs)
    runner = _offline_runner(_FakeGenerator([response]))
    product = asyncio.run(runner.run(context))
    assert product.blocking_error == expected and len(product.payload["calls"]) == call_count
    assert (product.usage.cost_microusd if product.usage else None) == cost


def test_r9_runner_preserves_mixed_batches_when_the_repair_is_exhausted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    facts = _r9_facts(17)
    context, inputs = _r9_runner_artifacts(
        facts,
        [
            {"fact_id": fact.fact_id, "generation_allowed": True, "state": "confirmed_missing"}
            for fact in facts
        ],
    )
    r0, scope, *_ = inputs
    first_batch = V3GenerationRequest(
        policy_sha256=scope.policy_sha256,
        scope_sha256=scope.scope_sha256,
        style_profile="cloze",
        facts=tuple(
            V3GenerationFact(
                fact_id=fact.fact_id,
                statement=fact.statement,
                evidence=(V3Evidence(evidence_id="e", text="evidence"),),
                forbidden_cloze_targets=fact.forbidden_cloze_targets,
                generation_allowed=True,
            )
            for fact in facts[:16]
        ),
    )
    r0["r9_repair_authorization"] = _repair_authorization(
        first_batch, rate_table_sha256=str(r0["rate_table_sha256"])
    )
    monkeypatch.setattr(stages, "_v3_phase_f_inputs", lambda _context: inputs)
    runner = _offline_runner(
        _FakeGenerator(
            [
                {"resolutions": []},
                _r9_response(*(fact.fact_id for fact in facts[:16])),
                {"resolutions": []},
            ]
        )
    )
    product = asyncio.run(runner.run(context))
    assert product.blocking_error == "R9 repair already consumed"
    assert [call["request_id"] for call in product.payload["calls"]] == [
        "request-1",
        "request-2",
        "request-3",
    ]
    assert product.usage is not None and product.usage.cost_microusd == 3
    rows = {item["fact_id"]: item for item in product.payload["resolutions"]}
    assert rows["f01"]["status"] == "generated" and rows["f17"]["status"] == "unresolved"


def test_r9_runner_blocks_a_transport_failed_authorized_repair(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    facts = _r9_facts(1)
    context, inputs = _r9_runner_artifacts(
        facts,
        [{"fact_id": "f01", "generation_allowed": True, "state": "confirmed_missing"}],
    )
    r0, scope, *_ = inputs
    request = V3GenerationRequest(
        policy_sha256=scope.policy_sha256,
        scope_sha256=scope.scope_sha256,
        style_profile="cloze",
        facts=(
            V3GenerationFact(
                fact_id="f01",
                statement="fact 1",
                evidence=(V3Evidence(evidence_id="e", text="evidence"),),
                generation_allowed=True,
            ),
        ),
    )
    r0["r9_repair_authorization"] = _repair_authorization(
        request, rate_table_sha256=str(r0["rate_table_sha256"])
    )
    monkeypatch.setattr(stages, "_v3_phase_f_inputs", lambda _context: inputs)
    runner = _offline_runner(_FakeGenerator([{"resolutions": []}, RuntimeError("repair network")]))
    events: list[ProviderEventEvidence] = []
    with bind_provider_attempts(
        ProviderAttemptBinding(
            job_id=UUID("12345678-1234-5678-1234-567812345678"),
            stage=CurationStage.V3_R9_GENERATION,
            stage_attempt=1,
            mode="canonical",
            recorder=events.append,
        )
    ):
        product = asyncio.run(runner.run(context))
    assert product.blocking_error == "R9 repair transport failure: repair network"
    assert [call["request_id"] for call in product.payload["calls"]] == ["request-1"]
    assert product.usage is not None and product.usage.cost_microusd == 1
    terminal_events = [
        event.event.event
        for event in events
        if event.event.event in {"contract_failed", "transport_failed"}
    ]
    assert terminal_events == [
        "contract_failed",
        "transport_failed",
    ]


def test_r10_runner_uses_exact_only_and_pinned_partial_vector_coverage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    facts = _r9_facts(1)
    context, inputs = _r9_runner_artifacts(
        facts,
        [{"fact_id": "f01", "generation_allowed": True, "state": "confirmed_missing"}],
    )
    r0, scope, r4, *_ = inputs
    r8 = context.prior_payloads[CurationStage.V3_R8_GAP_CONFIRMATION]
    r4.update(
        {"semantic_generation": "semantic-1", "card_identities": [], "semantic_identities": []}
    )
    context.stage = CurationStage.V3_R10_DEDUPE
    monkeypatch.setattr(stages, "_v3_phase_f_inputs", lambda _context: inputs)

    exact_note = NormalizedNote(
        1, "Basic", "{{c1::same}}", "", {}, (), (), (), "a", "a" * 64, ("Deck",)
    )
    r4["card_identities"] = [{"note_id": 1, "content_sha256": exact_note.content_sha256}]
    r9 = {
        "policy_sha256": r0["policy_sha256"],
        "scope_sha256": scope.scope_sha256,
        "r8_artifact_sha256": r8["artifact_sha256"],
        "resolutions": [
            {
                "fact_id": "f01",
                "status": "generated",
                "card_id": "card:f01:1",
                "text": "{{c1::same}}",
                "extra": "",
                "split_index": None,
            }
        ],
    }
    _empty_ledger(r9)
    r9["artifact_sha256"] = canonical_payload_sha256(r9)
    context.prior_payloads[CurationStage.V3_R9_GENERATION] = r9
    exact_runner = object.__new__(CurationServicesRunner)
    exact_runner.structured = StructuredTextService(_FakeGenerator([]))
    exact_runner.companion = _Companion((exact_note,))
    exact_runner.semantic = _PinnedVectors({})
    exact_runner.embedder = _RaisingEmbedder()
    exact = asyncio.run(exact_runner.run(context))
    assert exact.payload["resolutions"][0]["status"] == "duplicate_of_existing"
    context.stage = CurationStage.V3_R11_REVIEW
    with pytest.raises(KeyError):
        asyncio.run(exact_runner.run(context))

    covered = NormalizedNote(1, "Basic", "covered", "", {}, (), (), (), "a", "a" * 64, ("Deck",))
    missing = NormalizedNote(2, "Basic", "uncovered", "", {}, (), (), (), "b", "b" * 64, ("Deck",))
    r4["card_identities"] = [
        {"note_id": note.note_id, "content_sha256": note.content_sha256}
        for note in (covered, missing)
    ]
    r4["semantic_identities"] = [{"note_id": 1}]
    r9["resolutions"][0].update(text="{{c1::fresh}}", card_id="card:f01:1")
    r9["artifact_sha256"] = canonical_payload_sha256(
        {key: value for key, value in r9.items() if key != "artifact_sha256"}
    )
    semantic = _PinnedVectors({1: (1.0, 0.0)})
    embedder = _Embedder([[0.0, 1.0]])
    partial_runner = object.__new__(CurationServicesRunner)
    partial_runner.structured = StructuredTextService(_FakeGenerator([]))
    partial_runner.companion = _Companion((covered, missing))
    partial_runner.semantic = semantic
    partial_runner.embedder = embedder
    context.stage = CurationStage.V3_R10_DEDUPE
    partial = asyncio.run(partial_runner.run(context))
    dedupe = partial.payload["resolutions"][0]["dedupe"]
    assert partial.payload["resolutions"][0]["status"] == "generated"
    assert dedupe["missing_existing_vector_note_ids"] == [2]
    assert semantic.calls == [((1,), "semantic-1")] and len(embedder.calls) == 1


def test_r9_to_r10_runner_preserves_ten_numeric_split_ordinals(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    facts = _r9_facts(1)
    context, inputs = _r9_runner_artifacts(
        facts,
        [{"fact_id": "f01", "generation_allowed": True, "state": "confirmed_missing"}],
    )
    monkeypatch.setattr(stages, "_v3_phase_f_inputs", lambda _context: inputs)
    split_response = _r9_response(*("f01",) * 10)
    split_response["resolutions"] = [
        {
            **row,
            "text": f"{{{{c1::answer {index}}}}}",
            "split": True,
            "split_index": index,
        }
        for index, row in enumerate(split_response["resolutions"], start=1)
    ]
    r9_runner = _offline_runner(_FakeGenerator([split_response]))
    r9 = asyncio.run(r9_runner.run(context)).payload
    context.prior_payloads[CurationStage.V3_R9_GENERATION] = r9
    context.stage = CurationStage.V3_R10_DEDUPE
    _r0, _scope, r4, *_ = inputs
    r4.update(
        {"semantic_generation": "semantic-1", "card_identities": [], "semantic_identities": []}
    )
    r10_runner = object.__new__(CurationServicesRunner)
    r10_runner.structured = StructuredTextService(_FakeGenerator([]))
    r10_runner.companion = _Companion(())
    r10_runner.semantic = _PinnedVectors({})
    r10_runner.embedder = _Embedder([[1.0, 0.0]] * 10)
    r10 = asyncio.run(r10_runner.run(context))
    assert [row["card_id"] for row in r9["resolutions"]] == [
        f"card:f01:{index}" for index in range(1, 11)
    ]
    assert [row["card_id"] for row in r10.payload["resolutions"]] == [
        f"card:f01:{index}" for index in range(1, 11)
    ]


def test_r10_numeric_order_boundaries_and_equal_score_ties() -> None:
    existing = (
        NormalizedNote(2, "Basic", "second", "", {}, (), (), (), "b", "b" * 64, ("Deck",)),
        NormalizedNote(1, "Basic", "first", "", {}, (), (), (), "a", "a" * 64, ("Deck",)),
    )

    def result_at(score: float) -> str:
        return asyncio.run(
            DeduplicationService(_Embedder([[score, sqrt(1 - score**2)]])).classify_v3_batch(
                (V3DedupeProposal("card:f01:1", "f01", "new", ""),),
                existing[:1],
                existing_document_vectors={2: (1.0, 0.0)},
            )
        )[0].disposition

    assert result_at(0.97) == "duplicate"
    assert result_at(0.86) == "overlap"
    tied = asyncio.run(
        DeduplicationService(_Embedder([[1.0, 0.0], [0.0, 1.0]])).classify_v3_batch(
            (
                V3DedupeProposal("card:f01:1", "f01", "first new", ""),
                V3DedupeProposal("card:f02:1", "f02", "second new", ""),
            ),
            existing,
            existing_document_vectors={1: (1.0, 0.0), 2: (1.0, 0.0)},
        )
    )
    assert [row.card_id for row in tied] == ["card:f01:1", "card:f02:1"]
    assert tied[0].duplicate_of == "note:1" and len(tied) == 2
    linked_existing = (existing[1],)

    def linked(score: float):
        return asyncio.run(
            DeduplicationService(_Embedder([[score, sqrt(1 - score**2)]])).classify_v3_batch(
                (
                    V3DedupeProposal("card:f01:1", "f01", "same", ""),
                    V3DedupeProposal("card:f02:1", "f02", "same", ""),
                ),
                linked_existing,
                existing_document_vectors={1: (1.0, 0.0)},
            )
        )

    duplicate_chain = linked(0.97)
    overlap_chain = linked(0.90)
    assert duplicate_chain[1].duplicate_of == "note:1"
    assert overlap_chain[1].disposition == "overlap" and overlap_chain[1].duplicate_of is None
