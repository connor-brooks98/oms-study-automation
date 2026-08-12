import asyncio
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID

import numpy as np
import pytest

import oms_hub.anki.stages as stages_module
from oms_hub.anki.card_centric import build_source_index
from oms_hub.anki.card_centric_contracts import (
    CardConcept,
    CardConceptLedger,
    CardRecord,
    ClassifierResult,
    ClassifierTelemetry,
    SemanticPreFilterResult,
    TagScopeResult,
)
from oms_hub.anki.domain import CurationStage, PipelineContractVersion, SourceKind
from oms_hub.anki.pipeline import PinnedInputChanged
from oms_hub.anki.provider_attempts import (
    ProviderAttemptBinding,
    ProviderEventEvidence,
    bind_provider_attempts,
)
from oms_hub.anki.rehearsal.vectors import ReplayEmbeddingClient
from oms_hub.anki.semantic.domain import (
    DocumentRecord,
    InputType,
    PinnedCentroidSimilarityResult,
    SemanticGenerationMismatchError,
    SemanticHit,
)
from oms_hub.anki.semantic.service import (
    SemanticCoverageError,
    SemanticIndexService,
    content_hash,
)
from oms_hub.anki.semantic.store import SemanticSnapshotStore
from oms_hub.anki.sources import SourcePassage


class FakeEmbeddingClient:
    def __init__(self, vectors: dict[str, list[float]]) -> None:
        self.vectors = vectors
        self.calls: list[tuple[InputType, tuple[str, ...]]] = []

    async def embed(
        self,
        texts: list[str] | tuple[str, ...],
        *,
        input_type: InputType,
    ) -> np.ndarray:
        self.calls.append((input_type, tuple(texts)))
        return np.asarray([self.vectors[text] for text in texts], dtype=np.float32)


def _record(note_id: int, text: str) -> DocumentRecord:
    return DocumentRecord(note_id=note_id, text=text, content_hash=content_hash(text))


def _semantic_service(
    tmp_path,
    embedder: FakeEmbeddingClient,
    *,
    min_coverage: float = 0.0,
) -> SemanticIndexService:
    return SemanticIndexService(
        SemanticSnapshotStore(tmp_path / "semantic"),
        embedder,
        model="fixture",
        dimensions=3,
        min_coverage=min_coverage,
        query_cache_size=8,
    )


def _card(note_id: int) -> CardRecord:
    return CardRecord(
        note_id=note_id,
        content_sha256=f"{note_id:064x}",
        text=f"card {note_id}",
        extra="",
        tags=(),
        deck_names=(),
    )


def _telemetry() -> ClassifierTelemetry:
    return ClassifierTelemetry(
        batch_count=0,
        cache_prefix_sha256="a" * 64,
        cache_mode="ordinary_prefix",
        provider="openai",
        model="fixture",
        request_ids=(),
        batches=(),
    )


def _v2_residual_audit_context(
    cards: tuple[CardRecord, ...],
    residual_payload: object,
) -> SimpleNamespace:
    return SimpleNamespace(
        job=SimpleNamespace(pipeline_contract_version=PipelineContractVersion.CARD_CENTRIC_V2),
        prior_payloads={
            CurationStage.SOURCE_INDEX: {
                "cards": [card.model_dump(mode="json") for card in cards]
            },
            CurationStage.CARD_CLASSIFY: {
                "classifier": ClassifierResult(
                    results=(), telemetry=_telemetry()
                ).model_dump(mode="json")
            },
            CurationStage.CARD_RESIDUAL: residual_payload,
        },
    )


def test_pinned_centroid_similarity_embeds_terms_separately_and_normalizes_centroid(
    tmp_path,
) -> None:
    async def scenario() -> None:
        embedder = FakeEmbeddingClient(
            {
                "centroid note": [1.0, 1.0, 0.0],
                "primary note": [1.0, 0.0, 0.0],
                "Primary": [1.0, 0.0, 0.0],
                "Alias": [0.0, 1.0, 0.0],
            }
        )
        service = _semantic_service(tmp_path, embedder)
        generation = await service.refresh(
            [_record(10, "centroid note"), _record(20, "primary note")]
        )

        result = await service.pinned_centroid_similarity(
            (("Primary", "Alias"),),
            note_ids=(20, 10),
            expected_generation=str(generation.manifest.generation),
        )

        assert embedder.calls[-1] == ("query", ("Primary", "Alias"))
        assert result.unavailable_note_ids == ()
        assert result.scores[10] == pytest.approx(1.0, abs=0.001)
        assert result.scores[20] == pytest.approx(2**-0.5, abs=0.001)

    asyncio.run(scenario())


def test_pinned_centroid_similarity_blocks_invalid_pinned_note_vector(tmp_path) -> None:
    async def scenario() -> None:
        embedder = FakeEmbeddingClient({"query": [1.0, 0.0, 0.0]})
        service = _semantic_service(tmp_path, embedder)
        manifest = service.store.replace(
            [_record(1, "stored")],
            np.asarray([[0.0, 0.0, 0.0]], dtype=np.float32),
            model="fixture",
        )

        with pytest.raises(ValueError, match="cannot contain zero vectors"):
            await service.pinned_centroid_similarity(
                (("query",),),
                note_ids=(1,),
                expected_generation=str(manifest.generation),
            )

    asyncio.run(scenario())


