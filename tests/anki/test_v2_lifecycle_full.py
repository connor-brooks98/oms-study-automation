"""P4-A executable real-handler lifecycle assertions for card_centric_v2."""

from __future__ import annotations

import asyncio

import numpy as np

from oms_hub.anki.card_centric_contracts import (
    CardClassification,
    ClassifierResult,
    ClassifierTelemetry,
    FastCardClassification,
    FastClassificationResult,
    GeneratedCardResolution,
)
from oms_hub.anki.domain import CurationStage
from oms_hub.anki.prompt_catalog import AnkiPromptCatalogService
from oms_hub.anki.stages import CurationServicesRunner
from oms_hub.llm.structured import StructuredTextService
from tests.anki.fixtures.card_centric_v2_lifecycle import (
    CardCentricV2LifecycleHarness,
    DeterministicEmbeddingClient,
    DeterministicStructuredGenerator,
)
from tests.anki.fixtures.card_centric_v2_lifecycle_data import (
    LifecycleRepository,
    LifecycleSemanticService,
    lifecycle_job,
    lifecycle_ledger,
    lifecycle_preflight,
    lifecycle_source_payload,
    payloads,
)


class _UniqueEmbeddings:
    async def embed(self, texts: list[str], *, input_type: str) -> np.ndarray:
        del input_type
        # The proposed card is orthogonal to each comparison in every S8 call.
        return np.eye(len(texts), dtype=np.float32)


class _NoResidualHits:
    """S4a retains every scoped card; S6 executes but finds no recall candidates."""

    async def pinned_similarity(
        self, queries: tuple[str, ...], *, note_ids: tuple[int, ...], expected_generation: str
    ) -> dict[int, float]:
        del queries
        assert expected_generation == "fixture-generation"
        return {note_id: 0.90 for note_id in note_ids}

    async def search(
        self,
        queries: tuple[str, ...],
        *,
        eligible_note_ids: set[int],
        limit: int,
        expected_generation: str = "fixture-generation",
    ) -> list[list[object]]:
        del eligible_note_ids, limit
        assert expected_generation == "fixture-generation"
        return [[] for _ in queries]


def _runner(
    responses: list[dict[str, object]], *, embedder: object | None = None
) -> tuple[CardCentricV2LifecycleHarness, object]:
    runner = CurationServicesRunner.__new__(CurationServicesRunner)
    generator = DeterministicStructuredGenerator(responses)
    runner.structured = StructuredTextService(generator)
    runner.embedder = embedder or DeterministicEmbeddingClient({})
    runner.semantic = LifecycleSemanticService()
    runner.repository = LifecycleRepository()
    runner.prompts = AnkiPromptCatalogService()
    return CardCentricV2LifecycleHarness(runner), generator


