import asyncio
import hashlib
from types import SimpleNamespace
from typing import cast

from oms_hub.anki.card_centric import build_source_index
from oms_hub.anki.card_centric_contracts import (
    CardConcept,
    CardConceptLedger,
    ClassifierResult,
    ClassifierTelemetry,
    FastClassificationResult,
    GeneratedCardResolution,
)
from oms_hub.anki.domain import (
    CurationJob,
    CurationStage,
    PipelineContractVersion,
    ResolvedModelConfiguration,
    SourceKind,
)
from oms_hub.anki.prompts import AnkiPromptLibrary
from oms_hub.anki.sources import SourcePassage
from oms_hub.anki.stages import CurationServicesRunner
from oms_hub.llm.structured import StructuredTextService
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
        summary = SourcePassage.create(
            revision_id=8,
            lecture_id=12,
            artifact_id="summary-8",
            source_kind=SourceKind.SUMMARY,
            locator="summary:outline",
            text="Heme synthesis begins in mitochondria.",
        )
        source = build_source_index(
            (summary, passage),
            snapshot_id="snapshot-1",
            source_revision_hashes={7: "a" * 64, 8: "b" * 64},
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
            ledger=ledger.model_dump(mode="json"),
            fast_batches=({"results": []},),
            thorough_batches=({"results": []}, {"results": []}),
            gap_batches=({"results": []},),
            embeddings={
                "first fact alpha": (1.0, 0.0, 0.0),
                "second fact beta": (0.0, 1.0, 0.0),
            },
            selection_payload={"selected_note_ids": []},
            reconciliation_payload={"can_render_envelope": False},
        )
        structured = DeterministicStructuredGenerator(script.structured_responses())
        embedder = DeterministicEmbeddingClient(script.embeddings)
        assert structured.calls == []
        assert embedder.calls == []

        runner = CurationServicesRunner.__new__(CurationServicesRunner)
        runner.structured = StructuredTextService(structured)
        runner.embedder = embedder
        harness = CardCentricV2LifecycleHarness(runner)
        prompt = AnkiPromptLibrary().load("card-centric-ledger-v2")
        job = cast(
            CurationJob,
            SimpleNamespace(
                pipeline_contract_version=PipelineContractVersion.CARD_CENTRIC_V2,
                resolved_model_config=ResolvedModelConfiguration.card_centric_v2_default(
                    "anthropic",
                    "fixture-model",
                ),
                gap_prompt_version="gap-v2",
            ),
        )
        preflight = {
            "prompt_snapshot": [
                {
                    "id": prompt.metadata.id,
                    "version": prompt.metadata.version,
                    "prompt_hash": hashlib.sha256(prompt.content.encode()).hexdigest()[:12],
                    "content": prompt.content,
                    "path": str(prompt.path),
                    "source_paths": [str(path) for path in prompt.source_paths],
                    "metadata": prompt.metadata.model_dump(mode="json", by_alias=True),
                }
            ]
        }
        ledger_product = await harness.invoke(
            job=job,
            stage=CurationStage.CARD_LEDGER,
            prior_payloads={
                CurationStage.PREFLIGHT: preflight,
                CurationStage.SOURCE_INDEX: {
                    "source_index": source.model_dump(mode="json"),
                },
            },
        )
        assert ledger_product.kind == "card_centric_ledger"
        assert len(structured.calls) == 1
        assert structured.calls[0][2].value == "anthropic"
        assert structured.calls[0][3] == "fixture-model"

        product = await harness.invoke(
            job=job,
            stage=CurationStage.CARD_EVIDENCE_AUDIT,
            prior_payloads={
                CurationStage.SOURCE_INDEX: {
                    "source_index": source.model_dump(mode="json"),
                },
                CurationStage.CARD_LEDGER: {
                    "ledger": ledger_product.payload["ledger"],
                },
            },
        )

        assert product.kind == "card_centric_evidence_audit"
        slide_passage_id = next(
            item.passage_id for item in source.passages if item.authority == "slide"
        )
        assert product.payload["matched_slide_passage_ids"] == {"C01": [slide_passage_id]}
        generated = (
            GeneratedCardResolution(
                card_id="G01",
                concept_id="C01",
                fact_id="C01-M1",
                text="First fact {{c1::alpha}}.",
                source_passage_ids=(slide_passage_id,),
                evidence_ids=("E01",),
            ),
            GeneratedCardResolution(
                card_id="G02",
                concept_id="C01",
                fact_id="C01-M2",
                text="Second fact {{c1::beta}}.",
                source_passage_ids=(slide_passage_id,),
                evidence_ids=("E02",),
            ),
        )
        dedupe_product = await harness.invoke(
            job=job,
            stage=CurationStage.DEDUPE,
            prior_payloads={
                CurationStage.SOURCE_INDEX: {
                    "source_index": source.model_dump(mode="json"),
                    "cards": [],
                },
                CurationStage.CARD_CLASSIFY: {
                    "classifier": ClassifierResult(
                        results=(),
                        telemetry=ClassifierTelemetry(
                            batch_count=0,
                            cache_prefix_sha256="c" * 64,
                            cache_mode="ordinary_prefix",
                            provider="anthropic",
                            model="fixture-model",
                            request_ids=(),
                            batches=(),
                        ),
                    ).model_dump(mode="json"),
                },
                CurationStage.CARD_RESIDUAL: {"classifier": None},
                CurationStage.CARD_FAST_CLASSIFY: {
                    "fast_classifier": FastClassificationResult(results=()).model_dump(mode="json"),
                    "fallback_note_ids": [],
                },
                CurationStage.CARD_GAP_FILL: {
                    "resolutions": [item.model_dump(mode="json") for item in generated]
                },
            },
        )
        assert dedupe_product.kind == "card_centric_dedupe"
        assert [item["status"] for item in dedupe_product.payload["resolutions"]] == [
            "generated",
            "generated",
        ]
        assert embedder.calls == [("document", ("second fact beta", "first fact alpha"))]
        assert harness.exposed_payloads() == {
            CurationStage.CARD_LEDGER: ledger_product.payload,
            CurationStage.CARD_EVIDENCE_AUDIT: product.payload,
            CurationStage.DEDUPE: dedupe_product.payload,
        }

    asyncio.run(scenario())