def test_pinned_centroid_similarity_reports_valid_snapshot_unavailable_notes(tmp_path) -> None:
    async def scenario() -> None:
        embedder = FakeEmbeddingClient(
            {"stored": [1.0, 0.0, 0.0], "Primary": [1.0, 0.0, 0.0]}
        )
        service = _semantic_service(tmp_path, embedder, min_coverage=0.5)
        generation = await service.refresh(
            [_record(1, "stored")], expected_note_ids=(1, 2)
        )

        result = await service.pinned_centroid_similarity(
            (("Primary",),),
            note_ids=(1, 2),
            expected_generation=str(generation.manifest.generation),
        )

        assert result.scores == {1: pytest.approx(1.0, abs=0.001)}
        assert result.unavailable_note_ids == (2,)
        with pytest.raises(SemanticCoverageError, match="lacks scoped notes"):
            await service.pinned_similarity(
                ("Primary",),
                note_ids=(1, 2),
                expected_generation=str(generation.manifest.generation),
            )

    asyncio.run(scenario())


def test_generation_aware_search_rejects_a_replacement_snapshot(tmp_path) -> None:
    async def scenario() -> None:
        embedder = FakeEmbeddingClient(
            {"stored": [1.0, 0.0, 0.0], "query": [1.0, 0.0, 0.0]}
        )
        service = _semantic_service(tmp_path, embedder)
        generation = await service.refresh([_record(1, "stored")])

        with pytest.raises(SemanticGenerationMismatchError, match="no longer active"):
            await service.search(
                ("query",),
                eligible_note_ids=(1,),
                limit=1,
                expected_generation=f"{generation.manifest.generation}-replacement",
            )

    asyncio.run(scenario())


def test_prefilter_routes_only_reported_unavailable_notes_to_s4b_and_audits_them() -> None:
    class FakeSemantic:
        async def pinned_centroid_similarity(self, terms, **kwargs):
            assert terms == (("Primary", "Alias"),)
            assert kwargs == {"note_ids": (1, 2, 3), "expected_generation": "generation"}
            return PinnedCentroidSimilarityResult(
                scores={1: 0.80, 2: 0.20}, unavailable_note_ids=(3,)
            )

    scope = TagScopeResult(
        snapshot_id="snapshot",
        filters_sha256="b" * 64,
        scoped_note_ids=(1, 2, 3),
        unscoped_note_ids=(),
    )
    ledger = CardConceptLedger(
        lecture_entity_count=1,
        concepts=(
            CardConcept(
                concept_id="C01",
                canonical_statement="fact",
                primary_entity="  Primary ",
                aliases=("Alias",),
                depth="deep",
                emphasis_flag=False,
                importance="high",
            ),
        ),
    )
    runner = stages_module.CurationServicesRunner.__new__(stages_module.CurationServicesRunner)
    runner.semantic = FakeSemantic()
    context = SimpleNamespace(
        job=SimpleNamespace(
            semantic_generation="generation",
            pipeline_contract_version=PipelineContractVersion.CARD_CENTRIC_V2,
        ),
        prior_payloads={
            CurationStage.SOURCE_INDEX: {
                "cards": [_card(1).model_dump(), _card(2).model_dump(), _card(3).model_dump()]
            },
            CurationStage.CARD_TAG_SCOPE: {"scope": scope.model_dump(mode="json")},
            CurationStage.CARD_LEDGER: {"ledger": ledger.model_dump(mode="json")},
        },
    )

    product = asyncio.run(runner._card_prefilter(context))

    assert product.payload["pre_filtered_note_ids"] == [1, 3]
    assert product.payload["pre_excluded_note_ids"] == [2]
    assert product.payload["embedding_unavailable_note_ids"] == [3]
    context.prior_payloads[CurationStage.CARD_PREFILTER] = product.payload
    prefilter, unavailable = stages_module._read_card_prefilter(context, scope)
    assert prefilter.pre_filtered_note_ids == (1, 3)
    assert unavailable == (3,)