def test_continuous_real_handler_lifecycle_reaches_review_with_envelope_eligibility() -> None:
    """Every v2 handler receives the preceding production product payload unchanged."""

    async def scenario() -> None:
        source = lifecycle_source_payload()
        ledger = lifecycle_ledger()
        slide_id = next(
            passage["passage_id"]
            for passage in source["source_index"]["passages"]
            if passage["authority"] == "slide"
        )
        fast = {
            "results": [
                {
                    "note_id": 1,
                    "verdict": "LIKELY_YES",
                    "grounded_concept_ids": ["C01"],
                    "supporting_passage_ids": [slide_id],
                    "reason": "Grounded fast high coverage.",
                },
                *[
                    {
                        "note_id": note_id,
                        "verdict": "NEEDS_REVIEW",
                        "reason": "Thorough routing.",
                    }
                    for note_id in range(2, 11)
                ],
            ]
        }
        thorough = {
            "results": [
                {
                    "note_id": note_id,
                    "verdict": "YES",
                    "primary_subject": "heme synthesis",
                    "reason": "Grounded existing high coverage.",
                    "covered_concept_ids": ["C01"],
                    "supporting_passage_ids": [slide_id],
                    "flags": [],
                }
                for note_id in range(2, 11)
            ]
        }
        gap = {
            "resolutions": [
                {
                    "fact_id": "C02-M1",
                    "status": "generated",
                    "text": "ALA synthase uses {{c1::glycine}}.",
                    "extra": "First split.",
                    "note_type": "Cloze",
                    "source_passage_ids": [slide_id],
                    "split": True,
                },
                {
                    "fact_id": "C02-M1",
                    "status": "generated",
                    "text": "ALA synthase also uses {{c1::succinyl-CoA}}.",
                    "extra": "Second split.",
                    "note_type": "Cloze",
                    "source_passage_ids": [slide_id],
                    "split": True,
                },
                {
                    "fact_id": "C02-M2",
                    "status": "generated",
                    "text": "This step occurs in {{c1::mitochondria}}.",
                    "extra": "Location.",
                    "note_type": "Cloze",
                    "source_passage_ids": [slide_id],
                },
                {
                    "fact_id": "C02-M3",
                    "status": "generated",
                    "text": "The substrate pair includes {{c1::glycine}}.",
                    "extra": "Substrates.",
                    "note_type": "Cloze",
                    "source_passage_ids": [slide_id],
                },
            ]
        }
        harness, _ = _runner(
            [ledger.model_dump(mode="json"), fast, thorough, gap], embedder=_UniqueEmbeddings()
        )
        harness.runner.semantic = _NoResidualHits()
        job = lifecycle_job()
        prior = payloads(source=source, preflight=lifecycle_preflight())

        stage_order = (
            CurationStage.CARD_LEDGER,
            CurationStage.CARD_EVIDENCE_AUDIT,
            CurationStage.CARD_TAG_SCOPE,
            CurationStage.CARD_PREFILTER,
            CurationStage.CARD_FAST_CLASSIFY,
            CurationStage.CARD_CLASSIFY,
            CurationStage.CARD_COVERAGE,
            CurationStage.CARD_RESIDUAL,
            CurationStage.CARD_GAP_FILL,
            CurationStage.DEDUPE,
            CurationStage.CARD_SELECTION,
            CurationStage.RECONCILIATION,
        )
        products = {}
        for stage in stage_order:
            product = await harness.invoke(job=job, stage=stage, prior_payloads=prior)
            products[stage] = product
            prior[stage] = product.payload

        reconciliation = products[CurationStage.RECONCILIATION]
        assert products[CurationStage.CARD_GAP_FILL].kind == "card_centric_gap_fill"
        assert (
            products[CurationStage.DEDUPE].payload["resolutions"]
            == products[CurationStage.CARD_GAP_FILL].payload["resolutions"]
        )
        assert products[CurationStage.CARD_SELECTION].payload["selected_generated_card_ids"]
        assert reconciliation.kind == "card_centric_reconciliation"
        assert reconciliation.blocking_error is None
        assert reconciliation.payload["failed"] == []
        assert reconciliation.payload["can_render_envelope"] is True

    asyncio.run(scenario())


