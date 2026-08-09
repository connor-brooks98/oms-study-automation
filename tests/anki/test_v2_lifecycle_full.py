"""P4-A executable real-handler lifecycle assertions for card_centric_v2."""

from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import replace

import numpy as np

from oms_hub.anki.card_centric import select_high_yield_v2
from oms_hub.anki.card_centric_contracts import (
    CardCentricSourceIndex,
    CardClassification,
    CardConcept,
    CardConceptLedger,
    ClassifierResult,
    ClassifierTelemetry,
    FastCardClassification,
    FastClassificationResult,
    GeneratedCardResolution,
)
from oms_hub.anki.correction_contracts import (
    DuplicateIdentity,
    GeneratedFactResolution,
    GeneratedResolutionKind,
)
from oms_hub.anki.domain import (
    CreateCurationJob,
    CurationStage,
    PipelineContractVersion,
    ResolvedClassifierExecution,
)
from oms_hub.anki.prompt_catalog import AnkiPromptCatalogService
from oms_hub.anki.reconciliation import (
    AuditResolution,
    CardCentricReconciliationInput,
    reconcile_card_centric,
)
from oms_hub.anki.repository import AnkiCurationRepository
from oms_hub.anki.semantic.domain import PinnedCentroidSimilarityResult
from oms_hub.anki.stages import CurationServicesRunner
from oms_hub.db import Database
from oms_hub.llm.structured import StructuredTextService
from oms_hub.models import LectureModel
from tests.anki.fixtures.card_centric_v2_lifecycle import (
    CardCentricV2LifecycleHarness,
    DeterministicEmbeddingClient,
    DeterministicStructuredGenerator,
)
from tests.anki.fixtures.card_centric_v2_lifecycle_data import (
    LifecycleRepository,
    LifecycleSemanticService,
    lifecycle_empty_a11_history,
    lifecycle_job,
    lifecycle_ledger,
    lifecycle_pinned_lecture,
    lifecycle_preflight,
    lifecycle_source_payload,
    payloads,
)


def _s9_snapshot(*, generated_cards: tuple, selected_generated: tuple[str, ...]):
    """Build a minimal real S9 input while keeping tests focused on one invariant."""
    from oms_hub.anki.reconciliation import CardCentricReconciliationInput

    return CardCentricReconciliationInput(
        pipeline_contract_version="card_centric_v2",
        concept_ids=("C01",),
        coverage={"C01": "covered"},
        required_fact_ids=(),
        uncovered_after_s5=(),
        residual_ran_for=(),
        generated_cards=generated_cards,
        canonical_generated_cards=generated_cards,
        unresolved_fact_ids=(),
        expected_scoped_nids=(),
        classifications=(),
        eligible_yes_nids=(),
        selected_nids=(),
        selected_generated_card_ids=selected_generated,
        generated_card_ids=tuple(item.card_id for item in generated_cards),
        source_passage_ids=(),
        forbidden_cloze_targets=(),
        prompt_sync_stale=False,
        untagged_rate=0,
        generated_concept_id_by_card_id={item.card_id: "C01" for item in generated_cards},
        mandatory_generated_card_ids=selected_generated,
    )


def _normalized_selection_result(
    result: object,
) -> tuple[tuple[int, ...], tuple[int, ...], tuple[str, ...], tuple[dict[str, object], ...]]:
    """Normalize S0's selector tuple and P3's metadata-bearing selection result."""
    if isinstance(result, tuple) and len(result) == 3:
        selected_existing, excluded_existing, selected_generated = result
        return tuple(selected_existing), tuple(excluded_existing), tuple(selected_generated), ()
    raw_metadata = result.selection_metadata  # type: ignore[attr-defined]
    metadata = tuple(
        item.model_dump(mode="json") if hasattr(item, "model_dump") else dict(item)
        for item in raw_metadata
    )
    return (
        tuple(result.selected_existing_note_ids),  # type: ignore[attr-defined]
        tuple(result.excluded_existing_note_ids),  # type: ignore[attr-defined]
        tuple(result.selected_generated_card_ids),  # type: ignore[attr-defined]
        metadata,
    )


