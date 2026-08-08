import asyncio
from types import SimpleNamespace

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
from oms_hub.anki.semantic.domain import (
    DocumentRecord,
    InputType,
    PinnedCentroidSimilarityResult,
    SemanticHit,
)
from oms_hub.anki.semantic.service import SemanticIndexService, content_hash
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


def _semantic_service(tmp_path, embedder: FakeEmbeddingClient) -> SemanticIndexService:
    return SemanticIndexService(
        SemanticSnapshotStore(tmp_path / "semantic"),
        embedder,
        model="fixture",
        dimensions=3,
        min_coverage=0.0,
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


def test_generation_aware_search_rejects_a_replacement_snapshot(tmp_path) -> None:
    async def scenario() -> None:
        embedder = FakeEmbeddingClient(
            {"stored": [1.0, 0.0, 0.0], "query": [1.0, 0.0, 0.0]}
        )
        service = _semantic_service(tmp_path, embedder)
        generation = await service.refresh([_record(1, "stored")])

        with pytest.raises(ValueError, match="generation is no longer active"):
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
        job=SimpleNamespace(semantic_generation="generation"),
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


def test_residual_audits_borderline_band_and_binds_semantic_generation(monkeypatch) -> None:
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
        scoped_note_ids=(1,),
        unscoped_note_ids=(),
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
            return [[SemanticHit(note_id=1, score=0.45, content_hash=card.content_sha256)]]

    async def fake_classify(_self, cards, **_kwargs):
        assert cards == ()
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

    assert product.payload["audits"] == [
        {
            "concept_id": "C01",
            "query": "Primary Primary",
            "hit_note_ids": [1],
            "semantic_scores": {"1": 0.45},
            "below_classification_threshold_note_ids": [1],
            "classified_note_ids": [],
            "semantic_skip": False,
        }
    ]


def test_unrecovered_s4a_exclusions_never_enter_selection_fallback_contract() -> None:
    assert stages_module._effective_v2_fallback_note_ids((11,), ()) == ()
    assert stages_module._unrecovered_s4a_exclusion_note_ids((11,), ()) == (11,)