def test_s6_mid_band_has_explicit_disposition_expected_red_p2_m8() -> None:
    """P2 M-8: [0.40, 0.50) hits must be visibly dispositioned, not silently dropped."""

    async def scenario() -> None:
        ledger = lifecycle_ledger()
        source = lifecycle_source_payload()
        slide_id = next(
            passage["passage_id"]
            for passage in source["source_index"]["passages"]
            if passage["authority"] == "slide"
        )
        # S4b routes one grounded fast positive and nine thorough decisions.
        fast = {
            "results": [
                {
                    "note_id": 1,
                    "verdict": "LIKELY_YES",
                    "grounded_concept_ids": ["C01"],
                    "supporting_passage_ids": [slide_id],
                    "reason": "Fast grounded coverage.",
                },
                *[
                    {
                        "note_id": note_id,
                        "verdict": "NEEDS_REVIEW",
                        "grounded_concept_ids": [],
                        "supporting_passage_ids": [],
                        "reason": "Needs thorough classification.",
                    }
                    for note_id in range(3, 11)
                ],
            ]
        }
        thorough = {
            "results": [
                {
                    "note_id": note_id,
                    "verdict": "NO",
                    "primary_subject": "unrelated fixture card",
                    "reason": "No grounded heme coverage.",
                    "covered_concept_ids": [],
                    "supporting_passage_ids": [],
                    "flags": [],
                }
                for note_id in range(3, 11)
            ]
        }
        harness, generator = _runner([ledger.model_dump(mode="json"), fast, thorough])
        job = lifecycle_job()
        prior = payloads(source=source, preflight=lifecycle_preflight())
        ledger_product = await harness.invoke(
            job=job, stage=CurationStage.CARD_LEDGER, prior_payloads=prior
        )
        prior[CurationStage.CARD_LEDGER] = ledger_product.payload
        evidence = await harness.invoke(
            job=job, stage=CurationStage.CARD_EVIDENCE_AUDIT, prior_payloads=prior
        )
        scope = await harness.invoke(
            job=job, stage=CurationStage.CARD_TAG_SCOPE, prior_payloads=prior
        )
        prior[CurationStage.CARD_TAG_SCOPE] = scope.payload
        prefilter = await harness.invoke(
            job=job, stage=CurationStage.CARD_PREFILTER, prior_payloads=prior
        )
        prior[CurationStage.CARD_PREFILTER] = prefilter.payload
        fast_product = await harness.invoke(
            job=job, stage=CurationStage.CARD_FAST_CLASSIFY, prior_payloads=prior
        )
        prior[CurationStage.CARD_FAST_CLASSIFY] = fast_product.payload
        classified = await harness.invoke(
            job=job, stage=CurationStage.CARD_CLASSIFY, prior_payloads=prior
        )
        prior[CurationStage.CARD_CLASSIFY] = classified.payload
        coverage = await harness.invoke(
            job=job, stage=CurationStage.CARD_COVERAGE, prior_payloads=prior
        )
        prior[CurationStage.CARD_COVERAGE] = coverage.payload
        residual = await harness.invoke(
            job=job, stage=CurationStage.CARD_RESIDUAL, prior_payloads=prior
        )

        assert evidence.payload["matched_slide_passage_ids"]["C01"]
        assert evidence.payload["matched_slide_char_counts"]["C02"] > 0
        assert set(prefilter.payload["pre_filtered_note_ids"]) | set(
            prefilter.payload["pre_excluded_note_ids"]
        ) == set(range(1, 11))
        assert fast_product.payload["degraded_batches"] == []
        assert fast_product.payload["fast_classifier"]["results"][0]["verdict"] == "LIKELY_YES"
        assert classified.payload["thorough_count"] == 8
        assert coverage.payload["coverage"]["C01"]["status"] == "covered"
        assert coverage.payload["coverage"]["C02"]["status"] == "uncovered"
        assert residual.payload["audits"][0]["hit_note_ids"] == [2]
        assert residual.payload["audits"][0]["disposition"] == "below_classification_threshold"
        assert generator.calls[0][2].value == "anthropic"

    asyncio.run(scenario())


def test_real_handlers_s7_split_generation_and_s8_unique_resolutions() -> None:
    async def scenario() -> None:
        ledger = lifecycle_ledger()
        source = lifecycle_source_payload()
        slide_id = next(
            passage["passage_id"]
            for passage in source["source_index"]["passages"]
            if passage["authority"] == "slide"
        )
        gap = {
            "resolutions": [
                {
                    "fact_id": "C02-M1",
                    "status": "generated",
                    "text": "ALA synthase uses {{c1::glycine}}.",
                    "extra": "First split.",
                    "note_type": "Cloze",
                    "source_passage_ids": [slide_id],
                    "split": True,
                },
                {
                    "fact_id": "C02-M1",
                    "status": "generated",
                    "text": "ALA synthase also uses {{c1::succinyl-CoA}}.",
                    "extra": "Second split.",
                    "note_type": "Cloze",
                    "source_passage_ids": [slide_id],
                    "split": True,
                },
                {
                    "fact_id": "C02-M2",
                    "status": "generated",
                    "text": "This step occurs in {{c1::mitochondria}}.",
                    "extra": "Location.",
                    "note_type": "Cloze",
                    "source_passage_ids": [slide_id],
                },
                {
                    "fact_id": "C02-M3",
                    "status": "generated",
                    "text": "The substrate pair includes {{c1::glycine}}.",
                    "extra": "Substrates.",
                    "note_type": "Cloze",
                    "source_passage_ids": [slide_id],
                },
            ]
        }
        harness, _ = _runner([gap], embedder=_UniqueEmbeddings())
        job = lifecycle_job()
        prior = payloads(source=source, preflight=lifecycle_preflight())
        prior[CurationStage.CARD_LEDGER] = {"ledger": ledger.model_dump(mode="json")}
        prior[CurationStage.CARD_COVERAGE] = {
            "coverage": {
                "C01": {"status": "covered", "evidence": []},
                "C02": {"status": "uncovered", "evidence": []},
            }
        }
        empty_classifier = ClassifierResult(
            results=(),
            telemetry=ClassifierTelemetry(
                batch_count=0,
                cache_prefix_sha256="a" * 64,
                cache_mode="ordinary_prefix",
                provider="anthropic",
                model="fixture-model",
                request_ids=(),
                batches=(),
            ),
        )
        prior[CurationStage.CARD_CLASSIFY] = {
            "classifier": empty_classifier.model_dump(mode="json")
        }
        prior[CurationStage.CARD_RESIDUAL] = {"classifier": None}
        prior[CurationStage.CARD_FAST_CLASSIFY] = {
            "fast_classifier": FastClassificationResult(results=()).model_dump(mode="json"),
            "fallback_note_ids": [],
        }
        generated = await harness.invoke(
            job=job, stage=CurationStage.CARD_GAP_FILL, prior_payloads=prior
        )
        prior[CurationStage.CARD_GAP_FILL] = generated.payload
        dedupe = await harness.invoke(job=job, stage=CurationStage.DEDUPE, prior_payloads=prior)

        resolutions = generated.payload["resolutions"]
        assert {row["fact_id"] for row in resolutions} == {"C02-M1", "C02-M2", "C02-M3"}
        assert sum(row["fact_id"] == "C02-M1" for row in resolutions) == 2
        assert all(row["split"] for row in resolutions if row["fact_id"] == "C02-M1")
        assert {row["status"] for row in dedupe.payload["resolutions"]} == {"generated"}

    asyncio.run(scenario())