def test_bound_s4a_and_s6_query_embeddings_replay_with_stable_provider_events(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """S4a/S6 query embeds must be replayable provider attempts, never unscoped calls."""
    cards = (_card(1), _card(2))
    passage = SourcePassage.create(
        revision_id=1,
        lecture_id=1,
        artifact_id="replay",
        source_kind=SourceKind.SLIDE,
        locator="slide:1",
        text="Primary evidence",
        slide_number=1,
    )
    source = build_source_index(
        (passage,), snapshot_id="replay", source_revision_hashes={1: "a" * 64}
    )
    scope = TagScopeResult(
        snapshot_id="replay",
        filters_sha256="b" * 64,
        scoped_note_ids=(1, 2),
        unscoped_note_ids=(),
    )
    ledger = CardConceptLedger(
        lecture_entity_count=1,
        concepts=(
            CardConcept(
                concept_id="C01",
                canonical_statement="Primary fact",
                primary_entity="Primary",
                depth="deep",
                emphasis_flag=False,
                importance="high",
            ),
        ),
    )
    replay = ReplayEmbeddingClient(tmp_path / "replay-vectors", model="fixture", dimensions=2)
    replay.seed(
        ("Primary", "Primary Primary"),
        input_type="query",
        vectors=np.asarray(((1.0, 0.0), (1.0, 0.0)), dtype=np.float32),
    )
    semantic_store = SemanticSnapshotStore(tmp_path / "semantic")
    generation = semantic_store.replace(
        (_record(1, "card 1"), _record(2, "card 2")),
        np.asarray(((1.0, 0.0), (0.0, 1.0)), dtype=np.float32),
        model="fixture",
    )
    configuration = SimpleNamespace(residual_s6=SimpleNamespace(provider="openai", model="fixture"))

    class EmptyClassifier:
        async def classify(self, _cards: object, **_kwargs: object) -> ClassifierResult:
            return ClassifierResult(results=(), telemetry=_telemetry())

    monkeypatch.setattr(
        stages_module,
        "_card_classifier_for_version",
        lambda *_args, **_kwargs: (EmptyClassifier(), None),
    )

    def run_once() -> tuple[list[ProviderEventEvidence], list[ProviderEventEvidence]]:
        runner = stages_module.CurationServicesRunner.__new__(stages_module.CurationServicesRunner)
        runner.semantic = SemanticIndexService(
            semantic_store,
            replay,
            model="fixture",
            dimensions=2,
            min_coverage=0.0,
            query_cache_size=8,
        )
        runner.structured = SimpleNamespace(generator=SimpleNamespace())  # type: ignore[assignment]
        runner.prompts = SimpleNamespace()  # type: ignore[assignment]
        context = SimpleNamespace(
            job=SimpleNamespace(
                semantic_generation=str(generation.generation),
                pipeline_contract_version=PipelineContractVersion.CARD_CENTRIC_V2,
                resolved_model_config=configuration,
            ),
            prior_payloads={
                CurationStage.SOURCE_INDEX: {
                    "source_index": source.model_dump(mode="json"),
                    "cards": [card.model_dump(mode="json") for card in cards],
                },
                CurationStage.CARD_TAG_SCOPE: {
                    "scope": scope.model_dump(mode="json"),
                    "residual_mode": "gaps_only",
                },
                CurationStage.CARD_LEDGER: {"ledger": ledger.model_dump(mode="json")},
                CurationStage.CARD_COVERAGE: {
                    "coverage": {"C01": {"status": "uncovered", "evidence": []}}
                },
                CurationStage.CARD_FAST_CLASSIFY: {
                    "fast_classifier": {"results": []},
                    "fallback_note_ids": [],
                },
                CurationStage.CARD_CLASSIFY: {
                    "classifier": ClassifierResult(
                        results=(), telemetry=_telemetry()
                    ).model_dump(mode="json")
                },
            },
        )
        s4a_events: list[ProviderEventEvidence] = []
        with bind_provider_attempts(
            ProviderAttemptBinding(
                job_id=UUID("12345678-1234-5678-1234-567812345678"),
                stage=CurationStage.CARD_PREFILTER,
                stage_attempt=1,
                mode="canonical",
                recorder=s4a_events.append,
                replay_namespace="bound-query-replay",
            )
        ):
            prefilter = asyncio.run(runner._card_prefilter(context))  # type: ignore[arg-type]
        context.prior_payloads[CurationStage.CARD_PREFILTER] = prefilter.payload
        s6_events: list[ProviderEventEvidence] = []
        with bind_provider_attempts(
            ProviderAttemptBinding(
                job_id=UUID("12345678-1234-5678-1234-567812345678"),
                stage=CurationStage.CARD_RESIDUAL,
                stage_attempt=1,
                mode="canonical",
                recorder=s6_events.append,
                replay_namespace="bound-query-replay",
            )
        ):
            asyncio.run(runner._card_residual(context))  # type: ignore[arg-type]
        return s4a_events, s6_events

    first_s4a, first_s6 = run_once()
    second_s4a, second_s6 = run_once()
    primary_hash = hashlib.sha256(b"Primary").hexdigest()
    residual_hash = hashlib.sha256(b"Primary Primary").hexdigest()
    expected_input_hashes = (
        hashlib.sha256(json.dumps([primary_hash], separators=(",", ":")).encode()).hexdigest(),
        hashlib.sha256(json.dumps([residual_hash], separators=(",", ":")).encode()).hexdigest(),
    )

    for events, stage, expected_input_hash in (
        (first_s4a, CurationStage.CARD_PREFILTER, expected_input_hashes[0]),
        (first_s6, CurationStage.CARD_RESIDUAL, expected_input_hashes[1]),
    ):
        assert [e.event.event for e in events] == [
            "begun",
            "dispatched",
            "response_received",
            "accepted",
        ]
        assert {e.event.identity.stage for e in events} == {stage}
        assert {e.event.identity.batch_index for e in events} == {0}
        assert {e.event.identity.batch_note_ids for e in events} == {(1, 2)}
        assert {e.event.identity.kind for e in events} == {"query_embedding"}
        assert {e.event.identity.subcall_ordinal for e in events} == {0}
        assert {e.input_sha256 for e in events} == {expected_input_hash}
        assert all(e.event.response_sha256 for e in events if e.event.event == "response_received")
        assert events[-1].event.event == "accepted"
    assert first_s4a[0].event.request_sha256 == second_s4a[0].event.request_sha256
    assert first_s6[0].event.request_sha256 == second_s6[0].event.request_sha256
    assert replay.evidence.query_replay_hits == 4
    assert replay.evidence.live_query_calls == replay.evidence.live_document_calls == 0


def test_v1_prefilter_preserves_concatenated_pinned_similarity_contract() -> None:
    class FakeSemantic:
        async def pinned_similarity(self, queries, **kwargs):
            assert queries == ("Primary Alias",)
            assert kwargs == {"note_ids": (1, 2), "expected_generation": "generation"}
            return {1: 0.80, 2: 0.20}

        async def pinned_centroid_similarity(self, *_args, **_kwargs):
            raise AssertionError("v1 must not use centroid scoring")

    scope = TagScopeResult(
        snapshot_id="snapshot",
        filters_sha256="b" * 64,
        scoped_note_ids=(1, 2),
        unscoped_note_ids=(),
    )
    ledger = CardConceptLedger(
        lecture_entity_count=1,
        concepts=(
            CardConcept(
                concept_id="C01",
                canonical_statement="fact",
                primary_entity="Primary",
                aliases=("Alias",),
                depth="deep",
                emphasis_flag=False,
                importance="high",
            ),
        ),
    )
    runner = stages_module.CurationServicesRunner.__new__(stages_module.CurationServicesRunner)
    runner.semantic = FakeSemantic()
    context = SimpleNamespace(
        job=SimpleNamespace(
            semantic_generation="generation",
            pipeline_contract_version=PipelineContractVersion.CARD_CENTRIC_V1,
        ),
        prior_payloads={
            CurationStage.SOURCE_INDEX: {
                "cards": [_card(1).model_dump(), _card(2).model_dump()]
            },
            CurationStage.CARD_TAG_SCOPE: {"scope": scope.model_dump(mode="json")},
            CurationStage.CARD_LEDGER: {"ledger": ledger.model_dump(mode="json")},
        },
    )

    product = asyncio.run(runner._card_prefilter(context))

    assert product.payload["pre_filtered_note_ids"] == [1]
    assert product.payload["pre_excluded_note_ids"] == [2]
    assert "embedding_unavailable_note_ids" not in product.payload


def test_prefilter_read_blocks_incomplete_s4a_partition() -> None:
    scope = TagScopeResult(
        snapshot_id="snapshot",
        filters_sha256="b" * 64,
        scoped_note_ids=(1, 2),
        unscoped_note_ids=(),
    )
    prefilter = SemanticPreFilterResult(
        pre_filtered_note_ids=(1,),
        pre_excluded_note_ids=(),
        threshold=0.55,
        similarity_stats={"min": 0.1, "max": 0.1, "mean": 0.1, "median": 0.1},
    )
    context = SimpleNamespace(
        prior_payloads={CurationStage.CARD_PREFILTER: prefilter.model_dump(mode="json")}
    )

    with pytest.raises(PinnedInputChanged, match="does not partition"):
        stages_module._read_card_prefilter(context, scope)


def test_residual_excludes_blank_cards_audits_eligibility_and_binds_generation(monkeypatch) -> None:
    passage = SourcePassage.create(
        revision_id=1,
        lecture_id=1,
        artifact_id="slides",
        source_kind=SourceKind.SLIDE,
        locator="slide:1",
        text="evidence",
        slide_number=1,
    )
    source = build_source_index(
        (passage,), snapshot_id="snapshot", source_revision_hashes={1: "c" * 64}
    )
    blank = _card(1).model_copy(update={"text": " \t", "extra": "\n"})
    extra_only = _card(2).model_copy(
        update={"text": " \t", "extra": "Extra supplies semantic text"}
    )
    ordinary = _card(3)
    scope = TagScopeResult(
        snapshot_id="snapshot",
        filters_sha256="b" * 64,
        scoped_note_ids=(1,),
        unscoped_note_ids=(2, 3),
    )
    ledger = CardConceptLedger(
        lecture_entity_count=1,
        concepts=(
            CardConcept(
                concept_id="C01",
                canonical_statement="fact",
                primary_entity="Primary",
                depth="deep",
                emphasis_flag=False,
                importance="high",
            ),
        ),
    )

    class FakeSemantic:
        async def search(self, queries, **kwargs):
            assert queries == ("Primary Primary",)
            assert kwargs["expected_generation"] == "generation"
            assert kwargs["eligible_note_ids"] == {2, 3}
            return [[SemanticHit(note_id=2, score=0.80, content_hash=extra_only.content_sha256)]]

    async def fake_classify(_self, cards, **_kwargs):
        assert tuple(card.note_id for card in cards) == (2,)
        return ClassifierResult(results=(), telemetry=_telemetry())

    monkeypatch.setattr(stages_module.CardCentricClassifier, "classify", fake_classify)
    runner = stages_module.CurationServicesRunner.__new__(stages_module.CurationServicesRunner)
    runner.semantic = FakeSemantic()
    runner.structured = SimpleNamespace(generator=SimpleNamespace())
    runner.prompts = SimpleNamespace()
    monkeypatch.setattr(stages_module, "_card_classifier_prompt", lambda _catalog: "fixture")
    context = SimpleNamespace(
        job=SimpleNamespace(
            semantic_generation="generation",
            pipeline_contract_version=PipelineContractVersion.CARD_CENTRIC_V2,
            resolved_model_config=SimpleNamespace(
                residual_s6=SimpleNamespace(provider="openai", model="fixture")
            ),
        ),
        prior_payloads={
            CurationStage.PREFLIGHT: {
                "prompt_snapshot": [
                    {
                        "id": "card-centric-classifier",
                        "version": "2.0.0",
                        "prompt_hash": hashlib.sha256(
                            b"Pinned classifier instruction"
                        ).hexdigest()[:12],
                        "content": "Pinned classifier instruction",
                        "metadata": {
                            "id": "card-centric-classifier",
                            "version": "2.0.0",
                            "schema": "card_centric_classify_v1",
                            "response_format": "json",
                        },
                    }
                ]
            },
            CurationStage.SOURCE_INDEX: {
                "source_index": source.model_dump(mode="json"),
                "cards": [
                    blank.model_dump(mode="json"),
                    extra_only.model_dump(mode="json"),
                    ordinary.model_dump(mode="json"),
                ],
            },
            CurationStage.CARD_LEDGER: {"ledger": ledger.model_dump(mode="json")},
            CurationStage.CARD_COVERAGE: {
                "coverage": {"C01": {"status": "uncovered", "evidence": []}}
            },
            CurationStage.CARD_TAG_SCOPE: {
                "scope": scope.model_dump(mode="json"),
                "residual_mode": "gaps_only",
            },
            CurationStage.CARD_FAST_CLASSIFY: {
                "fast_classifier": {"results": []},
                "fallback_note_ids": [1],
            },
            CurationStage.CARD_CLASSIFY: {
                "classifier": ClassifierResult(results=(), telemetry=_telemetry()).model_dump(
                    mode="json"
                )
            },
        },
    )

    product = asyncio.run(runner._card_residual(context))
    repeated = asyncio.run(runner._card_residual(context))

    assert product.payload["audits"] == [
        {
            "concept_id": "C01",
            "query": "Primary Primary",
            "hit_note_ids": [2],
            "semantic_scores": {"2": 0.80},
            "below_classification_threshold_note_ids": [],
            "classified_note_ids": [2],
            "semantic_skip": False,
            "disposition": "classified",
        }
    ]
    assert product.payload["embedding_unavailable_blank_note_ids"] == [1]
    assert product.payload["searchable_note_count"] == 2
    assert product.payload["semantic_eligibility_audit_version"] == "v1"
    assert (
        product.payload["semantic_eligibility_audit_domain"]
        == "oms-study-automation:anki:card_centric_v2:s6:semantic_eligibility"
    )
    assert product.payload["searchable_note_ids_sha256"] == hashlib.sha256(
        b'{"domain":"oms-study-automation:anki:card_centric_v2:s6:semantic_eligibility","searchable_note_ids":[2,3],"version":"v1"}'
    ).hexdigest()
    assert json.dumps(product.payload, sort_keys=True, separators=(",", ":")) == json.dumps(
        repeated.payload, sort_keys=True, separators=(",", ":")
    )


def test_v1_residual_search_omits_generation_pin_when_absent_or_none(monkeypatch) -> None:
    passage = SourcePassage.create(
        revision_id=1,
        lecture_id=1,
        artifact_id="slides",
        source_kind=SourceKind.SLIDE,
        locator="slide:1",
        text="evidence",
        slide_number=1,
    )
    source = build_source_index(
        (passage,), snapshot_id="snapshot", source_revision_hashes={1: "c" * 64}
    )
    card = _card(1)
    scope = TagScopeResult(
        snapshot_id="snapshot",
        filters_sha256="b" * 64,
        scoped_note_ids=(),
        unscoped_note_ids=(1,),
    )
    ledger = CardConceptLedger(
        lecture_entity_count=1,
        concepts=(
            CardConcept(
                concept_id="C01",
                canonical_statement="fact",
                primary_entity="Primary",
                depth="deep",
                emphasis_flag=False,
                importance="high",
            ),
        ),
    )
    seen_search_kwargs: list[dict[str, object]] = []

    class FakeSemantic:
        async def search(self, _queries, **kwargs):
            seen_search_kwargs.append(kwargs)
            return [[SemanticHit(note_id=1, score=0.9, content_hash=card.content_sha256)]]

    async def fake_classify(_self, cards, **_kwargs):
        assert tuple(item.note_id for item in cards) == (1,)
        return ClassifierResult(results=(), telemetry=_telemetry())

    monkeypatch.setattr(stages_module.CardCentricClassifier, "classify", fake_classify)
    monkeypatch.setattr(stages_module, "_card_classifier_prompt", lambda _catalog: "fixture")
    runner = stages_module.CurationServicesRunner.__new__(stages_module.CurationServicesRunner)
    runner.semantic = FakeSemantic()
    runner.structured = SimpleNamespace(generator=SimpleNamespace())
    runner.prompts = SimpleNamespace()
    shared_payloads = {
        CurationStage.SOURCE_INDEX: {
            "source_index": source.model_dump(mode="json"),
            "cards": [card.model_dump(mode="json")],
        },
        CurationStage.CARD_LEDGER: {"ledger": ledger.model_dump(mode="json")},
        CurationStage.CARD_COVERAGE: {
            "coverage": {"C01": {"status": "uncovered", "evidence": []}}
        },
        CurationStage.CARD_TAG_SCOPE: {
            "scope": scope.model_dump(mode="json"),
            "residual_mode": "gaps_only",
        },
    }
    for job in (
        SimpleNamespace(
            pipeline_contract_version=PipelineContractVersion.CARD_CENTRIC_V1,
            resolved_model_config=SimpleNamespace(
                residual_s6=SimpleNamespace(provider="openai", model="fixture")
            ),
        ),
        SimpleNamespace(
            pipeline_contract_version=PipelineContractVersion.CARD_CENTRIC_V1,
            semantic_generation=None,
            resolved_model_config=SimpleNamespace(
                residual_s6=SimpleNamespace(provider="openai", model="fixture")
            ),
        ),
    ):
        asyncio.run(
            runner._card_residual(
                SimpleNamespace(job=job, prior_payloads=shared_payloads)
            )
        )

    assert seen_search_kwargs == [
        {"eligible_note_ids": {1}, "limit": 12},
        {"eligible_note_ids": {1}, "limit": 12},
    ]


def test_v2_residual_missing_nonblank_pinned_manifest_note_is_fatal(tmp_path) -> None:
    async def scenario() -> None:
        semantic = _semantic_service(
            tmp_path,
            FakeEmbeddingClient({"indexed note": [1.0, 0.0, 0.0]}),
        )
        generation = await semantic.refresh([_record(1, "indexed note")])
        runner = stages_module.CurationServicesRunner.__new__(stages_module.CurationServicesRunner)
        runner.semantic = semantic
        ledger = CardConceptLedger(
            lecture_entity_count=1,
            concepts=(
                CardConcept(
                    concept_id="C01",
                    canonical_statement="fact",
                    primary_entity="Primary",
                    depth="deep",
                    emphasis_flag=False,
                    importance="high",
                ),
            ),
        )
        blank = _card(1).model_copy(update={"text": "", "extra": "  "})
        missing = _card(2).model_copy(update={"text": "nonblank missing manifest", "extra": ""})
        context = SimpleNamespace(
            job=SimpleNamespace(
                semantic_generation=str(generation.manifest.generation),
                pipeline_contract_version=PipelineContractVersion.CARD_CENTRIC_V2,
            ),
            prior_payloads={
                CurationStage.SOURCE_INDEX: {
                    "cards": [blank.model_dump(mode="json"), missing.model_dump(mode="json")]
                },
                CurationStage.CARD_LEDGER: {"ledger": ledger.model_dump(mode="json")},
                CurationStage.CARD_COVERAGE: {
                    "coverage": {"C01": {"status": "uncovered", "evidence": []}}
                },
                CurationStage.CARD_TAG_SCOPE: {
                    "scope": TagScopeResult(
                        snapshot_id="snapshot",
                        filters_sha256="b" * 64,
                        scoped_note_ids=(),
                        unscoped_note_ids=(1, 2),
                    ).model_dump(mode="json"),
                    "residual_mode": "gaps_only",
                },
            },
        )

        with pytest.raises(SemanticCoverageError, match="lacks eligible notes"):
            await runner._card_residual(context)

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "cards",
    (
        [{"note_id": 1}],
        [_card(1).model_dump(mode="json"), _card(1).model_dump(mode="json")],
    ),
)
def test_v2_residual_rejects_malformed_or_duplicate_source_card_accounting(cards) -> None:
    ledger = CardConceptLedger(
        lecture_entity_count=1,
        concepts=(
            CardConcept(
                concept_id="C01",
                canonical_statement="fact",
                primary_entity="Primary",
                depth="deep",
                emphasis_flag=False,
                importance="high",
            ),
        ),
    )
    context = SimpleNamespace(
        job=SimpleNamespace(pipeline_contract_version=PipelineContractVersion.CARD_CENTRIC_V2),
        prior_payloads={
            CurationStage.SOURCE_INDEX: {"cards": cards},
            CurationStage.CARD_LEDGER: {"ledger": ledger.model_dump(mode="json")},
            CurationStage.CARD_COVERAGE: {
                "coverage": {"C01": {"status": "uncovered", "evidence": []}}
            },
            CurationStage.CARD_TAG_SCOPE: {
                "scope": TagScopeResult(
                    snapshot_id="snapshot",
                    filters_sha256="b" * 64,
                    scoped_note_ids=(),
                    unscoped_note_ids=(),
                ).model_dump(mode="json"),
                "residual_mode": "gaps_only",
            },
        },
    )
    runner = stages_module.CurationServicesRunner.__new__(stages_module.CurationServicesRunner)

    with pytest.raises(
        PinnedInputChanged,
        match="source-index (cards are malformed|has duplicate cards)",
    ):
        asyncio.run(runner._card_residual(context))


