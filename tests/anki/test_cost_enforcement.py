import asyncio
import hashlib
import json
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from pydantic import BaseModel

from oms_hub.anki.cost_estimator import (
    CostAuthorizationError,
    CostEstimator,
    CostKind,
    CostLedgerEntry,
    FrozenRateTable,
    HigherOrdinaryAuthorization,
    ModelRate,
    StageCostSession,
    TokenUsage,
)
from oms_hub.anki.course_policy import CourseCurationPolicy, PolicyEmphasisColor
from oms_hub.anki.domain import PipelineContractVersion
from oms_hub.anki.pipeline import _durable_cost_entries
from oms_hub.anki.stages import _GuardedEmbeddingClient, _GuardedStructuredService
from oms_hub.llm.domain import GenerationOptions, ProviderName


def _estimator() -> CostEstimator:
    return CostEstimator(
        FrozenRateTable(
            rates=(ModelRate("model", 1_000_000, 0, 0, 1_000_000, 1_000_000),),
            effective_at=datetime(2026, 8, 17, tzinfo=UTC),
            source="fixture",
        )
    )


def _entry(estimator: CostEstimator, amount: int) -> CostLedgerEntry:
    usage = TokenUsage(input_tokens=amount)
    return CostLedgerEntry(
        call_id=f"r7-{amount}",
        stage="r7",
        modality="structured",
        model="model",
        request_sha256="a" * 64,
        rate_table_sha256=estimator.rate_table.rate_table_sha256,
        estimator_version=estimator.version,
        predicted=estimator.estimate(CostKind.PREDICTED, model="model", usage=usage),
        reserved=estimator.estimate(CostKind.RESERVED, model="model", usage=usage),
    )


def test_cumulative_cost_equal_boundary_is_allowed_and_plus_one_blocks() -> None:
    estimator = _estimator()
    one = _entry(estimator, 1)
    estimator.authorize_dispatch(
        job_id="job",
        policy_sha256="a" * 64,
        ordinary_limit_microusd=1,
        hard_limit_microusd=1,
        current=one,
    )
    with pytest.raises(CostAuthorizationError, match="ordinary"):
        estimator.authorize_dispatch(
            job_id="job",
            policy_sha256="a" * 64,
            ordinary_limit_microusd=1,
            hard_limit_microusd=9,
            current=one,
            prior=(one,),
        )


def test_hard_ceiling_and_unknown_rate_fail_before_dispatch() -> None:
    estimator = _estimator()
    with pytest.raises(CostAuthorizationError, match="unknown"):
        estimator.estimate(CostKind.PREDICTED, model="unknown", usage=TokenUsage(input_tokens=1))
    with pytest.raises(CostAuthorizationError, match="hard"):
        estimator.authorize_dispatch(
            job_id="job",
            policy_sha256="a" * 64,
            ordinary_limit_microusd=9,
            hard_limit_microusd=0,
            current=_entry(estimator, 1),
        )


def test_reserved_cache_exposure_uses_worst_frozen_rate_and_observed_keeps_categories() -> None:
    estimator = CostEstimator(
        FrozenRateTable(
            rates=(ModelRate("model", 2, 7, 3, 5, 1),),
            effective_at=datetime(2026, 8, 17, tzinfo=UTC),
            source="fixture",
        )
    )
    usage = TokenUsage(input_tokens=1_000_000, cache_creation_tokens=1_000_000)
    reserved = estimator.estimate(CostKind.RESERVED, model="model", usage=usage)
    observed = estimator.estimate(
        CostKind.OBSERVED,
        model="model",
        usage=TokenUsage(cache_creation_tokens=1_000_000, cache_read_tokens=1_000_000),
    )
    assert reserved.microusd == 14 and observed.microusd == 10