def test_real_handler_s9_preserves_selected_fact_artifacts_and_review_eligibility() -> None:
    async def scenario() -> None:
        source = lifecycle_source_payload()
        slide_id = next(
            passage["passage_id"]
            for passage in source["source_index"]["passages"]
            if passage["authority"] == "slide"
        )
        harness, _ = _runner([])
        job = lifecycle_job()
        prior = payloads(source=source, preflight=lifecycle_preflight())
        scope = await harness.invoke(
            job=job, stage=CurationStage.CARD_TAG_SCOPE, prior_payloads=prior
        )
        prior[CurationStage.CARD_TAG_SCOPE] = scope.payload
        prior[CurationStage.CARD_LEDGER] = {"ledger": lifecycle_ledger().model_dump(mode="json")}
        classifier = ClassifierResult(
            results=tuple(
                CardClassification(
                    note_id=note_id,
                    verdict="YES",
                    primary_subject="heme synthesis",
                    reason="Grounded lifecycle fixture coverage.",
                    covered_concept_ids=("C01",),
                    supporting_passage_ids=(slide_id,),
                )
                for note_id in range(1, 11)
            ),
            telemetry=ClassifierTelemetry(
                batch_count=0,
                cache_prefix_sha256="a" * 64,
                cache_mode="ordinary_prefix",
                provider="anthropic",
                model="fixture-model",
                request_ids=(),
                batches=(),
            ),
        )
        generated = tuple(
            GeneratedCardResolution(
                card_id=f"G{index}",
                concept_id="C02",
                fact_id=fact_id,
                text=f"Generated fact {index} is {{{{c1::grounded}}}}.",
                source_passage_ids=(slide_id,),
                evidence_ids=(f"E{index}",),
                split=fact_id == "C02-M1",
            )
            for index, fact_id in enumerate(("C02-M1", "C02-M1", "C02-M2", "C02-M3"), 1)
        )
        prior[CurationStage.CARD_CLASSIFY] = {"classifier": classifier.model_dump(mode="json")}
        prior[CurationStage.CARD_FAST_CLASSIFY] = {
            "fast_classifier": FastClassificationResult(results=()).model_dump(mode="json"),
            "fallback_note_ids": [],
        }
        prior[CurationStage.CARD_COVERAGE] = {
            "coverage": {
                "C01": {"status": "covered", "evidence": []},
                "C02": {"status": "uncovered", "evidence": []},
            }
        }
        prior[CurationStage.CARD_RESIDUAL] = {
            "classifier": None,
            "uncovered_concept_ids": ["C02"],
        }
        prior[CurationStage.DEDUPE] = {
            "resolutions": [item.model_dump(mode="json") for item in generated]
        }
        selection = await harness.invoke(
            job=job, stage=CurationStage.CARD_SELECTION, prior_payloads=prior
        )
        prior[CurationStage.CARD_SELECTION] = selection.payload

        report = await harness.invoke(
            job=job, stage=CurationStage.RECONCILIATION, prior_payloads=prior
        )

        assert report.kind == "card_centric_reconciliation"
        assert report.payload["can_render_envelope"] is True
        assert report.payload["failed"] == []
        assert selection.kind == "card_centric_selection"
        assert selection.payload["selected_existing_note_ids"] == list(range(1, 11))
        assert selection.payload["selected_generated_card_ids"] == ["G1", "G2", "G3", "G4"]
        assert selection.payload["minimum_target"] == 60
        assert selection.payload["target"] == 65
        assert selection.payload["cap"] == 70
        snapshot = report.payload["snapshot"]
        assert {item["fact_id"] for item in snapshot["canonical_generated_cards"]} == {
            "C02-M1",
            "C02-M2",
            "C02-M3",
        }
        assert snapshot["selected_generated_card_ids"] == ["G1", "G2", "G3", "G4"]

    asyncio.run(scenario())