def test_v2_residual_no_targets_persists_and_downstream_validates_semantic_audit() -> None:
    ledger = CardConceptLedger(
        lecture_entity_count=1,
        concepts=(
            CardConcept(
                concept_id="C01",
                canonical_statement="fact",
                primary_entity="Primary",
                depth="deep",
                emphasis_flag=False,
                importance="high",
            ),
        ),
    )
    blank = _card(1).model_copy(update={"text": " ", "extra": "\n"})
    searchable = _card(2)
    context = SimpleNamespace(
        job=SimpleNamespace(pipeline_contract_version=PipelineContractVersion.CARD_CENTRIC_V2),
        prior_payloads={
            CurationStage.SOURCE_INDEX: {
                "cards": [searchable.model_dump(mode="json"), blank.model_dump(mode="json")]
            },
            CurationStage.CARD_LEDGER: {"ledger": ledger.model_dump(mode="json")},
            CurationStage.CARD_COVERAGE: {
                "coverage": {"C01": {"status": "covered", "evidence": []}}
            },
            CurationStage.CARD_TAG_SCOPE: {
                "scope": TagScopeResult(
                    snapshot_id="snapshot",
                    filters_sha256="b" * 64,
                    scoped_note_ids=(),
                    unscoped_note_ids=(1, 2),
                ).model_dump(mode="json"),
                "residual_mode": "gaps_only",
            },
            CurationStage.CARD_CLASSIFY: {
                "classifier": ClassifierResult(
                    results=(), telemetry=_telemetry()
                ).model_dump(mode="json")
            },
        },
    )
    runner = stages_module.CurationServicesRunner.__new__(stages_module.CurationServicesRunner)
    residual = asyncio.run(runner._card_residual(context))

    assert residual.payload["uncovered_concept_ids"] == []
    assert residual.payload["embedding_unavailable_blank_note_ids"] == [1]
    assert residual.payload["searchable_note_count"] == 1
    context.prior_payloads[CurationStage.CARD_RESIDUAL] = residual.payload
    assert stages_module._all_card_classifications(context) == ()
    context.prior_payloads[CurationStage.CARD_RESIDUAL]["searchable_note_count"] = 2
    with pytest.raises(PinnedInputChanged, match="semantic eligibility audit changed"):
        stages_module._all_card_classifications(context)