def test_stage_cost_session_hard_block_and_higher_ordinary_authorization() -> None:
    estimator = _estimator()
    session = StageCostSession(
        estimator,
        job_id="job",
        policy_sha256="a" * 64,
        ordinary_limit_microusd=1,
        hard_limit_microusd=1,
    )
    with pytest.raises(CostAuthorizationError, match="hard"):
        session.reserve(
            stage="R7",
            modality="structured",
            model="model",
            request_sha256="b" * 64,
            predicted_usage=TokenUsage(input_tokens=1),
            reserved_usage=TokenUsage(input_tokens=2),
        )
    document = {
        "job_id": "job",
        "policy_sha256": "a" * 64,
        "rate_table_sha256": estimator.rate_table.rate_table_sha256,
        "estimator_version": estimator.version,
        "authorized_limit_microusd": 2,
    }
    authorization = HigherOrdinaryAuthorization(
        **document,
        authorization_sha256=hashlib.sha256(
            json.dumps(document, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest(),
    )
    assert authorization.authorized_limit_microusd == 2


def test_r0_policy_limit_drift_blocks_before_a_guarded_fake_call() -> None:
    policy = CourseCurationPolicy(
        policy_id="policy",
        revision=1,
        course_id="course",
        professor_label="professor",
        scope_instruction="scope",
        emphasis_mode="colored_text",
        emphasis_colors=(PolicyEmphasisColor(rgb="FF0000", label="red"),),
        missing_emphasis_fallback="block",
        tag_scope_mode="hard_filter",
        classification_strictness="strict",
        generation_style_profile="cloze",
        ordinary_cost_limit_microusd=10,
        hard_stop_cost_limit_microusd=20,
    )
    estimator = _estimator()
    r0 = {
        "rate_table": estimator.rate_table.document(),
        "rate_table_sha256": estimator.rate_table.rate_table_sha256,
        "cost_ledger": [],
        "cost_ledger_sha256": hashlib.sha256(b"[]").hexdigest(),
        "policy": policy.model_dump(mode="json"),
        "policy_sha256": policy.policy_sha256,
        "ordinary_cost_limit_microusd": 11,
        "hard_stop_cost_limit_microusd": 20,
    }
    context = SimpleNamespace(
        job=SimpleNamespace(id="job", policy_sha256=policy.policy_sha256), prior_payloads={}
    )
    calls = []
    with pytest.raises(CostAuthorizationError, match="drift"):
        StageCostSession.from_prior(context, r0)
    assert calls == []
    r0["ordinary_cost_limit_microusd"] = 10
    session = StageCostSession.from_prior(context, r0)
    session.reserve(
        stage="R3",
        modality="structured",
        model="model",
        request_sha256="b" * 64,
        predicted_usage=TokenUsage(input_tokens=1),
        reserved_usage=TokenUsage(input_tokens=1),
    )
    assert len(session.entries) == 1


def test_durable_terminal_entry_rehydrates_once_and_reuses_exact_replay_reservation() -> None:
    policy = CourseCurationPolicy(
        policy_id="policy",
        revision=1,
        course_id="course",
        professor_label="professor",
        scope_instruction="scope",
        emphasis_mode="transcript_emphasis",
        missing_emphasis_fallback="block",
        tag_scope_mode="disabled",
        classification_strictness="strict",
        generation_style_profile="cloze",
        ordinary_cost_limit_microusd=100,
        hard_stop_cost_limit_microusd=100,
    )
    estimator = _estimator()
    entry = _entry(estimator, 1)
    observed = estimator.estimate(
        CostKind.OBSERVED, model="model", usage=TokenUsage(input_tokens=1)
    )
    durable = {
        **entry.document(),
        "observed": {
            "kind": "observed",
            "microusd": observed.microusd,
            "usage": {
                "input_tokens": 1,
                "cache_creation_tokens": 0,
                "cache_read_tokens": 0,
                "output_tokens": 0,
                "embedding_tokens": 0,
            },
        },
    }
    r0 = {
        "rate_table": estimator.rate_table.document(),
        "rate_table_sha256": estimator.rate_table.rate_table_sha256,
        "cost_ledger": [],
        "cost_ledger_sha256": hashlib.sha256(b"[]").hexdigest(),
        "policy": policy.model_dump(mode="json"),
        "policy_sha256": policy.policy_sha256,
        "ordinary_cost_limit_microusd": 100,
        "hard_stop_cost_limit_microusd": 100,
    }
    context = SimpleNamespace(
        job=SimpleNamespace(id="job", policy_sha256=policy.policy_sha256),
        prior_payloads={},
        durable_cost_entries=(durable,),
    )
    session = StageCostSession.from_prior(context, r0)
    reused = session.reserve(
        stage="r7",
        modality="structured",
        model="model",
        request_sha256="a" * 64,
        predicted_usage=TokenUsage(input_tokens=1),
        reserved_usage=TokenUsage(input_tokens=1),
    )
    assert reused.call_id == entry.call_id and reused.observed == observed
    next_entry = session.reserve(
        stage="r7",
        modality="structured",
        model="model",
        request_sha256="a" * 64,
        predicted_usage=TokenUsage(input_tokens=1),
        reserved_usage=TokenUsage(input_tokens=1),
    )
    assert next_entry.call_id != entry.call_id


def test_terminal_transport_then_success_rehydrates_one_observed_reservation() -> None:
    estimator = _estimator()
    entry = _entry(estimator, 1)
    reservation = entry.document()
    rows: list[dict[str, object]] = []
    for call_index, events, usage in (
        (1, ("begun", "dispatched", "transport_failed"), 0),
        (2, ("begun", "dispatched", "response_received", "accepted"), 3),
    ):
        for event in events:
            rows.append(
                {
                    "id": len(rows) + 1,
                    "stage": "v3_r3_scope",
                    "stage_attempt": 1,
                    "mode": "canonical",
                    "call_index": call_index,
                    "subcall_ordinal": 0,
                    "event": event,
                    "cost_reservation": reservation,
                    "cost_reservation_sha256": hashlib.sha256(
                        json.dumps(reservation, sort_keys=True, separators=(",", ":")).encode()
                    ).hexdigest(),
                    "input_tokens": usage if event in {"response_received", "accepted"} else 0,
                    "output_tokens": 0,
                    "cache_creation_input_tokens": 0,
                    "cache_read_input_tokens": 0,
                }
            )
    job = SimpleNamespace(
        id="job",
        pipeline_contract_version=PipelineContractVersion.CARD_CENTRIC_V3,
        rate_table_document=estimator.rate_table.document(),
    )
    repository = SimpleNamespace(list_provider_attempt_events=lambda _job_id: rows)

    recovered = _durable_cost_entries(repository, job)

    assert len(recovered) == 1
    assert recovered[0]["call_id"] == entry.call_id
    assert recovered[0]["observed"]["usage"]["input_tokens"] == 3


class _Output(BaseModel):
    value: str


class _StructuredFake:
    def __init__(self, *, cache_creation: int = 0, cache_read: int = 0) -> None:
        self.calls = 0
        self.result = SimpleNamespace(
            input_tokens=12,
            output_tokens=2,
            cache_creation_input_tokens=cache_creation,
            cache_read_input_tokens=cache_read,
        )

    def generate_json(self, *_args: object, **_kwargs: object) -> SimpleNamespace:
        self.calls += 1
        return self.result


class _EmbeddingFake:
    def __init__(self) -> None:
        self.calls = 0
        self.model = "model"
        self.max_attempts = 1
        self.batch_size = 128
        self.split_on_limit = False

    async def embed(self, texts: object, *, input_type: str) -> list[list[float]]:
        self.calls += 1
        assert input_type == "query" and texts == ("query",)
        return [[1.0]]


def test_actual_guarded_runner_seams_reserve_before_fake_calls_and_reprice_observed_cache() -> None:
    table = FrozenRateTable(
        rates=(ModelRate("model", 1_000_000, 3_000_000, 2_000_000, 1_000_000, 1_000_000),),
        effective_at=datetime(2026, 8, 17, tzinfo=UTC),
        source="fixture",
    )
    session = StageCostSession(
        CostEstimator(table),
        job_id="job",
        policy_sha256="a" * 64,
        ordinary_limit_microusd=1_000,
        hard_limit_microusd=1_000,
    )
    fake = _StructuredFake(cache_creation=4, cache_read=3)
    _GuardedStructuredService(fake, session, "R3").generate_json(
        "scope",
        "payload",
        output_model=_Output,
        provider=ProviderName.OPENAI,
        model="model",
        options=GenerationOptions(cacheable_source_prefix="source", max_tokens=8),
    )
    entry = session.entries[0]
    assert fake.calls == 1 and entry.stage == "R3" and entry.modality == "structured"
    assert entry.observed is not None
    assert entry.observed.usage == TokenUsage(
        input_tokens=5, cache_creation_tokens=4, cache_read_tokens=3, output_tokens=2
    )
    embedder = _EmbeddingFake()
    guarded_embedder = _GuardedEmbeddingClient(embedder, session, "R10", "model")
    assert asyncio.run(guarded_embedder.embed(("query",), input_type="query")) == [[1.0]]
    assert embedder.calls == 1
    assert session.entries[-1].stage == "R10" and session.entries[-1].observed_estimated is True
    assert session.entries[-1].observed is not None
    assert session.entries[-1].observed.usage.embedding_tokens == 2


def test_guarded_embedding_reserves_retry_split_bound_before_delegate() -> None:
    table = FrozenRateTable(
        rates=(ModelRate("model", 1, 0, 0, 1, 1_000_000),),
        effective_at=datetime(2026, 8, 17, tzinfo=UTC),
        source="fixture",
    )
    fake = _EmbeddingFake()
    fake.max_attempts, fake.batch_size, fake.split_on_limit = 3, 2, True
    blocked = StageCostSession(
        CostEstimator(table),
        job_id="job",
        policy_sha256="a" * 64,
        ordinary_limit_microusd=1_000,
        hard_limit_microusd=10,
    )
    with pytest.raises(CostAuthorizationError, match="hard"):
        asyncio.run(
            _GuardedEmbeddingClient(fake, blocked, "R5", "model").embed(
                ("one", "two"), input_type="query"
            )
        )
    assert fake.calls == 0 and blocked.entries == []


def test_actual_guarded_structured_seam_blocks_cacheable_worst_case_before_fake_call() -> None:
    table = FrozenRateTable(
        rates=(ModelRate("model", 1_000_000, 9_000_000, 1_000_000, 1_000_000, 1_000_000),),
        effective_at=datetime(2026, 8, 17, tzinfo=UTC),
        source="fixture",
    )
    session = StageCostSession(
        CostEstimator(table),
        job_id="job",
        policy_sha256="a" * 64,
        ordinary_limit_microusd=1_000,
        hard_limit_microusd=3,
    )
    fake = _StructuredFake()
    with pytest.raises(CostAuthorizationError, match="hard"):
        _GuardedStructuredService(fake, session, "R7").generate_json(
            "instruction",
            "payload",
            output_model=_Output,
            provider=ProviderName.OPENAI,
            model="model",
            options=GenerationOptions(cacheable_source_prefix="prefix", max_tokens=1),
        )
    assert fake.calls == 0 and session.entries == []