def test_real_handler_s8_preserves_duplicate_identity() -> None:
    async def scenario() -> None:
        source = lifecycle_source_payload()
        slide_id = next(
            passage["passage_id"]
            for passage in source["source_index"]["passages"]
            if passage["authority"] == "slide"
        )
        harness, _ = _runner([])
        prior = payloads(source=source, preflight=lifecycle_preflight())
        prior[CurationStage.CARD_LEDGER] = {"ledger": lifecycle_ledger().model_dump(mode="json")}
        classifier = ClassifierResult(
            results=(
                CardClassification(
                    note_id=1,
                    verdict="YES",
                    primary_subject="heme synthesis",
                    reason="Grounded existing coverage.",
                    covered_concept_ids=("C01",),
                    supporting_passage_ids=(slide_id,),
                ),
            ),
            telemetry=ClassifierTelemetry(
                batch_count=0,
                cache_prefix_sha256="a" * 64,
                cache_mode="ordinary_prefix",
                provider="anthropic",
                model="fixture-model",
                request_ids=(),
                batches=(),
            ),
        )
        prior[CurationStage.CARD_CLASSIFY] = {"classifier": classifier.model_dump(mode="json")}
        prior[CurationStage.CARD_RESIDUAL] = {"classifier": None}
        prior[CurationStage.CARD_FAST_CLASSIFY] = {
            "fast_classifier": FastClassificationResult(results=()).model_dump(mode="json"),
            "fallback_note_ids": [],
        }
        prior[CurationStage.CARD_GAP_FILL] = {
            "resolutions": [
                GeneratedCardResolution(
                    card_id="duplicate-row",
                    concept_id="C02",
                    fact_id="C02-M1",
                    text="Heme synthesis begins in {{c1::mitochondria}}.",
                    extra="Fixture card.",
                    source_passage_ids=(slide_id,),
                    evidence_ids=("E-duplicate",),
                ).model_dump(mode="json")
            ]
        }

        product = await harness.invoke(
            job=lifecycle_job(), stage=CurationStage.DEDUPE, prior_payloads=prior
        )

        resolution = product.payload["resolutions"][0]
        assert resolution["status"] == "duplicate_of_existing"
        assert resolution["duplicate_of_existing_note_id"] == 1
        assert resolution["duplicate_of_generated_card_id"] is None

    asyncio.run(scenario())