def test_v2_residual_all_blank_cards_searches_empty_eligible_universe(monkeypatch) -> None:
    passage = SourcePassage.create(
        revision_id=1,
        lecture_id=1,
        artifact_id="slides",
        source_kind=SourceKind.SLIDE,
        locator="slide:1",
        text="evidence",
        slide_number=1,
    )
    source = build_source_index(
        (passage,), snapshot_id="snapshot", source_revision_hashes={1: "c" * 64}
    )
    ledger = CardConceptLedger(
        lecture_entity_count=1,
        concepts=(
            CardConcept(
                concept_id="C01",
                canonical_statement="fact",
                primary_entity="Primary",
                depth="deep",
                emphasis_flag=False,
                importance="high",
            ),
        ),
    )
    blank_cards = (
        _card(2).model_copy(update={"text": "\t", "extra": ""}),
        _card(1).model_copy(update={"text": "", "extra": "\n"}),
    )

    class FakeSemantic:
        def __init__(self) -> None:
            self.calls: list[dict[str, object]] = []

        async def search(self, queries, **kwargs):
            assert queries == ("Primary Primary",)
            self.calls.append(kwargs)
            assert kwargs["eligible_note_ids"] == set()
            assert kwargs["expected_generation"] == "generation"
            return [[]]

    class FakeClassifier:
        def __init__(self) -> None:
            self.cards: list[tuple[CardRecord, ...]] = []

        async def classify(self, cards, **_kwargs):
            self.cards.append(tuple(cards))
            assert cards == ()
            return ClassifierResult(results=(), telemetry=_telemetry())

    semantic = FakeSemantic()
    classifier = FakeClassifier()
    monkeypatch.setattr(
        stages_module,
        "_card_classifier_for_version",
        lambda *_args, **_kwargs: (classifier, None),
    )
    runner = stages_module.CurationServicesRunner.__new__(stages_module.CurationServicesRunner)
    runner.semantic = semantic
    runner.structured = SimpleNamespace(generator=SimpleNamespace())
    runner.prompts = SimpleNamespace()
    context = SimpleNamespace(
        job=SimpleNamespace(
            semantic_generation="generation",
            pipeline_contract_version=PipelineContractVersion.CARD_CENTRIC_V2,
            resolved_model_config=SimpleNamespace(
                residual_s6=SimpleNamespace(provider="openai", model="fixture")
            ),
        ),
        prior_payloads={
            CurationStage.SOURCE_INDEX: {
                "source_index": source.model_dump(mode="json"),
                "cards": [card.model_dump(mode="json") for card in blank_cards],
            },
            CurationStage.CARD_LEDGER: {"ledger": ledger.model_dump(mode="json")},
            CurationStage.CARD_COVERAGE: {
                "coverage": {"C01": {"status": "uncovered", "evidence": []}}
            },
            CurationStage.CARD_TAG_SCOPE: {
                "scope": TagScopeResult(
                    snapshot_id="snapshot",
                    filters_sha256="b" * 64,
                    scoped_note_ids=(),
                    unscoped_note_ids=(1, 2),
                ).model_dump(mode="json"),
                "residual_mode": "gaps_only",
            },
            CurationStage.CARD_FAST_CLASSIFY: {
                "fast_classifier": {"results": []},
                "fallback_note_ids": [],
            },
            CurationStage.CARD_CLASSIFY: {
                "classifier": ClassifierResult(results=(), telemetry=_telemetry()).model_dump(
                    mode="json"
                )
            },
        },
    )

    product = asyncio.run(runner._card_residual(context))
    repeated = asyncio.run(runner._card_residual(context))

    assert semantic.calls == [
        {"eligible_note_ids": set(), "limit": 12, "expected_generation": "generation"},
        {"eligible_note_ids": set(), "limit": 12, "expected_generation": "generation"},
    ]
    assert classifier.cards == [(), ()]
    assert product.payload["audits"] == [
        {
            "concept_id": "C01",
            "query": "Primary Primary",
            "hit_note_ids": [],
            "semantic_scores": {},
            "below_classification_threshold_note_ids": [],
            "classified_note_ids": [],
            "semantic_skip": True,
            "disposition": "semantic_skip",
        }
    ]
    assert product.payload["embedding_unavailable_blank_note_ids"] == [1, 2]
    assert product.payload["searchable_note_count"] == 0
    assert product.payload["searchable_note_ids_sha256"] == hashlib.sha256(
        b'{"domain":"oms-study-automation:anki:card_centric_v2:s6:semantic_eligibility","searchable_note_ids":[],"version":"v1"}'
    ).hexdigest()
    assert json.dumps(product.payload, sort_keys=True, separators=(",", ":")) == json.dumps(
        repeated.payload, sort_keys=True, separators=(",", ":")
    )