def _independent_ledger(count: int, *, mandatory: bool, low: bool = False) -> CardConceptLedger:
    """Build distinct independently selectable concept coverage for selector boundaries."""
    return CardConceptLedger(
        lecture_entity_count=count,
        concepts=tuple(
            CardConcept(
                concept_id=f"C{index:02d}",
                canonical_statement=f"Independent fact {index}.",
                primary_entity=f"independent entity {index}",
                depth="deep" if mandatory else "surface" if low else "medium",
                emphasis_flag=mandatory,
                importance="high" if mandatory else "low" if low else "medium",
                fact_descriptions=(f"Independent fact {index}.",),
                forbidden_cloze_targets_by_fact=((),),
            )
            for index in range(1, count + 1)
        ),
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

    async def pinned_centroid_similarity(
        self,
        concept_terms: tuple[tuple[str, ...], ...],
        *,
        note_ids: tuple[int, ...],
        expected_generation: str,
    ) -> PinnedCentroidSimilarityResult:
        del concept_terms
        assert expected_generation == "fixture-generation"
        return PinnedCentroidSimilarityResult(scores={note_id: 0.90 for note_id in note_ids})

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


def test_m13_real_handlers_use_the_persisted_resolved_model_routes() -> None:
    """M-13/D18: S2, S4c, and S7 use the job's pinned routes and retain their model document."""

    async def scenario() -> None:
        source = lifecycle_source_payload()
        ledger = lifecycle_ledger()
        ledger = ledger.model_copy(
            update={
                "concepts": ledger.concepts
                + tuple(
                    CardConcept(
                        concept_id=f"C{concept_number:02d}",
                        canonical_statement=(
                            f"Heme synthesis review concept {concept_number}."
                        ),
                        primary_entity="Heme synthesis",
                        aliases=("heme",),
                        depth="medium",
                        emphasis_flag=False,
                        importance="medium",
                        fact_descriptions=(
                            f"Heme synthesis review concept {concept_number}.",
                        ),
                        forbidden_cloze_targets_by_fact=((),),
                    )
                    for concept_number in range(3, 12)
                ),
            }
        )
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
                    "covered_concept_ids": [f"C{note_id + 1:02d}"],
                    "supporting_passage_ids": [slide_id],
                    "flags": [],
                }
                for note_id in range(2, 11)
            ]
        }
        fast_gap = {
            "resolutions": [
                {
                    "fact_id": "C01-M1",
                    "status": "generated",
                    "text": "Heme synthesis begins in {{c1::mitochondria}}.",
                    "extra": "Fast-only terminal replacement.",
                    "note_type": "Cloze",
                    "source_passage_ids": [slide_id],
                }
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
                    "split_index": 1,
                },
                {
                    "fact_id": "C02-M1",
                    "status": "generated",
                    "text": "ALA synthase also uses {{c1::succinyl-CoA}}.",
                    "extra": "Second split.",
                    "note_type": "Cloze",
                    "source_passage_ids": [slide_id],
                    "split": True,
                    "split_index": 2,
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
        harness, generator = _runner(
            [ledger.model_dump(mode="json"), fast, thorough, fast_gap, gap],
            embedder=_UniqueEmbeddings(),
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
        routes = job.resolved_model_config
        assert [(call[2].value, call[3]) for call in generator.calls] == [
            (routes.ledger_s2.provider, routes.ledger_s2.model),
            (routes.fast_classify_s4b.provider, routes.fast_classify_s4b.model),
            (routes.classify_s4.provider, routes.classify_s4.model),
            (routes.gap_fill_s7.provider, routes.gap_fill_s7.model),
            (routes.gap_fill_s7.provider, routes.gap_fill_s7.model),
        ]
        assert products[CurationStage.CARD_LEDGER].payload["provenance"] == {
            "provider": routes.ledger_s2.provider,
            "model": routes.ledger_s2.model,
            "request_id": "fixture-001",
            "cache_prefix_sha256": products[CurationStage.CARD_LEDGER].payload["provenance"][
                "cache_prefix_sha256"
            ],
        }
        assert (
            products[CurationStage.CARD_CLASSIFY].payload["model_config"]
            == routes.canonical_document()
        )
        assert (
            products[CurationStage.CARD_FAST_CLASSIFY].payload["model_config"]
            == routes.canonical_document()
        )

    asyncio.run(scenario())


def test_m13_repository_reload_preserves_resolved_model_document_and_hash(tmp_path) -> None:
    """M-13/D18: persisted S2/S4c/S7 routes reload with their canonical configuration hash."""
    database = Database(f"sqlite:///{tmp_path / 'models.db'}")
    database.migrate()
    try:
        with database.session() as session:
            lecture = LectureModel(
                subject="Heme",
                exam_number=1,
                lecture_number=1,
                topic="Synthesis",
                lecturer="Fixture",
            )
            session.add(lecture)
            session.flush()
            lecture_id = lecture.id
        repository = AnkiCurationRepository(database)
        resolved = replace(
            lifecycle_job().resolved_model_config,
            classifier_execution=ResolvedClassifierExecution(
                fast_concurrency=7,
                thorough_batch_size=31,
                thorough_concurrency=3,
                thinking_budget_tokens=2048,
            ),
        )
        job = repository.create_job(
            CreateCurationJob(
                lecture_id=lecture_id,
                block_id=None,
                source_revision_ids=(1,),
                deck_allowlist=("Medical",),
                tag_allowlist=("heme",),
                instruction_text="",
                target_deck="OMS::Heme",
                target_tag="fixture",
                index_snapshot_id="fixture-snapshot",
                lcl_prompt_version="lecture-concept-ledger",
                judgment_rubric_version="coverage-rubric",
                gap_prompt_version="gap-card-generation",
                provider="anthropic",
                model="fixture-model",
                pipeline_contract_version=PipelineContractVersion.CARD_CENTRIC_V2,
                resolved_model_config=resolved,
            )
        )
        reloaded = repository.require_job(job.id)
        canonical = json.dumps(resolved.canonical_document(), sort_keys=True, separators=(",", ":"))

        assert reloaded.resolved_model_config.canonical_document() == resolved.canonical_document()
        assert reloaded.resolved_model_config.classifier_execution == (
            resolved.classifier_execution
        )
        assert reloaded.model_config_sha256 == hashlib.sha256(canonical.encode()).hexdigest()
    finally:
        database.close()


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
                    "split_index": 1,
                },
                {
                    "fact_id": "C02-M1",
                    "status": "generated",
                    "text": "ALA synthase also uses {{c1::succinyl-CoA}}.",
                    "extra": "Second split.",
                    "note_type": "Cloze",
                    "source_passage_ids": [slide_id],
                    "split": True,
                    "split_index": 2,
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
                **{
                    f"C{concept_number:02d}": {"status": "covered", "evidence": []}
                    for concept_number in range(3, 12)
                },
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


def test_l1_real_handler_s9_constructs_holistic_reconciliation_snapshot() -> None:
    """L-1: S9 constructs every reconciliation-contract field from upstream artifacts."""

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
        base_ledger = lifecycle_ledger()
        ledger = base_ledger.model_copy(
            update={
                "concepts": base_ledger.concepts
                + tuple(
                    CardConcept(
                        concept_id=f"C{concept_number:02d}",
                        canonical_statement=(
                            f"Heme synthesis review concept {concept_number}."
                        ),
                        primary_entity="Heme synthesis",
                        aliases=("heme",),
                        depth="medium",
                        emphasis_flag=False,
                        importance="medium",
                        fact_descriptions=(
                            f"Heme synthesis review concept {concept_number}.",
                        ),
                        forbidden_cloze_targets_by_fact=((),),
                    )
                    for concept_number in range(3, 12)
                ),
            }
        )
        prior[CurationStage.CARD_LEDGER] = {"ledger": ledger.model_dump(mode="json")}
        classifier = ClassifierResult(
            results=tuple(
                CardClassification(
                    note_id=note_id,
                    verdict="YES",
                    primary_subject="heme synthesis",
                    reason="Grounded lifecycle fixture coverage.",
                    covered_concept_ids=(
                        "C01" if note_id == 1 else f"C{note_id + 1:02d}",
                    ),
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
                split_index=index if fact_id == "C02-M1" else None,
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
                **{
                    f"C{concept_number:02d}": {"status": "covered", "evidence": []}
                    for concept_number in range(3, 12)
                },
            }
        }
        prior[CurationStage.CARD_RESIDUAL] = {
            "classifier": None,
            "uncovered_concept_ids": ["C02"],
        }
        prior[CurationStage.DEDUPE] = {
            "resolutions": [item.model_dump(mode="json") for item in generated]
        }
        prior[CurationStage.CARD_GAP_FILL] = {
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
        assert report.payload["can_render_envelope"] is True, report.payload["failed"]
        assert report.payload["failed"] == []
        assert selection.kind == "card_centric_selection"
        assert selection.payload["selected_existing_note_ids"] == [1, 10, *range(2, 10)]
        assert selection.payload["selected_generated_card_ids"] == ["G1", "G3", "G4", "G2"]
        assert selection.payload["minimum_target"] == 60
        assert selection.payload["target"] == 65
        assert selection.payload["cap"] == 70
        snapshot = report.payload["snapshot"]
        assert {item["fact_id"] for item in snapshot["canonical_generated_cards"]} == {
            "C02-M1",
            "C02-M2",
            "C02-M3",
        }
        assert snapshot["selected_generated_card_ids"] == ["G1", "G3", "G4", "G2"]
        assert snapshot["canonical_unresolved_fact_ids"] == []
        assert [item["nid"] for item in snapshot["classifications"]] == list(range(1, 11))
        assert snapshot["selected_nids"] == [1, 10, *range(2, 10)]
        from oms_hub.anki.reconciliation import CardCentricReconciliationInput

        assert set(snapshot) == set(CardCentricReconciliationInput.model_fields)
        assert snapshot["concept_ids"] == [f"C{number:02d}" for number in range(1, 12)]
        assert snapshot["coverage"] == {
            "C01": "covered",
            "C02": "covered",
            **{f"C{number:02d}": "covered" for number in range(3, 12)},
        }
        assert snapshot["required_fact_ids"] == ["C02-M1", "C02-M2", "C02-M3"]
        assert snapshot["uncovered_after_s5"] == ["C02"]
        assert snapshot["residual_ran_for"] == ["C02"]
        assert snapshot["generated_card_ids"] == ["G1", "G2", "G3", "G4"]
        assert snapshot["expected_scoped_nids"] == list(range(1, 11))
        assert snapshot["eligible_yes_nids"] == list(range(1, 11))
        assert snapshot["source_passage_ids"] == [
            passage["passage_id"] for passage in source["source_index"]["passages"]
        ]
        assert snapshot["prompt_sync_stale"] is False
        assert snapshot["untagged_rate"] == 0.0

    asyncio.run(scenario())


def test_expected_red_p3_h3_s8_duplicate_identity_survives_into_s9_audit() -> None:
    """P3 H-3: S8 duplicate terminal status survives S9 audit, never an intentional gap."""

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
                *(
                    CardClassification(
                        note_id=note_id,
                        verdict="NO",
                        primary_subject="fixture",
                        reason="Not relevant.",
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

        # H-3 preserves the S8 terminal duplicate in the canonical S9 audit even
        # though it is not selectable as a generated card.
        prior[CurationStage.DEDUPE] = product.payload
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
        scope = await harness.invoke(
            job=lifecycle_job(), stage=CurationStage.CARD_TAG_SCOPE, prior_payloads=prior
        )
        prior[CurationStage.CARD_TAG_SCOPE] = scope.payload
        selection = await harness.invoke(
            job=lifecycle_job(), stage=CurationStage.CARD_SELECTION, prior_payloads=prior
        )
        prior[CurationStage.CARD_SELECTION] = selection.payload
        s9 = await harness.invoke(
            job=lifecycle_job(),
            stage=CurationStage.RECONCILIATION,
            prior_payloads=prior,
            replay_inputs={"a11_history": lifecycle_empty_a11_history()},
            replay_inputs_sha256="a" * 64,
        )

        assert selection.payload["selected_generated_card_ids"] == []
        terminals = s9.payload["snapshot"]["terminal_resolutions"]
        assert len(terminals) == 1
        assert terminals[0]["fact_id"] == "C02-M1"
        assert terminals[0]["kind"] == "duplicate_of_existing"
        assert terminals[0]["duplicate_of"] == {
            "correction_contract_version": 1,
            "existing_note_id": 1,
            "generated_card_id": None,
        }

    asyncio.run(scenario())


def test_duplicate_target_identity_survives_selection_into_reconciliation() -> None:
    """A named S8 duplicate target cannot be replaced by equivalent coverage."""
    source = lifecycle_source_payload(card_count=11)
    source_index = CardCentricSourceIndex.model_validate(source["source_index"])
    primary_id = next(
        passage.passage_id for passage in source_index.passages if passage.authority == "slide"
    )
    summary_id = next(
        passage.passage_id for passage in source_index.passages if passage.authority == "summary"
    )
    ledger = _independent_ledger(10, mandatory=False)
    duplicate = GeneratedCardResolution(
        card_id="G-duplicate",
        concept_id="C01",
        fact_id="C01-M1",
        text="{{c1::Duplicate fact}}",
        source_passage_ids=(primary_id,),
        evidence_ids=("E-duplicate",),
        status="duplicate_of_existing",
        duplicate_of_existing_note_id=11,
        reason="Semantic duplicate of existing note 11.",
    )
    classifications = (
        *(
            CardClassification(
                note_id=note_id,
                verdict="YES",
                primary_subject="fixture",
                reason="Unique grounded coverage.",
                covered_concept_ids=(f"C{note_id + 1:02d}",),
                supporting_passage_ids=(primary_id,),
            )
            for note_id in range(1, 10)
        ),
        CardClassification(
            note_id=10,
            verdict="YES",
            primary_subject="fixture",
            reason="Higher-ranked equivalent coverage.",
            covered_concept_ids=("C01",),
            supporting_passage_ids=(primary_id,),
        ),
        CardClassification(
            note_id=11,
            verdict="YES",
            primary_subject="fixture",
            reason="Exact S8 duplicate target.",
            covered_concept_ids=("C01",),
            supporting_passage_ids=(summary_id,),
        ),
    )

    selection = select_high_yield_v2(
        classifications,
        fast_classifications=(),
        ledger=ledger,
        source_index=source_index,
        generated_cards=(duplicate,),
    )
    snapshot = CardCentricReconciliationInput(
        pipeline_contract_version="card_centric_v2",
        concept_ids=tuple(concept.concept_id for concept in ledger.concepts),
        coverage={concept.concept_id: "covered" for concept in ledger.concepts},
        required_fact_ids=("C01-M1",),
        uncovered_after_s5=("C01",),
        residual_ran_for=("C01",),
        generated_cards=(),
        raw_generated_cards=(),
        canonical_generated_cards=(),
        terminal_resolutions=(
            GeneratedFactResolution(
                fact_id="C01-M1",
                kind=GeneratedResolutionKind.DUPLICATE_OF_EXISTING,
                duplicate_of=DuplicateIdentity(existing_note_id=11),
            ),
        ),
        terminal_resolutions_provided=True,
        canonical_unresolved_fact_ids=(),
        unresolved_fact_ids=(),
        expected_scoped_nids=tuple(range(1, 12)),
        classifications=tuple(
            AuditResolution(nid=classification.note_id, verdict="keep")
            for classification in classifications
        ),
        eligible_yes_nids=tuple(range(1, 12)),
        selected_nids=selection.selected_existing_note_ids,
        selected_generated_card_ids=(),
        generated_card_ids=(),
        source_passage_ids=tuple(passage.passage_id for passage in source_index.passages),
        forbidden_cloze_targets=(),
        prompt_sync_stale=False,
        untagged_rate=0,
        mandatory_nids=selection.mandatory_note_ids,
        covered_concept_ids_by_nid={
            classification.note_id: classification.covered_concept_ids
            for classification in classifications
        },
        selection_metadata=selection.selection_metadata,
        selection_order=tuple(item.identity for item in selection.selection_metadata),
        selected_count=len(selection.selected_existing_note_ids),
        below_warning_floor=selection.below_warning_floor,
    )

    report = reconcile_card_centric(snapshot)

    assert selection.selected_existing_note_ids == (11, *range(1, 10))
    assert 10 in selection.excluded_existing_note_ids
    assert report.failed == ()
    # The deliberately 10-card fixture still warns below the policy floor;
    # its envelope status is unrelated to the duplicate-target invariant.
    assert any(item.assertion_id == "selection_warning_floor" for item in report.warned)
    assert "duplicate_coverage" in report.passed


def test_fast_only_duplicate_terminal_does_not_promote_target_after_floor() -> None:
    """Malformed fast-only duplicate terminals cannot bypass the T6 floor."""
    source = lifecycle_source_payload(card_count=61)
    source_index = CardCentricSourceIndex.model_validate(source["source_index"])
    primary_id = next(
        passage.passage_id for passage in source_index.passages if passage.authority == "slide"
    )
    terminal = (
        GeneratedCardResolution(
            card_id="G-fast-duplicate",
            concept_id="C61",
            fact_id="C61-M1",
            text="{{c1::Fast-only duplicate fact}}",
            source_passage_ids=(primary_id,),
            evidence_ids=("E-fast-duplicate",),
            status="duplicate_of_existing",
            duplicate_of_existing_note_id=61,
            reason="Semantic duplicate of existing fast note 61.",
        ),
    )
    classifications = tuple(
        CardClassification(
            note_id=note_id,
            verdict="YES",
            primary_subject="fixture",
            reason="Independent grounded coverage.",
            covered_concept_ids=(f"C{note_id:02d}",),
            supporting_passage_ids=(primary_id,),
        )
        for note_id in range(1, 61)
    )
    fast_classifications = (
        FastCardClassification(
            note_id=61,
            verdict="LIKELY_YES",
            grounded_concept_ids=("C61",),
            supporting_passage_ids=(primary_id,),
            reason="Fast-only historical S8 target.",
        ),
    )
    selection = select_high_yield_v2(
        classifications,
        fast_classifications=fast_classifications,
        ledger=_independent_ledger(61, mandatory=False),
        source_index=source_index,
        generated_cards=terminal,
    )

    assert set(selection.selected_existing_note_ids) == set(range(1, 61))
    assert len(selection.selected_existing_note_ids) == 60
    assert 61 in selection.excluded_existing_note_ids
    assert selection.mandatory_note_ids == ()
    assert all(item.identity != "existing:61" for item in selection.selection_metadata)


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
        prior[CurationStage.DEDUPE] = {
            "resolutions": [
                GeneratedCardResolution(
                    card_id=f"G{index}",
                    concept_id="C02",
                    fact_id=f"C02-M{index}",
                    text=f"Generated {{c1::fact {index}}}.",
                    source_passage_ids=(slide_id,),
                    evidence_ids=(f"E{index}",),
                ).model_dump(mode="json")
                for index in range(1, 61)
            ]
        }

        selection = await harness.invoke(
            job=lifecycle_job(), stage=CurationStage.CARD_SELECTION, prior_payloads=prior
        )

        assert 1 not in selection.payload["selected_existing_note_ids"]

    asyncio.run(scenario())


def test_fast_only_concept_gets_terminal_replacement_after_floor_before_s9() -> None:
    """Fast evidence remains visible but cannot suppress the terminal S7/S9 path."""

    async def scenario() -> None:
        source = lifecycle_source_payload(card_count=61)
        slide_id = next(
            passage["passage_id"]
            for passage in source["source_index"]["passages"]
            if passage["authority"] == "slide"
        )
        ledger = _independent_ledger(61, mandatory=False)
        gap = {
            "resolutions": [
                {
                    "fact_id": "C61-M1",
                    "status": "generated",
                    # This is an exact semantic match for note 1, whose only
                    # classification is fast-pass LIKELY_YES below.
                    "text": "Heme synthesis begins in {{c1::mitochondria}}.",
                    "extra": "Fixture card.",
                    "note_type": "Cloze",
                    "source_passage_ids": [slide_id],
                }
            ]
        }
        harness, _ = _runner([gap], embedder=_UniqueEmbeddings())
        harness.runner.semantic = _NoResidualHits()
        job = lifecycle_job()
        prior = payloads(source=source, preflight=lifecycle_preflight())
        scope = await harness.invoke(
            job=job, stage=CurationStage.CARD_TAG_SCOPE, prior_payloads=prior
        )
        prior[CurationStage.CARD_TAG_SCOPE] = scope.payload
        prior[CurationStage.CARD_LEDGER] = {"ledger": ledger.model_dump(mode="json")}
        prior[CurationStage.CARD_CLASSIFY] = {
            "classifier": ClassifierResult(
                results=tuple(
                    CardClassification(
                        note_id=index,
                        verdict="YES",
                        primary_subject=f"independent entity {index}",
                        reason="Grounded ordinary coverage.",
                        covered_concept_ids=(f"C{index - 1:02d}",),
                        supporting_passage_ids=(slide_id,),
                    )
                    for index in range(2, 62)
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
            ).model_dump(mode="json")
        }
        prior[CurationStage.CARD_FAST_CLASSIFY] = {
            "fast_classifier": FastClassificationResult(
                results=(
                    FastCardClassification(
                        note_id=1,
                        verdict="LIKELY_YES",
                        grounded_concept_ids=("C61",),
                        supporting_passage_ids=(slide_id,),
                        reason="Grounded fast-only coverage.",
                    ),
                )
            ).model_dump(mode="json"),
            "fallback_note_ids": [],
        }

        coverage = await harness.invoke(
            job=job, stage=CurationStage.CARD_COVERAGE, prior_payloads=prior
        )
        prior[CurationStage.CARD_COVERAGE] = coverage.payload
        assert coverage.payload["coverage"]["C61"] == {
            "status": "covered",
            "evidence": [
                {
                    "note_id": 1,
                    "supporting_passage_ids": [slide_id],
                    "evidence_quality": "fast_pass",
                }
            ],
        }

        residual = await harness.invoke(
            job=job, stage=CurationStage.CARD_RESIDUAL, prior_payloads=prior
        )
        prior[CurationStage.CARD_RESIDUAL] = residual.payload
        assert residual.payload["uncovered_concept_ids"] == ["C61"]
        generated = await harness.invoke(
            job=job, stage=CurationStage.CARD_GAP_FILL, prior_payloads=prior
        )
        prior[CurationStage.CARD_GAP_FILL] = generated.payload
        assert [row["fact_id"] for row in generated.payload["resolutions"]] == ["C61-M1"]

        deduped = await harness.invoke(
            job=job, stage=CurationStage.DEDUPE, prior_payloads=prior
        )
        prior[CurationStage.DEDUPE] = deduped.payload
        resolution = deduped.payload["resolutions"][0]
        assert resolution["status"] == "generated"
        assert resolution["duplicate_of_existing_note_id"] is None
        assert resolution["duplicate_of_generated_card_id"] is None
        generated_card_id = resolution["card_id"]
        selection = await harness.invoke(
            job=job, stage=CurationStage.CARD_SELECTION, prior_payloads=prior
        )
        prior[CurationStage.CARD_SELECTION] = selection.payload
        reconciliation = await harness.invoke(
            job=job, stage=CurationStage.RECONCILIATION, prior_payloads=prior
        )

        assert selection.payload["selected_count"] == 61
        assert 1 not in selection.payload["selected_existing_note_ids"]
        assert selection.payload["selected_generated_card_ids"] == [generated_card_id]
        terminal = reconciliation.payload["snapshot"]["terminal_resolutions"]
        assert len(terminal) == 1
        assert terminal[0]["fact_id"] == "C61-M1"
        assert terminal[0]["kind"] == "generated"
        assert terminal[0]["generated_card_ids"] == [generated_card_id]
        assert terminal[0]["duplicate_of"] is None
        assert reconciliation.payload["failed"] == []
        assert reconciliation.blocking_error is None
        assert "A4" in reconciliation.payload["passed"]
        assert "duplicate_coverage" in reconciliation.payload["passed"]

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
            {"identity": "generated:G-T1", "selected_position": 1, "tier": "T1"},
            {"identity": "generated:G-T2", "selected_position": 2, "tier": "T2"},
            {"identity": "existing:1", "selected_position": 3, "tier": "T3"},
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
    """P3 H-5: selection exposes quality-first nonredundancy rather than count padding."""

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

        assert selection.payload["selection_metadata"] == [
            {
                "correction_contract_version": 1,
                "identity": "existing:1",
                "selected_position": 1,
                "tier": "T3",
                "evidence_quality": "primary_source",
                "mandatory": True,
                "marginal_value_reason": None,
                "overflow_reason": None,
                "manual_acknowledgement_required": False,
            }
        ], "P3 H-5: Selection lacks quality-first nonredundant coverage evidence"
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


def test_h2_generated_oversupply_is_carried_from_selection_to_s9_pending_overflow() -> None:
    """H-2: ten mandatory existing cards leave only target capacity for generated facts."""
    from oms_hub.anki.reconciliation import GeneratedResolution, reconcile_card_centric

    source = lifecycle_source_payload()
    source_index = CardCentricSourceIndex.model_validate(source["source_index"])
    mandatory_ledger = _independent_ledger(10, mandatory=True)
    ledger = CardConceptLedger(
        lecture_entity_count=11,
        concepts=(
            *mandatory_ledger.concepts,
            CardConcept(
                concept_id="C11",
                canonical_statement="Independent low-priority generated fact coverage.",
                primary_entity="independent generated entity",
                depth="surface",
                emphasis_flag=False,
                importance="low",
                fact_descriptions=("Independent low-priority generated fact coverage.",),
                forbidden_cloze_targets_by_fact=((),),
            ),
        ),
    )
    generated = tuple(
        GeneratedCardResolution(
            card_id=f"G-{number:03d}",
            concept_id="C11",
            fact_id=f"C11-M{number}",
            text=f"Generated {{{{c1::value-{number}}}}}.",
            source_passage_ids=(source_index.passages[0].passage_id,),
            evidence_ids=(f"E-{number}",),
        )
        for number in range(1, 65)
    )
    existing = tuple(
        CardClassification(
            note_id=note_id,
            verdict="YES",
            primary_subject="fixture",
            reason="Mandatory high-priority existing coverage.",
            covered_concept_ids=(f"C{note_id:02d}",),
            supporting_passage_ids=(source_index.passages[0].passage_id,),
        )
        for note_id in range(1, 11)
    )
    selection_result = select_high_yield_v2(
        existing,
        fast_classifications=(),
        ledger=ledger,
        source_index=source_index,
        generated_cards=generated,
    )
    notes, _excluded, selected, _metadata = _normalized_selection_result(selection_result)
    selection_updates: dict[str, object] = {}
    if hasattr(selection_result, "selection_metadata"):
        raw_metadata = selection_result.selection_metadata  # type: ignore[attr-defined]
        selection_updates = {
            "selection_metadata": raw_metadata,
            "selection_order": tuple(
                item.identity
                for item in sorted(raw_metadata, key=lambda item: item.selected_position)
            ),
            "selected_count": len(notes) + len(selected),
            "below_warning_floor": selection_result.below_warning_floor,  # type: ignore[attr-defined]
            "mandatory_nids": selection_result.mandatory_note_ids,  # type: ignore[attr-defined]
            "mandatory_generated_card_ids": (  # type: ignore[attr-defined]
                selection_result.mandatory_generated_card_ids
            ),
        }
    s9_generated = tuple(
        GeneratedResolution(card_id=item.card_id, fact_id=item.fact_id, text=item.text)
        for item in generated
    )
    terminal_updates: dict[str, object] = {}
    try:
        from oms_hub.anki.correction_contracts import (
            GeneratedFactResolution,
            GeneratedResolutionKind,
        )
    except ImportError:
        # S0 has not yet introduced the immutable terminal map.  Its legacy
        # A1/A2 path still checks the same canonical generated rows below.
        pass
    else:
        terminal_updates = {
            "raw_generated_cards": s9_generated,
            "terminal_resolutions": tuple(
                GeneratedFactResolution(
                    fact_id=item.fact_id,
                    kind=GeneratedResolutionKind.GENERATED,
                    generated_card_ids=(item.card_id,),
                )
                for item in generated
            ),
            "terminal_resolutions_provided": True,
        }
    concept_ids = tuple(concept.concept_id for concept in ledger.concepts)
    snapshot = _s9_snapshot(generated_cards=s9_generated, selected_generated=selected).model_copy(
        update={
            "cap": 70,
            "concept_ids": concept_ids,
            "coverage": {concept_id: "covered" for concept_id in concept_ids},
            "required_fact_ids": tuple(item.fact_id for item in generated),
            "selected_nids": notes,
            "eligible_yes_nids": notes,
            "covered_concept_ids_by_nid": {note_id: (f"C{note_id:02d}",) for note_id in notes},
            "generated_concept_id_by_card_id": {item.card_id: "C11" for item in generated},
            **terminal_updates,
            **selection_updates,
        }
    )
    report = reconcile_card_centric(snapshot)

    assert set(notes) == set(range(1, 11))
    assert 0 < len(selected) < len(generated)
    assert len(snapshot.canonical_generated_cards) == 64
    assert set(snapshot.generated_card_ids) - set(snapshot.selected_generated_card_ids)
    assert report.failed == ()
    assert not {item.assertion_id for item in report.failed} & {"A1", "A2"}
    assert report.can_render_envelope is True


def test_expected_red_p3_h9_a5_validates_unselected_generated_output() -> None:
    """P3 H-9: A5/A5b must reject malformed or forbidden unselected S7/S8 output too."""
    from oms_hub.anki.reconciliation import GeneratedResolution, reconcile_card_centric

    invalid = GeneratedCardResolution(
        card_id="unselected-forbidden",
        concept_id="C01",
        fact_id="C01-M1",
        text="This blanks {{c1::mitochondria}}.",
        source_passage_ids=("SLD:fixture:P:001",),
        evidence_ids=("evidence-1",),
    )
    snapshot = _s9_snapshot(generated_cards=(), selected_generated=()).model_copy(
        update={
            "canonical_generated_cards": (
                GeneratedResolution(
                    card_id=invalid.card_id,
                    fact_id=invalid.fact_id,
                    text=invalid.text,
                ),
            ),
            "forbidden_cloze_targets": ("mitochondria",),
        }
    )
    report = reconcile_card_centric(snapshot)

    assert "A5" in {item.assertion_id for item in report.failed}, (
        "P3 H-9: S9 currently validates only selected generated_cards and leaves canonical "
        "unselected generated output outside A5/A5b"
    )


def test_expected_red_p3_h6_h9_a5_uses_fact_local_forbidden_targets_for_unselected_rows() -> None:
    """P3 H-6/H-9: A5 validates every canonical fact against only that fact's targets."""
    from oms_hub.anki.reconciliation import GeneratedResolution, reconcile_card_centric

    # M1 forbids mitochondria; M2 forbids glycine.  Glycine in M1 is valid and
    # must not be contaminated by M2's local prohibition.
    allowed = GeneratedResolution(
        card_id="fact-a-allowed",
        fact_id="C01-M1",
        text="This fact keeps {{c1::glycine}} visible.",
    )
    forbidden_a = GeneratedResolution(
        card_id="fact-a-forbidden",
        fact_id="C01-M1",
        text="This fact blanks {{c1::mitochondria}}.",
    )
    forbidden_b = GeneratedResolution(
        card_id="fact-b-forbidden",
        fact_id="C02-M1",
        text="This fact blanks {{c1::glycine}}.",
    )
    base = _s9_snapshot(generated_cards=(), selected_generated=()).model_copy(
        update={
            "forbidden_cloze_targets_by_fact": {
                "C01-M1": ("mitochondria",),
                "C02-M1": ("glycine",),
            }
        }
    )
    allowed_report = reconcile_card_centric(
        base.model_copy(update={"canonical_generated_cards": (allowed,)})
    )
    fact_a_report = reconcile_card_centric(
        base.model_copy(update={"canonical_generated_cards": (forbidden_a,)})
    )
    fact_b_report = reconcile_card_centric(
        base.model_copy(update={"canonical_generated_cards": (forbidden_b,)})
    )
    assert "A5" not in {item.assertion_id for item in allowed_report.failed}
    assert "A5" in {item.assertion_id for item in fact_a_report.failed}
    assert "A5" in {item.assertion_id for item in fact_b_report.failed}


def test_h6_s7_provider_input_keeps_forbidden_cloze_targets_per_fact() -> None:
    """P3 H-6: S7 must send each fact only its own forbidden-cloze targets."""

    async def scenario() -> None:
        import json

        source = lifecycle_source_payload()
        ledger = lifecycle_ledger().model_copy(
            update={
                "concepts": (
                    lifecycle_ledger().concepts[0],
                    lifecycle_ledger()
                    .concepts[1]
                    .model_copy(
                        update={
                            "forbidden_cloze_targets_by_fact": (
                                ("glycine",),
                                ("mitochondria",),
                                ("succinyl-CoA",),
                            )
                        }
                    ),
                )
            }
        )
        gap = {
            "resolutions": [
                {
                    "fact_id": f"C02-M{number}",
                    "status": "unresolved",
                    "reason": "fixture",
                    "text": "",
                    "extra": "",
                    "note_type": "Cloze",
                    "source_passage_ids": [],
                }
                for number in range(1, 4)
            ]
        }
        harness, generator = _runner([gap])
        prior = payloads(source=source, preflight=lifecycle_preflight())
        prior[CurationStage.CARD_LEDGER] = {"ledger": ledger.model_dump(mode="json")}
        prior[CurationStage.CARD_COVERAGE] = {
            "coverage": {
                "C01": {"status": "covered", "evidence": []},
                "C02": {"status": "uncovered", "evidence": []},
            }
        }
        prior[CurationStage.CARD_CLASSIFY] = {
            "classifier": ClassifierResult(
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
            ).model_dump(mode="json")
        }
        prior[CurationStage.CARD_RESIDUAL] = {"classifier": None}
        prior[CurationStage.CARD_FAST_CLASSIFY] = {
            "fast_classifier": FastClassificationResult(results=()).model_dump(mode="json"),
            "fallback_note_ids": [],
        }

        product = await harness.invoke(
            job=lifecycle_job(),
            stage=CurationStage.CARD_GAP_FILL,
            prior_payloads=prior,
            replay_inputs={"pinned_lecture": lifecycle_pinned_lecture()},
            replay_inputs_sha256="b" * 64,
        )
        request = json.loads(generator.calls[0][1])

        assert product.payload["resolutions"]
        assert request["forbidden_cloze_targets_by_fact"] == [
            {"fact_id": "C02-M1", "targets": ["glycine"]},
            {"fact_id": "C02-M2", "targets": ["mitochondria"]},
            {"fact_id": "C02-M3", "targets": ["succinyl-CoA"]},
        ]

    asyncio.run(scenario())


def test_h5_selection_keeps_quality_first_lower_bound_without_padding() -> None:
    """H-5: a grounded 10-card selection stays below 60 instead of manufacturing quota cards."""
    source = CardCentricSourceIndex.model_validate(lifecycle_source_payload()["source_index"])
    slide_id = next(
        passage.passage_id for passage in source.passages if passage.authority == "slide"
    )
    ledger = _independent_ledger(10, mandatory=False)
    classifications = tuple(
        CardClassification(
            note_id=note_id,
            verdict="YES",
            primary_subject="fixture",
            reason="grounded",
            covered_concept_ids=(f"C{note_id:02d}",),
            supporting_passage_ids=(slide_id,),
        )
        for note_id in range(1, 11)
    )
    selected, excluded, generated, _metadata = _normalized_selection_result(
        select_high_yield_v2(
            classifications,
            fast_classifications=(),
            ledger=ledger,
            source_index=source,
            generated_cards=(),
        )
    )

    assert len(selected) == 10
    assert set(selected) == set(range(1, 11))
    assert excluded == ()
    assert generated == ()


def test_h5_ordinary_eligible_candidates_stop_at_the_65_target() -> None:
    """H-5: ordinary eligible candidates stop at 65; count never justifies positions 66-70."""
    source = CardCentricSourceIndex.model_validate(lifecycle_source_payload()["source_index"])
    slide_id = next(
        passage.passage_id for passage in source.passages if passage.authority == "slide"
    )
    ledger = _independent_ledger(80, mandatory=False, low=True)
    ordinary = tuple(
        CardClassification(
            note_id=note_id,
            verdict="YES",
            primary_subject="ordinary fixture",
            reason="Grounded ordinary coverage.",
            covered_concept_ids=(f"C{note_id:02d}",),
            supporting_passage_ids=(slide_id,),
        )
        for note_id in range(1, 81)
    )

    selected, _excluded, generated, _metadata = _normalized_selection_result(
        select_high_yield_v2(
            ordinary,
            fast_classifications=(),
            ledger=ledger,
            source_index=source,
            generated_cards=(),
        )
    )

    assert len(selected) == 65
    assert generated == ()


def test_expected_red_p3_h5_positions_66_to_70_require_governed_marginal_reasons() -> None:
    """P3 H-5: 66-70 are explicit exceptions, never blank, unsupported, or count-based reasons."""
    allowed = {
        "only_valid_required_fact",
        "unique_emphasized_distinction",
        "validated_necessary_split",
    }
    source = CardCentricSourceIndex.model_validate(lifecycle_source_payload()["source_index"])
    slide_id = next(
        passage.passage_id for passage in source.passages if passage.authority == "slide"
    )
    ledger = _independent_ledger(70, mandatory=True)
    mandatory = tuple(
        CardClassification(
            note_id=note_id,
            verdict="YES",
            primary_subject="required fixture",
            reason="Grounded required high-value coverage.",
            covered_concept_ids=(f"C{note_id:02d}",),
            supporting_passage_ids=(slide_id,),
        )
        for note_id in range(1, 71)
    )
    _selected, _excluded, _generated, raw_selection_metadata = _normalized_selection_result(
        select_high_yield_v2(
            mandatory,
            fast_classifications=(),
            ledger=ledger,
            source_index=source,
            generated_cards=(),
        )
    )

    marginal = [
        item for item in raw_selection_metadata if item.get("selected_position") in range(66, 71)
    ]
    assert [item["selected_position"] for item in marginal] == list(range(66, 71))
    assert all(item.get("marginal_value_reason") in allowed for item in marginal)


def test_expected_red_p3_h5_dominance_excludes_no_better_subset_coverage() -> None:
    """P3 H-5: a no-better subset candidate cannot displace clearer atomic coverage."""
    source = CardCentricSourceIndex.model_validate(lifecycle_source_payload()["source_index"])
    slide_id = next(
        passage.passage_id for passage in source.passages if passage.authority == "slide"
    )
    clearer_atomic = CardClassification(
        note_id=1,
        verdict="YES",
        primary_subject="heme synthesis mechanism",
        reason="Grounded atomic coverage with direct slide support.",
        covered_concept_ids=("C01", "C02"),
        supporting_passage_ids=(slide_id,),
    )
    dominated_subset = CardClassification(
        note_id=2,
        verdict="YES",
        primary_subject="heme synthesis",
        reason="Grounded subset coverage with no additional evidence.",
        covered_concept_ids=("C01",),
        supporting_passage_ids=(slide_id,),
    )

    selected, excluded, generated, _metadata = _normalized_selection_result(
        select_high_yield_v2(
            (clearer_atomic, dominated_subset),
            fast_classifications=(),
            ledger=lifecycle_ledger(),
            source_index=source,
            generated_cards=(),
        )
    )

    assert selected == (1,)
    assert excluded == (2,)
    assert generated == ()


def test_s9_reconciliation_is_deterministic_for_identical_frozen_input() -> None:
    """A frozen S9 snapshot yields byte-for-byte equivalent report data on replay."""
    from oms_hub.anki.reconciliation import reconcile_card_centric

    snapshot = _s9_snapshot(generated_cards=(), selected_generated=()).model_copy(
        update={"coverage": {"C01": "intentional_gap"}}
    )
    first = reconcile_card_centric(snapshot).model_dump(mode="json")
    second = reconcile_card_centric(snapshot).model_dump(mode="json")

    assert first == second