def test_selection_never_pads_with_unclassified_s4a_fallback_expected_red_p3_h1() -> None:
    """P3 H-1: an unrecovered S4a exclusion cannot be selected merely below 60."""

    async def scenario() -> None:
        harness, _ = _runner([])
        prior = payloads(source=lifecycle_source_payload(), preflight=lifecycle_preflight())
        scope = await harness.invoke(
            job=lifecycle_job(), stage=CurationStage.CARD_TAG_SCOPE, prior_payloads=prior
        )
        prior[CurationStage.CARD_TAG_SCOPE] = scope.payload
        prior[CurationStage.CARD_LEDGER] = {"ledger": lifecycle_ledger().model_dump(mode="json")}
        empty = ClassifierResult(
            results=(
                CardClassification(
                    note_id=note_id,
                    verdict="NO",
                    primary_subject="unrelated fixture card",
                    reason="Not eligible for selection.",
                )
                for note_id in (1, *range(3, 11))
            ),
            telemetry=ClassifierTelemetry(
                batch_count=0,
                cache_prefix_sha256="a" * 64,
                cache_mode="ordinary_prefix",
                provider="anthropic",
                model="fixture-model",
                request_ids=(),
                batches=(),
            ),
        )
        prior[CurationStage.CARD_CLASSIFY] = {"classifier": empty.model_dump(mode="json")}
        prior[CurationStage.CARD_RESIDUAL] = {"classifier": None}
        prior[CurationStage.CARD_FAST_CLASSIFY] = {
            "fast_classifier": FastClassificationResult(results=()).model_dump(mode="json"),
            "fallback_note_ids": [2],
        }
        prior[CurationStage.DEDUPE] = {"resolutions": []}

        selection = await harness.invoke(
            job=lifecycle_job(), stage=CurationStage.CARD_SELECTION, prior_payloads=prior
        )

        assert 2 not in selection.payload["selected_existing_note_ids"]

    asyncio.run(scenario())


def test_selection_does_not_promote_fast_yes_after_floor_expected_red_p3_m10() -> None:
    """P3 M-10: fast positives are T6-only and cannot displace 60 grounded YES cards."""

    async def scenario() -> None:
        source = lifecycle_source_payload(card_count=61)
        slide_id = next(
            passage["passage_id"]
            for passage in source["source_index"]["passages"]
            if passage["authority"] == "slide"
        )
        harness, _ = _runner([])
        prior = payloads(source=source, preflight=lifecycle_preflight())
        scope = await harness.invoke(
            job=lifecycle_job(), stage=CurationStage.CARD_TAG_SCOPE, prior_payloads=prior
        )
        prior[CurationStage.CARD_TAG_SCOPE] = scope.payload
        prior[CurationStage.CARD_LEDGER] = {"ledger": lifecycle_ledger().model_dump(mode="json")}
        classifier = ClassifierResult(
            results=tuple(
                CardClassification(
                    note_id=note_id,
                    verdict="YES",
                    primary_subject="ALA synthase",
                    reason="Grounded medium-priority coverage.",
                    covered_concept_ids=("C02",),
                    supporting_passage_ids=(slide_id,),
                )
                for note_id in range(2, 62)
            ),
            telemetry=ClassifierTelemetry(
                batch_count=0,
                cache_prefix_sha256="a" * 64,
                cache_mode="ordinary_prefix",
                provider="anthropic",
                model="fixture-model",
                request_ids=(),
                batches=(),
            ),
        )
        prior[CurationStage.CARD_CLASSIFY] = {"classifier": classifier.model_dump(mode="json")}
        prior[CurationStage.CARD_RESIDUAL] = {"classifier": None}
        prior[CurationStage.CARD_FAST_CLASSIFY] = {
            "fast_classifier": FastClassificationResult(
                results=(
                    FastCardClassification(
                        note_id=1,
                        verdict="LIKELY_YES",
                        grounded_concept_ids=("C01",),
                        supporting_passage_ids=(slide_id,),
                        reason="Fast high-priority coverage.",
                    ),
                )
            ).model_dump(mode="json"),
            "fallback_note_ids": [],
        }
        prior[CurationStage.DEDUPE] = {"resolutions": []}

        selection = await harness.invoke(
            job=lifecycle_job(), stage=CurationStage.CARD_SELECTION, prior_payloads=prior
        )

        assert 1 not in selection.payload["selected_existing_note_ids"]

    asyncio.run(scenario())