@pytest.mark.parametrize(
    ("target", "value"),
    (
        ("searchable_note_count", 1.0),
        ("searchable_note_count", True),
        ("embedding_unavailable_blank_note_ids", (1,)),
        ("embedding_unavailable_blank_note_ids", {"note_id": 1}),
        ("embedding_unavailable_blank_note_ids", [True]),
        ("embedding_unavailable_blank_note_ids", [1.0]),
        ("embedding_unavailable_blank_note_ids", [1, 1]),
        ("embedding_unavailable_blank_note_ids", [2, 1]),
        ("searchable_note_ids_sha256", "z" * 64),
        ("semantic_eligibility_audit_version", "v2"),
        ("semantic_eligibility_audit_domain", "wrong-domain"),
        ("payload", []),
    ),
)
def test_v2_residual_downstream_rejects_type_confusion_and_tampered_audit(
    target: str,
    value: object,
) -> None:
    cards = (
        _card(1).model_copy(update={"text": "", "extra": ""}),
        _card(2),
    )
    _, audit = stages_module._card_residual_v2_semantic_audit(
        {card.note_id: card for card in cards}
    )
    residual_payload: object = {"classifier": None, **audit}
    context = _v2_residual_audit_context(cards, residual_payload)
    if target == "payload":
        context.prior_payloads[CurationStage.CARD_RESIDUAL] = value
    else:
        assert isinstance(residual_payload, dict)
        residual_payload[target] = value

    with pytest.raises(PinnedInputChanged, match="semantic eligibility audit"):
        stages_module._all_card_classifications(context)


@pytest.mark.parametrize(
    "missing_field",
    (
        "semantic_eligibility_audit_version",
        "semantic_eligibility_audit_domain",
        "embedding_unavailable_blank_note_ids",
        "searchable_note_count",
        "searchable_note_ids_sha256",
    ),
)
def test_v2_residual_downstream_rejects_partial_semantic_audit(missing_field: str) -> None:
    cards = (_card(1),)
    _, audit = stages_module._card_residual_v2_semantic_audit({1: cards[0]})
    residual_payload = {"classifier": None, **audit}
    del residual_payload[missing_field]
    context = _v2_residual_audit_context(cards, residual_payload)

    with pytest.raises(PinnedInputChanged, match="semantic eligibility audit is malformed"):
        stages_module._all_card_classifications(context)


def test_unrecovered_s4a_exclusions_never_enter_selection_fallback_contract() -> None:
    assert stages_module._effective_v2_fallback_note_ids((11,), ()) == ()
    assert stages_module._unrecovered_s4a_exclusion_note_ids((11,), ()) == (11,)
