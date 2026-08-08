import asyncio
from types import SimpleNamespace
from typing import cast

from oms_hub.anki.card_centric import build_source_index
from oms_hub.anki.card_centric_contracts import CardConcept, CardConceptLedger
from oms_hub.anki.domain import CurationJob, CurationStage, SourceKind
from oms_hub.anki.sources import SourcePassage
from oms_hub.anki.stages import CurationServicesRunner
from oms_hub.llm.domain import ProviderName
from tests.anki.fixtures.card_centric_v2_lifecycle import (
    CardCentricV2LifecycleHarness,
    DeterministicEmbeddingClient,
    DeterministicStructuredGenerator,
    LifecycleProviderScript,
)


def test_real_handler_lifecycle_harness_exposes_stage_artifacts() -> None:
    async def scenario() -> None:
        passage = SourcePassage.create(
            revision_id=7,
            lecture_id=12,
            artifact_id="slides-7",
            source_kind=SourceKind.SLIDE,
            locator="slide:1",
            text="Heme synthesis starts with glycine and succinyl-CoA in mitochondria.",
            slide_number=1,
        )
        source = build_source_index(
            (passage,),
            snapshot_id="snapshot-1",
            source_revision_hashes={7: "a" * 64},
        )
        ledger = CardConceptLedger(
            concepts=(
                CardConcept(
                    concept_id="C01",
                    canonical_statement="Heme synthesis starts in mitochondria.",
                    primary_entity="Heme synthesis",
                    aliases=("heme",),
                    depth="deep",
                    emphasis_flag=True,
                    importance="high",
                ),
            ),
            lecture_entity_count=1,
        )
        script = LifecycleProviderScript(
            ledger={"concepts": []},
            fast_batches=({"results": []},),
            thorough_batches=({"results": []}, {"results": []}),
            gap_batches=({"results": []},),
            embeddings={"generated card": (1.0, 0.0, 0.0)},
            selection_payload={"selected_note_ids": []},
            reconciliation_payload={"can_render_envelope": False},
        )
        structured = DeterministicStructuredGenerator(script.structured_responses())
        embedder = DeterministicEmbeddingClient(script.embeddings)
        assert structured.calls == []
        assert embedder.calls == []
        generated = structured.generate_text(
            "fixture instruction",
            "fixture input",
            output_schema={},
            provider=ProviderName.ANTHROPIC,
            model="fixture-model",
        )
        assert generated.request_id == "fixture-001"

        runner = CurationServicesRunner.__new__(CurationServicesRunner)
        harness = CardCentricV2LifecycleHarness(runner)
        product = await harness.invoke(
            job=cast(CurationJob, SimpleNamespace()),
            stage=CurationStage.CARD_EVIDENCE_AUDIT,
            prior_payloads={
                CurationStage.SOURCE_INDEX: {
                    "source_index": source.model_dump(mode="json"),
                },
                CurationStage.CARD_LEDGER: {
                    "ledger": ledger.model_dump(mode="json"),
                },
            },
        )

        assert product.kind == "card_centric_evidence_audit"
        assert product.payload["matched_slide_passage_ids"] == {
            "C01": [source.passages[0].passage_id]
        }
        assert harness.exposed_payloads() == {
            CurationStage.CARD_EVIDENCE_AUDIT: product.payload
        }

    asyncio.run(scenario())