def test_selection_orders_t1_t2_t3_positions_expected_red_p3_m11() -> None:
    """P3 M-11: Selection must expose the approved T1 -> T2 -> T3 order."""

    async def scenario() -> None:
        source = lifecycle_source_payload()
        slide_id = next(
            passage["passage_id"]
            for passage in source["source_index"]["passages"]
            if passage["authority"] == "slide"
        )
        harness, _ = _runner([])
        prior = payloads(source=source, preflight=lifecycle_preflight())
        scope = await harness.invoke(
            job=lifecycle_job(), stage=CurationStage.CARD_TAG_SCOPE, prior_payloads=prior
        )
        prior[CurationStage.CARD_TAG_SCOPE] = scope.payload
        prior[CurationStage.CARD_LEDGER] = {"ledger": lifecycle_ledger().model_dump(mode="json")}
        empty = ClassifierResult(
            results=(
                CardClassification(
                    note_id=1,
                    verdict="YES",
                    primary_subject="heme synthesis",
                    reason="Grounded existing high-priority coverage.",
                    covered_concept_ids=("C01",),
                    supporting_passage_ids=(slide_id,),
                ),
                *(
                    CardClassification(
                        note_id=note_id,
                        verdict="NO",
                        primary_subject="unrelated fixture card",
                        reason="Not eligible for selection.",
                    )
                    for note_id in range(2, 11)
                ),
            ),
            telemetry=ClassifierTelemetry(
                batch_count=0,
                cache_prefix_sha256="a" * 64,
                cache_mode="ordinary_prefix",
                provider="anthropic",
                model="fixture-model",
                request_ids=(),
                batches=(),
            ),
        )
        prior[CurationStage.CARD_CLASSIFY] = {"classifier": empty.model_dump(mode="json")}
        prior[CurationStage.CARD_RESIDUAL] = {"classifier": None}
        prior[CurationStage.CARD_FAST_CLASSIFY] = {
            "fast_classifier": FastClassificationResult(results=()).model_dump(mode="json"),
            "fallback_note_ids": [],
        }
        prior[CurationStage.DEDUPE] = {
            "resolutions": [
                GeneratedCardResolution(
                    card_id="G-T1",
                    concept_id="C01",
                    fact_id="C01-M1",
                    text="Generated high fact {{c1::one}}.",
                    source_passage_ids=(slide_id,),
                    evidence_ids=("E-T1",),
                ).model_dump(mode="json"),
                GeneratedCardResolution(
                    card_id="G-T2",
                    concept_id="C02",
                    fact_id="C02-M1",
                    text="Generated medium fact {{c1::two}}.",
                    source_passage_ids=(slide_id,),
                    evidence_ids=("E-T2",),
                ).model_dump(mode="json"),
            ]
        }

        selection = await harness.invoke(
            job=lifecycle_job(), stage=CurationStage.CARD_SELECTION, prior_payloads=prior
        )

        expected = [
            {"identity": "card:G-T1", "selected_position": 1, "tier": "T1"},
            {"identity": "card:G-T2", "selected_position": 2, "tier": "T2"},
            {"identity": "note:1", "selected_position": 3, "tier": "T3"},
        ]
        raw_metadata = selection.payload.get("selection_metadata", [])
        actual = [
            {
                "identity": item.get("identity"),
                "selected_position": item.get("selected_position"),
                "tier": item.get("tier"),
            }
            for item in raw_metadata
            if isinstance(item, dict)
        ]
        assert actual == expected, (
            "S0 raw Selection output keeps generated="
            f"{selection.payload['selected_generated_card_ids']} and existing="
            f"{selection.payload['selected_existing_note_ids']}; it does not expose the "
            "required cross-kind positions, and its current selection implementation orders "
            "the competing identities T1 -> T3 -> T2."
        )

    asyncio.run(scenario())


def test_selection_keeps_only_best_redundant_coverage_expected_red_p3_h5() -> None:
    """P3 H-5: a soft target cannot retain duplicate coverage as padding."""

    async def scenario() -> None:
        source = lifecycle_source_payload()
        slide_id = next(
            passage["passage_id"]
            for passage in source["source_index"]["passages"]
            if passage["authority"] == "slide"
        )
        harness, _ = _runner([])
        prior = payloads(source=source, preflight=lifecycle_preflight())
        scope = await harness.invoke(
            job=lifecycle_job(), stage=CurationStage.CARD_TAG_SCOPE, prior_payloads=prior
        )
        prior[CurationStage.CARD_TAG_SCOPE] = scope.payload
        prior[CurationStage.CARD_LEDGER] = {"ledger": lifecycle_ledger().model_dump(mode="json")}
        empty = ClassifierResult(
            results=tuple(
                CardClassification(
                    note_id=note_id,
                    verdict="YES",
                    primary_subject="heme synthesis",
                    reason="Equivalent grounded coverage.",
                    covered_concept_ids=("C01",),
                    supporting_passage_ids=(slide_id,),
                )
                for note_id in range(1, 11)
            ),
            telemetry=ClassifierTelemetry(
                batch_count=0,
                cache_prefix_sha256="a" * 64,
                cache_mode="ordinary_prefix",
                provider="anthropic",
                model="fixture-model",
                request_ids=(),
                batches=(),
            ),
        )
        prior[CurationStage.CARD_CLASSIFY] = {"classifier": empty.model_dump(mode="json")}
        prior[CurationStage.CARD_RESIDUAL] = {"classifier": None}
        prior[CurationStage.CARD_FAST_CLASSIFY] = {
            "fast_classifier": FastClassificationResult(results=()).model_dump(mode="json"),
            "fallback_note_ids": [],
        }
        prior[CurationStage.DEDUPE] = {"resolutions": []}

        selection = await harness.invoke(
            job=lifecycle_job(), stage=CurationStage.CARD_SELECTION, prior_payloads=prior
        )

        assert selection.payload["selected_existing_note_ids"] == [1]

    asyncio.run(scenario())


def test_real_handler_s4b_invalid_optional_batch_degrades_every_note() -> None:
    async def scenario() -> None:
        ledger = lifecycle_ledger()
        invalid = {
            "results": [
                {"note_id": note_id, "verdict": "LIKELY_YES", "reason": "Ungrounded."}
                for note_id in (1, *range(3, 11))
            ]
        }
        harness, _ = _runner([ledger.model_dump(mode="json"), invalid])
        job = lifecycle_job()
        prior = payloads(source=lifecycle_source_payload(), preflight=lifecycle_preflight())
        product = await harness.invoke(
            job=job, stage=CurationStage.CARD_LEDGER, prior_payloads=prior
        )
        prior[CurationStage.CARD_LEDGER] = product.payload
        scope = await harness.invoke(
            job=job, stage=CurationStage.CARD_TAG_SCOPE, prior_payloads=prior
        )
        prior[CurationStage.CARD_TAG_SCOPE] = scope.payload
        prefilter = await harness.invoke(
            job=job, stage=CurationStage.CARD_PREFILTER, prior_payloads=prior
        )
        prior[CurationStage.CARD_PREFILTER] = prefilter.payload
        fast = await harness.invoke(
            job=job, stage=CurationStage.CARD_FAST_CLASSIFY, prior_payloads=prior
        )

        assert fast.payload["degraded_note_count"] == 9
        assert fast.payload["degraded_batches"][0]["reason_code"] == "ungrounded_likely_yes"
        assert {row["verdict"] for row in fast.payload["fast_classifier"]["results"]} == {
            "NEEDS_REVIEW"
        }

    asyncio.run(scenario())


def test_s9_below_warning_floor_is_executable_expected_red_p3_h4() -> None:
    """P3 H-4: a 10--59 selection needs an observable below-60 warning."""
    from oms_hub.anki.reconciliation import (
        AuditResolution,
        CardCentricReconciliationInput,
        reconcile_card_centric,
    )

    snapshot = CardCentricReconciliationInput(
        pipeline_contract_version="card_centric_v2",
        concept_ids=("C01",),
        coverage={"C01": "covered"},
        required_fact_ids=(),
        uncovered_after_s5=(),
        residual_ran_for=(),
        generated_cards=(),
        unresolved_fact_ids=(),
        expected_scoped_nids=tuple(range(1, 11)),
        classifications=tuple(AuditResolution(nid=nid, verdict="keep") for nid in range(1, 11)),
        eligible_yes_nids=tuple(range(1, 11)),
        selected_nids=tuple(range(1, 11)),
        selected_generated_card_ids=(),
        generated_card_ids=(),
        source_passage_ids=(),
        forbidden_cloze_targets=(),
        prompt_sync_stale=False,
        untagged_rate=0,
        covered_concept_ids_by_nid={1: ("C01",)},
    )
    report = reconcile_card_centric(snapshot)

    assert any(item.assertion_id == "selection_warning_floor" for item in report.warned)


def test_s7_split_rows_expose_sequential_index_expected_red_p3_h8() -> None:
    """P3 H-8: a split-card resolution must carry an explicit sequential index."""
    resolution = GeneratedCardResolution(
        card_id="split-row",
        concept_id="C02",
        fact_id="C02-M1",
        text="The first substrate is {{c1::glycine}}.",
        source_passage_ids=("SLD:fixture:P:001",),
        evidence_ids=("evidence-1",),
        split=True,
    )

    assert hasattr(resolution, "split_index")
