import asyncio
import hashlib
import json
import threading
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import numpy as np
import pytest

import oms_hub.anki.stages as stages_module
from oms_hub.anki.audit import AuditBatchV2, AuditCacheRecord
from oms_hub.anki.card_centric import (
    build_snapshot_census,
    build_source_index,
    select_high_yield_v2,
)
from oms_hub.anki.card_centric_contracts import (
    CardCentricSourceIndex,
    CardClassification,
    CardConcept,
    CardConceptLedger,
    CardGapBatch,
    CardGapOutput,
    CardRecord,
    ClassifierResult,
    ClassifierTelemetry,
    DedupeAdvisoryCandidate,
    FastCardClassification,
    FastClassificationResult,
    GeneratedCardResolution,
    QualitySelectionResult,
    SemanticDedupeReview,
    SemanticPreFilterResult,
    TagScopeResult,
)
from oms_hub.anki.correction_contracts import (
    A11HistoryEntry,
    A11HistorySnapshot,
    CanonicalJsonObject,
    DuplicateIdentity,
    EvidenceQuality,
    GeneratedFactResolution,
    MarginalValueReason,
    PinnedLectureMetadata,
    SelectionMetadata,
    SelectionTier,
)
from oms_hub.anki.dedupe import SemanticDedupeIntegrityError
from oms_hub.anki.domain import (
    Candidate,
    CurationStage,
    GapCard,
    PipelineContractVersion,
    RetrievalPass,
    SourceKind,
)
from oms_hub.anki.gaps import GapBatchV2
from oms_hub.anki.judgment import JudgmentCacheRecord
from oms_hub.anki.normalize import NormalizedNote
from oms_hub.anki.pipeline import PinnedInputChanged, StageProduct
from oms_hub.anki.prompt_catalog import AnkiPromptCatalogService
from oms_hub.anki.prompts import AnkiPromptLibrary, StaticPromptSynchronizer
from oms_hub.anki.reconciliation import (
    AssertionFinding,
    AuditResolution,
    CardCentricReconciliationInput,
    GeneratedResolution,
    ReconciliationReport,
    reconcile_card_centric,
)
from oms_hub.anki.semantic.domain import FloatMatrix, InputType, SemanticHit
from oms_hub.anki.semantic.voyage import VoyageEmbeddingError
from oms_hub.anki.sources import SourcePassage
from oms_hub.anki.stages import (
    CurationServicesRunner,
    _card_dedupe_reviews,
    _card_residual_targets,
    _effective_v2_fallback_note_ids,
    _pinned_card_v2_prompt,
    _priority_candidate_groups,
    _v2_card_candidates,
    _v2_reconciliation_classifications,
    record_exhausted_semantic_dedupe_review,
)
from oms_hub.anki.v2_contracts import (
    AuditVerdictV2,
    CoverageJudgmentV2,
    GeneratedGapCardV2,
    LectureConceptLedgerV2,
    LectureConceptV2,
    MissingFactV2,
)
from oms_hub.llm.domain import DiagnosticSource, GeneratedText, LLMRequestError, ProviderName
from oms_hub.llm.structured import StructuredJSONResult, StructuredOutputError

_CARD_GAP_FILL_PASSAGE_ID = "SLD:12:0003:P:b6a235c8f012693e"


class _DedupeEmbedder:
    def __init__(self, result: object) -> None:
        self.result = result

    async def embed(
        self,
        texts: list[str],
        *,
        input_type: InputType,
    ) -> FloatMatrix:
        assert input_type == "document"
        return self.result  # type: ignore[return-value]


class _OrthogonalDedupeEmbedder:
    async def embed(
        self,
        texts: list[str],
        *,
        input_type: InputType,
    ) -> FloatMatrix:
        assert input_type == "document"
        return [
            [1.0 if row == column else 0.0 for column in range(len(texts))]
            for row in range(len(texts))
        ]  # type: ignore[return-value]


class _FailingDedupeEmbedder:
    async def embed(
        self,
        texts: list[str],
        *,
        input_type: InputType,
    ) -> FloatMatrix:
        raise VoyageEmbeddingError("retryable provider outage")


class _RecordingDedupeEmbedder:
    def __init__(self) -> None:
        self.document_calls: list[tuple[str, ...]] = []

    async def embed(
        self,
        texts: list[str],
        *,
        input_type: InputType,
    ) -> FloatMatrix:
        assert input_type == "document"
        self.document_calls.append(tuple(texts))
        return np.asarray([[1.0, 0.0] for _ in texts], dtype=np.float32)


class _PinnedDedupeSemantic:
    def __init__(self) -> None:
        self.calls: list[tuple[tuple[int, ...], str]] = []

    async def pinned_document_vectors(
        self,
        *,
        note_ids: tuple[int, ...],
        expected_generation: str,
    ) -> dict[int, FloatMatrix]:
        self.calls.append((note_ids, expected_generation))
        return {
            note_id: np.asarray([0.0, 1.0], dtype=np.float32)
            for note_id in note_ids
        }


def _dedupe_stage_fixture(
    embedder: object,
    *,
    existing: tuple[CardClassification, ...] = (),
    cards: tuple[CardRecord, ...] = (),
) -> tuple[CurationServicesRunner, SimpleNamespace, str]:
    passage = SourcePassage.create(
        revision_id=7,
        lecture_id=12,
        artifact_id="slides-7",
        source_kind=SourceKind.SLIDE,
        locator="slide:8",
        text="Dedupe fixture evidence.",
        slide_number=8,
    )
    source = build_source_index(
        (passage,), snapshot_id="dedupe-snapshot", source_revision_hashes={7: "d" * 64}
    )
    telemetry = ClassifierTelemetry(
        batch_count=0,
        cache_prefix_sha256="e" * 64,
        cache_mode="ordinary_prefix",
        provider="openai",
        model="fixture",
        request_ids=(),
        batches=(),
    )
    runner = CurationServicesRunner.__new__(CurationServicesRunner)
    runner.embedder = embedder
    runner.semantic = _PinnedDedupeSemantic()
    context = SimpleNamespace(
        job=SimpleNamespace(
            pipeline_contract_version=PipelineContractVersion.CARD_CENTRIC_V2,
            semantic_generation="semantic-generation",
            resolved_model_config=SimpleNamespace(
                gap_fill_s7=SimpleNamespace(provider="openai", model="fixture")
            ),
            gap_prompt_version="gap-v2",
        ),
        prior_payloads={
            CurationStage.SOURCE_INDEX: {
                "source_index": source.model_dump(mode="json"),
                "cards": [card.model_dump(mode="json") for card in cards],
            },
            CurationStage.CARD_CLASSIFY: {
                "classifier": ClassifierResult(
                    results=existing,
                    telemetry=telemetry,
                ).model_dump(mode="json")
            },
            CurationStage.CARD_RESIDUAL: {},
            CurationStage.CARD_FAST_CLASSIFY: {
                "fast_classifier": FastClassificationResult(results=()).model_dump(mode="json"),
                "fallback_note_ids": [],
            },
        },
    )
    return runner, context, source.passages[0].passage_id


def _generated_dedupe_row(
    card_id: str,
    fact_id: str,
    text: str,
    passage_id: str,
) -> GeneratedCardResolution:
    return GeneratedCardResolution(
        card_id=card_id,
        concept_id="C01",
        fact_id=fact_id,
        text=text,
        source_passage_ids=(passage_id,),
        evidence_ids=("a" * 64,),
    )


def _selection_ledger(count: int, *, high_through: int = 0) -> CardConceptLedger:
    return CardConceptLedger(
        lecture_entity_count=count,
        concepts=tuple(
            CardConcept(
                concept_id=f"C{index:02d}",
                canonical_statement=f"Selection fact {index}.",
                primary_entity=f"Selection {index}",
                depth="deep" if index <= high_through else "medium",
                emphasis_flag=False,
                importance="high" if index <= high_through else "medium",
            )
            for index in range(1, count + 1)
        ),
    )


def _selection_generated_rows(count: int, passage_id: str) -> tuple[GeneratedCardResolution, ...]:
    return tuple(
        GeneratedCardResolution(
            card_id=f"G{index:02d}",
            concept_id=f"C{index:02d}",
            fact_id=f"C{index:02d}-M1",
            text=f"{{{{c1::Selection fact {index}}}}}",
            source_passage_ids=(passage_id,),
            evidence_ids=(f"{index:064x}",),
        )
        for index in range(1, count + 1)
    )


def _selection_stage_context(
    *,
    ledger: CardConceptLedger,
    generated: tuple[GeneratedCardResolution, ...],
) -> tuple[CurationServicesRunner, SimpleNamespace, str]:
    runner, context, passage_id = _dedupe_stage_fixture(_DedupeEmbedder([]))
    context.job.pipeline_contract_version = PipelineContractVersion.CARD_CENTRIC_V2
    context.prior_payloads[CurationStage.CARD_TAG_SCOPE] = {
        "scope": TagScopeResult(
            snapshot_id="dedupe-snapshot",
            filters_sha256="f" * 64,
            scoped_note_ids=(),
            unscoped_note_ids=(),
        ).model_dump(mode="json")
    }
    context.prior_payloads[CurationStage.CARD_LEDGER] = {
        "ledger": ledger.model_dump(mode="json")
    }
    context.prior_payloads[CurationStage.DEDUPE] = {
        "resolutions": [item.model_dump(mode="json") for item in generated],
        "semantic_dedupe_reviews": [],
    }
    return runner, context, passage_id


def _set_dedupe_existing(
    context: SimpleNamespace,
    note_id: int,
    passage_id: str,
    *,
    concept_id: str = "C01",
) -> None:
    context.prior_payloads[CurationStage.CARD_CLASSIFY]["classifier"]["results"] = [
        CardClassification(
            note_id=note_id,
            verdict="YES",
            primary_subject="fixture",
            reason="eligible comparison",
            covered_concept_ids=(concept_id,) if concept_id else (),
            supporting_passage_ids=(passage_id,),
        ).model_dump(mode="json")
    ]


def test_card_dedupe_v2_preserves_existing_duplicate_identity() -> None:
    note = CardRecord(
        note_id=41,
        content_sha256="1" * 64,
        text="{{c1::Alpha}} beta",
        extra="",
        tags=(),
        deck_names=("AnKing",),
    )
    runner, context, passage_id = _dedupe_stage_fixture(
        _DedupeEmbedder([]),
        cards=(note,),
    )
    _set_dedupe_existing(context, note.note_id, passage_id)

    product = asyncio.run(
        runner._card_dedupe_v2(
            context,
            {note.note_id: note},
            (_generated_dedupe_row("G01", "C01-M1", "Alpha beta", passage_id),),
        )
    )

    result = product.payload["resolutions"][0]
    assert result["status"] == "duplicate_of_existing"
    assert result["duplicate_of_existing_note_id"] == 41
    assert result["duplicate_of_generated_card_id"] is None
    terminal = GeneratedFactResolution.model_validate(product.payload["terminal_resolutions"][0])
    assert terminal.fact_id == "C01-M1"
    assert terminal.kind == "duplicate_of_existing"
    assert terminal.duplicate_of == DuplicateIdentity(existing_note_id=41)


def test_card_dedupe_v2_does_not_cross_concept_boundaries() -> None:
    note = CardRecord(
        note_id=41,
        content_sha256="1" * 64,
        text="{{c1::Alpha}} beta",
        extra="",
        tags=(),
        deck_names=("AnKing",),
    )
    runner, context, passage_id = _dedupe_stage_fixture(
        _OrthogonalDedupeEmbedder(),
        cards=(note,),
    )
    _set_dedupe_existing(context, note.note_id, passage_id, concept_id="C02")

    product = asyncio.run(
        runner._card_dedupe_v2(
            context,
            {note.note_id: note},
            (
                _generated_dedupe_row("G01", "C01-M1", "Alpha beta", passage_id),
                _generated_dedupe_row("G02", "C01-M2", "Gamma delta", passage_id),
            ),
        )
    )

    assert [item["status"] for item in product.payload["resolutions"]] == [
        "generated",
        "generated",
    ]


def test_card_dedupe_v2_recovers_an_unmapped_existing_duplicate() -> None:
    note = CardRecord(
        note_id=41,
        content_sha256="1" * 64,
        text="{{c1::Mature B cells are absent}} in XLA",
        extra="",
        tags=(),
        deck_names=("AnKing",),
    )
    runner, context, passage_id = _dedupe_stage_fixture(
        _DedupeEmbedder([]),
        cards=(note,),
    )
    _set_dedupe_existing(context, note.note_id, passage_id, concept_id="")

    product = asyncio.run(
        runner._card_dedupe_v2(
            context,
            {note.note_id: note},
            (
                _generated_dedupe_row(
                    "G01",
                    "C02-M2",
                    "Mature B cells are absent in XLA",
                    passage_id,
                ),
            ),
        )
    )

    result = product.payload["resolutions"][0]
    assert result["status"] == "duplicate_of_existing"
    assert result["duplicate_of_existing_note_id"] == 41


def test_card_dedupe_v2_uses_pinned_existing_vectors_without_uploading_notes() -> None:
    note = CardRecord(
        note_id=41,
        content_sha256="1" * 64,
        text="Frozen existing note document text",
        extra="existing extra must not be embedded",
        tags=(),
        deck_names=("AnKing",),
    )
    embedder = _RecordingDedupeEmbedder()
    runner, context, passage_id = _dedupe_stage_fixture(embedder, cards=(note,))
    _set_dedupe_existing(context, note.note_id, passage_id)

    product = asyncio.run(
        runner._card_dedupe_v2(
            context,
            {note.note_id: note},
            (
                _generated_dedupe_row(
                    "G01",
                    "C01-M1",
                    "Generated proposal document text",
                    passage_id,
                ),
            ),
        )
    )

    assert product.payload["resolutions"][0]["status"] == "generated"
    assert embedder.document_calls == [("Generated proposal document text",)]
    assert "Frozen existing note document text" not in embedder.document_calls[0]
    assert "existing extra must not be embedded" not in embedder.document_calls[0]
    assert runner.semantic.calls == [((41,), "semantic-generation")]


def test_card_dedupe_v2_preserves_generated_duplicate_card_identity() -> None:
    runner, context, passage_id = _dedupe_stage_fixture(_DedupeEmbedder([]))
    first = _generated_dedupe_row("G01", "C01-M1", "Alpha beta", passage_id)
    second = _generated_dedupe_row("G02", "C01-M2", "{{c1::Alpha}} beta", passage_id)

    product = asyncio.run(runner._card_dedupe_v2(context, {}, (first, second)))

    result = product.payload["resolutions"][1]
    assert result["status"] == "duplicate_of_existing"
    assert result["duplicate_of_existing_note_id"] is None
    assert result["duplicate_of_generated_card_id"] == "G01"
    terminal = GeneratedFactResolution.model_validate(product.payload["terminal_resolutions"][1])
    assert terminal.fact_id == "C01-M2"
    assert terminal.kind == "duplicate_of_existing"
    assert terminal.duplicate_of == DuplicateIdentity(generated_card_id="G01")


def test_real_s8_generated_duplicate_target_survives_selection_into_s9() -> None:
    """S8's generated-card identity remains selected through S9 reconciliation."""
    runner, context, passage_id = _dedupe_stage_fixture(_OrthogonalDedupeEmbedder())
    first = _generated_dedupe_row("G01", "C01-M1", "{{c1::Alpha}} beta", passage_id)
    second = _generated_dedupe_row("G02", "C01-M2", "{{c1::Alpha}} beta", passage_id)
    other_generated = tuple(
        _generated_dedupe_row(
            f"G{index:02d}",
            f"C{index - 1:02d}-M1",
            f"{{{{c1::Independent {index}}}}}",
            passage_id,
        ).model_copy(update={"concept_id": f"C{index - 1:02d}"})
        for index in range(3, 12)
    )

    s8 = asyncio.run(runner._card_dedupe_v2(context, {}, (first, second, *other_generated)))
    deduped = tuple(
        GeneratedCardResolution.model_validate(row) for row in s8.payload["resolutions"]
    )
    source_index = CardCentricSourceIndex.model_validate(
        context.prior_payloads[CurationStage.SOURCE_INDEX]["source_index"]
    )
    selection = select_high_yield_v2(
        (),
        fast_classifications=(),
        ledger=_selection_ledger(10),
        source_index=source_index,
        generated_cards=deduped,
    )
    canonical = tuple(row for row in deduped if row.status == "generated")

    def as_s9_card(row: GeneratedCardResolution) -> GeneratedResolution:
        return GeneratedResolution(
            card_id=row.card_id,
            fact_id=row.fact_id,
            text=row.text,
            split=row.split,
            split_index=row.split_index,
        )

    snapshot = CardCentricReconciliationInput(
        pipeline_contract_version="card_centric_v2",
        concept_ids=tuple(f"C{index:02d}" for index in range(1, 11)),
        coverage={f"C{index:02d}": "covered" for index in range(1, 11)},
        required_fact_ids=tuple(row["fact_id"] for row in s8.payload["terminal_resolutions"]),
        uncovered_after_s5=tuple(f"C{index:02d}" for index in range(1, 11)),
        residual_ran_for=tuple(f"C{index:02d}" for index in range(1, 11)),
        generated_cards=tuple(as_s9_card(row) for row in canonical),
        raw_generated_cards=tuple(as_s9_card(row) for row in deduped),
        canonical_generated_cards=tuple(as_s9_card(row) for row in canonical),
        terminal_resolutions=tuple(
            GeneratedFactResolution.model_validate(row)
            for row in s8.payload["terminal_resolutions"]
        ),
        terminal_resolutions_provided=True,
        canonical_unresolved_fact_ids=(),
        unresolved_fact_ids=(),
        expected_scoped_nids=(),
        classifications=(),
        eligible_yes_nids=(),
        selected_nids=(),
        selected_generated_card_ids=selection.selected_generated_card_ids,
        generated_card_ids=tuple(row.card_id for row in canonical),
        source_passage_ids=(passage_id,),
        forbidden_cloze_targets=(),
        prompt_sync_stale=False,
        untagged_rate=0,
        mandatory_nids=(),
        mandatory_generated_card_ids=selection.mandatory_generated_card_ids,
        generated_concept_id_by_card_id={
            row.card_id: row.concept_id for row in canonical
        },
        selection_metadata=selection.selection_metadata,
        selection_order=tuple(item.identity for item in selection.selection_metadata),
        selected_count=len(selection.selected_generated_card_ids),
        below_warning_floor=selection.below_warning_floor,
    )

    report = reconcile_card_centric(snapshot)

    assert "G01" in selection.selected_generated_card_ids
    assert selection.mandatory_generated_card_ids == ("G01",)
    assert report.failed == ()
    assert "duplicate_coverage" in report.passed


def test_card_dedupe_v2_propagates_provider_and_vector_integrity_failures() -> None:
    existing_note = CardRecord(
        note_id=41,
        content_sha256="1" * 64,
        text="Gamma delta",
        extra="",
        tags=(),
        deck_names=("AnKing",),
    )
    failing_runner, failing_context, passage_id = _dedupe_stage_fixture(
        _FailingDedupeEmbedder(),
        cards=(existing_note,),
    )
    _set_dedupe_existing(failing_context, existing_note.note_id, passage_id)
    generated = (_generated_dedupe_row("G01", "C01-M1", "Alpha beta", passage_id),)
    with pytest.raises(VoyageEmbeddingError, match="retryable provider outage"):
        asyncio.run(failing_runner._card_dedupe_v2(failing_context, {41: existing_note}, generated))

    invalid_runner, invalid_context, passage_id = _dedupe_stage_fixture(
        _DedupeEmbedder([[1.0, 0.0], [1.0]]),
        cards=(existing_note,),
    )
    _set_dedupe_existing(invalid_context, existing_note.note_id, passage_id)
    with pytest.raises(SemanticDedupeIntegrityError, match="numeric rectangular matrix"):
        asyncio.run(
            invalid_runner._card_dedupe_v2(
                invalid_context,
                {41: existing_note},
                (_generated_dedupe_row("G01", "C01-M1", "Alpha beta", passage_id),),
            )
        )


def test_exhausted_semantic_dedupe_review_is_transportable_to_selection() -> None:
    runner, context, passage_id = _dedupe_stage_fixture(_DedupeEmbedder([]))
    generated = _generated_dedupe_row("G01", "C01-M1", "Alpha beta", passage_id)
    review = SemanticDedupeReview(
        card_id="G01",
        fact_id="C01-M1",
        lexical_candidates=(
            DedupeAdvisoryCandidate(
                card_id="G01",
                fact_id="C01-M1",
                identity=DuplicateIdentity(existing_note_id=41),
                lexical_score=0.5,
            ),
        ),
    )
    payload = record_exhausted_semantic_dedupe_review(
        {
            "resolutions": [generated.model_dump(mode="json")],
            "semantic_dedupe_reviews": [],
        },
        review,
    )
    context.prior_payloads[CurationStage.DEDUPE] = payload
    context.job.pipeline_contract_version = PipelineContractVersion.CARD_CENTRIC_V2
    context.prior_payloads[CurationStage.CARD_TAG_SCOPE] = {
        "scope": TagScopeResult(
            snapshot_id="dedupe-snapshot",
            filters_sha256="f" * 64,
            scoped_note_ids=(),
            unscoped_note_ids=(),
        ).model_dump(mode="json")
    }
    context.prior_payloads[CurationStage.CARD_LEDGER] = {
        "ledger": CardConceptLedger(
            lecture_entity_count=1,
            concepts=(
                CardConcept(
                    concept_id="C01",
                    canonical_statement="Alpha beta is a fixture fact.",
                    primary_entity="Alpha",
                    depth="deep",
                    emphasis_flag=False,
                    importance="high",
                ),
            ),
        ).model_dump(mode="json")
    }

    assert _card_dedupe_reviews(context) == (review,)
    assert payload["semantic_dedupe_reviews"] == [review.model_dump(mode="json")]
    assert payload["terminal_resolutions"] == []
    selection = asyncio.run(runner._card_selection(context))
    assert selection.payload["semantic_review_required_card_ids"] == ["G01"]
    assert selection.payload["selected_generated_card_ids"] == []
    assert selection.payload["semantic_dedupe_reviews"] == [review.model_dump(mode="json")]
    assert selection.payload["terminal_resolutions"] == []


def test_dedupe_terminal_resolutions_aggregate_split_cards_per_fact() -> None:
    _, _, passage_id = _dedupe_stage_fixture(_DedupeEmbedder([]))
    later = _generated_dedupe_row("G02", "C01-M1", "second split", passage_id).model_copy(
        update={"split": True, "split_index": 2}
    )
    earlier = _generated_dedupe_row("G01", "C01-M1", "first split", passage_id).model_copy(
        update={"split": True, "split_index": 1}
    )

    terminal = stages_module._dedupe_terminal_resolutions((later, earlier))

    assert len(terminal) == 1
    resolution = GeneratedFactResolution.model_validate(terminal[0])
    assert resolution.fact_id == "C01-M1"
    assert resolution.kind == "generated"
    assert resolution.generated_card_ids == ("G01", "G02")


def test_dedupe_terminal_resolutions_keep_unique_survivor_from_mixed_split() -> None:
    _, _, passage_id = _dedupe_stage_fixture(_DedupeEmbedder([]))
    survivor = _generated_dedupe_row(
        "G01", "C01-M1", "unique split", passage_id
    ).model_copy(update={"split": True, "split_index": 1})
    duplicate = _generated_dedupe_row(
        "G02", "C01-M1", "covered split", passage_id
    ).model_copy(
        update={
            "split": True,
            "split_index": 2,
            "status": "duplicate_of_existing",
            "duplicate_of_existing_note_id": 41,
            "reason": "semantic duplicate",
        }
    )

    terminal = stages_module._dedupe_terminal_resolutions((survivor, duplicate))

    assert len(terminal) == 1
    resolution = GeneratedFactResolution.model_validate(terminal[0])
    assert resolution.fact_id == "C01-M1"
    assert resolution.kind == "generated"
    assert resolution.generated_card_ids == ("G01",)


def test_dedupe_terminal_resolutions_reject_mixed_fact_states() -> None:
    _, _, passage_id = _dedupe_stage_fixture(_DedupeEmbedder([]))
    generated = _generated_dedupe_row("G01", "C01-M1", "Alpha beta", passage_id)
    unresolved = GeneratedCardResolution(
        card_id="G02",
        concept_id="C01",
        fact_id="C01-M1",
        source_passage_ids=(passage_id,),
        status="unresolved",
        reason="generation could not resolve the fact",
    )

    with pytest.raises(PinnedInputChanged, match="conflicting terminal states"):
        stages_module._dedupe_terminal_resolutions((generated, unresolved))


def test_card_selection_v2_persists_exact_pending_overflow_metadata_and_flags() -> None:
    runner, context, passage_id = _selection_stage_context(
        ledger=_selection_ledger(72, high_through=71),
        generated=(),
    )
    generated = tuple(
        item.model_copy(update={"source_passage_ids": (passage_id,)})
        for item in _selection_generated_rows(72, passage_id)
    )
    context.prior_payloads[CurationStage.DEDUPE]["resolutions"] = [
        item.model_dump(mode="json") for item in reversed(generated)
    ]

    product = asyncio.run(runner._card_selection(context))

    assert product.payload["selected_count"] == 71
    assert product.payload["selected_generated_card_ids"] == [
        f"G{index:02d}" for index in range(1, 72)
    ]
    assert product.payload["excluded_generated_card_ids"] == ["G72"]
    assert product.payload["below_warning_floor"] is False
    assert product.payload["overflow_acknowledgement"] is None
    metadata = product.payload["selection_metadata"]
    assert product.payload["selection_order"] == [item["identity"] for item in metadata]
    assert [item["selected_position"] for item in metadata] == list(range(1, 72))
    assert [item["marginal_value_reason"] for item in metadata[65:70]] == [
        MarginalValueReason.ONLY_VALID_REQUIRED_FACT.value
    ] * 5
    assert metadata[-1]["mandatory"] is True
    assert metadata[-1]["manual_acknowledgement_required"] is True
    assert metadata[-1]["overflow_reason"]
    selected_gap_cards = {card.card_id: card for card in product.gap_cards if card.selected}
    assert set(selected_gap_cards) == {f"G{index:02d}" for index in range(1, 72)}
    assert next(card for card in product.gap_cards if card.card_id == "G72").selected is False
    assert selected_gap_cards["G01"].provenance["selection"]["selected_position"] == 1


def test_card_selection_v2_persists_unique_required_existing_card_after_65() -> None:
    source_ledger = _selection_ledger(65)
    ledger = CardConceptLedger(
        lecture_entity_count=67,
        concepts=(
            *source_ledger.concepts,
            CardConcept(
                concept_id="C66",
                canonical_statement="Required high-value existing fact.",
                primary_entity="Required existing",
                depth="deep",
                emphasis_flag=False,
                importance="high",
            ),
            CardConcept(
                concept_id="C67",
                canonical_statement="Low-value existing fact.",
                primary_entity="Low existing",
                depth="surface",
                emphasis_flag=False,
                importance="low",
            ),
        ),
    )
    cards = (
        CardRecord(
            note_id=66,
            content_sha256="6" * 64,
            text="Required high-value existing fact",
            extra="",
            tags=(),
            deck_names=("AnKing",),
        ),
        CardRecord(
            note_id=67,
            content_sha256="7" * 64,
            text="Low-value existing fact",
            extra="",
            tags=(),
            deck_names=("AnKing",),
        ),
    )
    classifications = (
        CardClassification(
            note_id=66,
            verdict="YES",
            primary_subject="fixture",
            reason="unique required high coverage",
            covered_concept_ids=("C66",),
            supporting_passage_ids=(),
        ),
        CardClassification(
            note_id=67,
            verdict="YES",
            primary_subject="fixture",
            reason="unique low-value coverage",
            covered_concept_ids=("C67",),
            supporting_passage_ids=(),
        ),
    )
    runner, context, passage_id = _dedupe_stage_fixture(
        _DedupeEmbedder([]),
        existing=classifications,
        cards=cards,
    )
    context.job.pipeline_contract_version = PipelineContractVersion.CARD_CENTRIC_V2
    context.prior_payloads[CurationStage.CARD_TAG_SCOPE] = {
        "scope": TagScopeResult(
            snapshot_id="dedupe-snapshot",
            filters_sha256="f" * 64,
            scoped_note_ids=(66, 67),
            unscoped_note_ids=(),
        ).model_dump(mode="json")
    }
    context.prior_payloads[CurationStage.CARD_LEDGER] = {
        "ledger": ledger.model_dump(mode="json")
    }
    generated = _selection_generated_rows(65, passage_id)
    context.prior_payloads[CurationStage.DEDUPE] = {
        "resolutions": [item.model_dump(mode="json") for item in generated],
        "semantic_dedupe_reviews": [],
    }
    context.prior_payloads[CurationStage.CARD_CLASSIFY]["classifier"]["results"] = [
        item.model_copy(update={"supporting_passage_ids": (passage_id,)}).model_dump(mode="json")
        for item in classifications
    ]

    product = asyncio.run(runner._card_selection(context))

    assert product.payload["selected_count"] == 66
    assert product.payload["selected_existing_note_ids"] == [66]
    assert product.payload["excluded_existing_note_ids"] == [67]
    marginal = product.payload["selection_metadata"][-1]
    assert marginal["identity"] == "existing:66"
    assert marginal["selected_position"] == 66
    assert marginal["tier"] == SelectionTier.T3.value
    assert marginal["marginal_value_reason"] == MarginalValueReason.ONLY_VALID_REQUIRED_FACT.value
    candidates = {candidate.note_id: candidate for candidate in product.candidates}
    assert candidates[66].selected is True
    assert candidates[66].provenance["selection"] == marginal
    assert candidates[67].selected is False


def test_card_selection_v2_preserves_a_supplied_overflow_acknowledgement(monkeypatch) -> None:
    runner, context, passage_id = _selection_stage_context(
        ledger=_selection_ledger(71, high_through=71),
        generated=(),
    )
    generated = tuple(
        item.model_copy(update={"source_passage_ids": (passage_id,)})
        for item in _selection_generated_rows(71, passage_id)
    )
    context.prior_payloads[CurationStage.DEDUPE]["resolutions"] = [
        item.model_dump(mode="json") for item in generated
    ]
    original = stages_module.select_high_yield_v2

    def signed_selection(*args, **kwargs):
        result = original(*args, **kwargs)
        return result.model_copy(
            update={
                "overflow_acknowledgement": CanonicalJsonObject.from_mapping(
                    {"signature": "fixture", "token": "fixture"}
                )
            }
        )

    monkeypatch.setattr(stages_module, "select_high_yield_v2", signed_selection)
    product = asyncio.run(runner._card_selection(context))

    assert product.payload["overflow_acknowledgement"] == {
        "canonical_json": '{"signature":"fixture","token":"fixture"}'
    }


def test_card_selection_v2_keeps_short_decks_and_fallbacks_unselected() -> None:
    fallback_card = CardRecord(
        note_id=1,
        content_sha256="1" * 64,
        text="Fallback fixture",
        extra="",
        tags=(),
        deck_names=("AnKing",),
    )
    runner, context, _ = _dedupe_stage_fixture(_DedupeEmbedder([]), cards=(fallback_card,))
    context.job.pipeline_contract_version = PipelineContractVersion.CARD_CENTRIC_V2
    context.prior_payloads[CurationStage.CARD_TAG_SCOPE] = {
        "scope": TagScopeResult(
            snapshot_id="dedupe-snapshot",
            filters_sha256="f" * 64,
            scoped_note_ids=(fallback_card.note_id,),
            unscoped_note_ids=(),
        ).model_dump(mode="json")
    }
    context.prior_payloads[CurationStage.CARD_LEDGER] = {
        "ledger": _selection_ledger(1).model_dump(mode="json")
    }
    context.prior_payloads[CurationStage.DEDUPE] = {
        "resolutions": [],
        "semantic_dedupe_reviews": [],
    }
    context.prior_payloads[CurationStage.CARD_FAST_CLASSIFY]["fallback_note_ids"] = [
        fallback_card.note_id
    ]

    product = asyncio.run(runner._card_selection(context))

    assert product.payload["selected_count"] == 0
    assert product.payload["below_warning_floor"] is True
    assert product.payload["selection_metadata"] == []
    assert product.payload["excluded_existing_note_ids"] == [fallback_card.note_id]
    assert product.candidates[0].selected is False


def test_card_reconciliation_constructs_the_full_v2_s9_snapshot() -> None:
    class SnapshotRepository:
        def __init__(self) -> None:
            self.acknowledgement_calls: list[dict[str, object]] = []
            self.history_calls: list[object] = []

        def validate_card_centric_overflow_acknowledgement(
            self,
            job_id: object,
            **kwargs: object,
        ) -> bool:
            self.acknowledgement_calls.append({"job_id": job_id, **kwargs})
            return True

        def card_centric_yes_rate_history(self, job_id: object) -> tuple[float, ...]:
            self.history_calls.append(job_id)
            return (0.99,)

    cards = tuple(_card_record(note_id, ()) for note_id in range(1, 11))
    runner, context, passage_id = _dedupe_stage_fixture(_DedupeEmbedder([]), cards=cards)
    repository = SnapshotRepository()
    runner.repository = repository
    context.job.pipeline_contract_version = PipelineContractVersion.CARD_CENTRIC_V2
    context.job.id = uuid4()
    context.job.review_revision = 7
    history_entry = A11HistoryEntry(
        job_id=uuid4(),
        review_revision=3,
        yes_rate=0.25,
        reviewed_at=datetime(2026, 8, 8, tzinfo=UTC),
    )
    history_payload = [history_entry.model_dump(mode="json")]
    context.replay_inputs = {
        "a11_history": A11HistorySnapshot(
            entries=(history_entry,),
            snapshot_sha256=hashlib.sha256(
                json.dumps(history_payload, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest(),
        ).model_dump(mode="json")
    }
    ledger = CardConceptLedger(
        lecture_entity_count=4,
        forbidden_cloze_targets=("global-only",),
        concepts=(
            CardConcept(
                concept_id="C01",
                canonical_statement="First generated fact.",
                primary_entity="first",
                depth="deep",
                emphasis_flag=False,
                importance="high",
                forbidden_cloze_targets_by_fact=(("alpha",),),
            ),
            CardConcept(
                concept_id="C02",
                canonical_statement="Second duplicate fact.",
                primary_entity="second",
                depth="medium",
                emphasis_flag=False,
                importance="medium",
            ),
            CardConcept(
                concept_id="C03",
                canonical_statement="Third unresolved fact.",
                primary_entity="third",
                depth="medium",
                emphasis_flag=False,
                importance="medium",
                forbidden_cloze_targets_by_fact=(("gamma",),),
            ),
            CardConcept(
                concept_id="C04",
                canonical_statement="Fourth review fact.",
                primary_entity="fourth",
                depth="medium",
                emphasis_flag=False,
                importance="medium",
                forbidden_cloze_targets_by_fact=(("delta",),),
            ),
        ),
    )
    classifications = tuple(
        CardClassification(
            note_id=note_id,
            verdict="YES",
            primary_subject="fixture",
            reason="grounded existing candidate",
            supporting_passage_ids=(passage_id,),
        )
        for note_id in range(1, 11)
    )
    classifier = ClassifierResult.model_validate(
        context.prior_payloads[CurationStage.CARD_CLASSIFY]["classifier"]
    ).model_copy(update={"results": classifications})
    raw = (
        _generated_dedupe_row("G1", "C01-M1", "{{c1::first split}}", passage_id).model_copy(
            update={"split": True, "split_index": 1}
        ),
        _generated_dedupe_row("G2", "C01-M1", "{{c1::second split}}", passage_id).model_copy(
            update={"split": True, "split_index": 2}
        ),
        _generated_dedupe_row("G3", "C02-M1", "{{c1::deduplicated}}", passage_id),
        GeneratedCardResolution(
            card_id="G4",
            concept_id="C03",
            fact_id="C03-M1",
            source_passage_ids=(passage_id,),
            status="unresolved",
            reason="No grounded atomic cloze.",
        ),
        _generated_dedupe_row("G5", "C04-M1", "{{c1::manual review}}", passage_id),
    )
    deduped = (
        raw[0],
        raw[1],
        raw[2].model_copy(
            update={
                "status": "duplicate_of_existing",
                "duplicate_of_existing_note_id": 99,
                "reason": "Exact existing duplicate.",
            }
        ),
        raw[3],
        raw[4],
    )
    semantic_review = SemanticDedupeReview(
        card_id="G5",
        fact_id="C04-M1",
        lexical_candidates=(
            DedupeAdvisoryCandidate(
                card_id="G5",
                fact_id="C04-M1",
                identity=DuplicateIdentity(existing_note_id=55),
                lexical_score=0.8,
            ),
        ),
    )
    selected_existing = tuple(range(1, 11))
    selection_metadata = tuple(
        SelectionMetadata(
            identity=identity,
            selected_position=position,
            tier=SelectionTier.T1,
            evidence_quality=EvidenceQuality.PRIMARY_SOURCE,
        )
        for position, identity in enumerate(
            [*(f"existing:{note_id}" for note_id in selected_existing), "generated:G1"],
            start=1,
        )
    )
    acknowledgement = CanonicalJsonObject.from_mapping(
        {"token": "fixture-token", "selection_digest": "fixture", "signature": "fixture"}
    )
    selection_result = QualitySelectionResult(
        existing_candidate_note_ids=selected_existing,
        generated_candidate_card_ids=("G1", "G2", "G5"),
        selected_existing_note_ids=selected_existing,
        selected_generated_card_ids=("G1",),
        excluded_existing_note_ids=(),
        excluded_generated_card_ids=("G2", "G5"),
        selection_metadata=selection_metadata,
        below_warning_floor=True,
        target=65,
        cap=70,
        minimum_target=60,
        semantic_review_required_card_ids=("G5",),
        overflow_acknowledgement=acknowledgement,
    )
    selection_payload = selection_result.model_dump(mode="json")
    selection_payload.update(
        {
            "selected_count": 11,
            "selection_order": [item.identity for item in selection_metadata],
            "terminal_resolutions": stages_module._dedupe_terminal_resolutions(
                deduped,
                (semantic_review,),
            ),
        }
    )
    context.prior_payloads.update(
        {
            CurationStage.CARD_LEDGER: {"ledger": ledger.model_dump(mode="json")},
            CurationStage.CARD_CLASSIFY: {"classifier": classifier.model_dump(mode="json")},
            CurationStage.CARD_COVERAGE: {
                "coverage": {
                    concept.concept_id: {"status": "uncovered", "evidence": []}
                    for concept in ledger.concepts
                }
            },
            CurationStage.CARD_RESIDUAL: {
                "uncovered_concept_ids": [concept.concept_id for concept in ledger.concepts]
            },
            CurationStage.CARD_TAG_SCOPE: {
                "scope": TagScopeResult(
                    snapshot_id="dedupe-snapshot",
                    filters_sha256="f" * 64,
                    scoped_note_ids=selected_existing,
                    unscoped_note_ids=(),
                ).model_dump(mode="json")
            },
            CurationStage.CARD_GAP_FILL: {
                "resolutions": [item.model_dump(mode="json") for item in raw]
            },
            CurationStage.DEDUPE: {
                "resolutions": [item.model_dump(mode="json") for item in deduped],
                "semantic_dedupe_reviews": [semantic_review.model_dump(mode="json")],
            },
            CurationStage.CARD_SELECTION: selection_payload,
            CurationStage.PREFLIGHT: {"prompt_sync_stale": False},
        }
    )
    context.prior_payloads[CurationStage.SOURCE_INDEX]["census"] = build_snapshot_census(
        cards,
        deck_allowlist=("AnKing",),
        scope_tokens=("fixture",),
        snapshot_id="dedupe-snapshot",
    ).model_dump(mode="json")

    product = asyncio.run(runner._card_reconciliation(context))

    snapshot = product.payload["snapshot"]
    assert [row["card_id"] for row in snapshot["raw_generated_cards"]] == [
        "G1",
        "G2",
        "G3",
        "G5",
    ]
    raw_identity_and_split = [
        (row["card_id"], row.get("split_index")) for row in snapshot["raw_generated_cards"]
    ]
    assert raw_identity_and_split == [
        ("G1", 1),
        ("G2", 2),
        ("G3", None),
        ("G5", None),
    ]
    assert [row["card_id"] for row in snapshot["canonical_generated_cards"]] == ["G1", "G2", "G5"]
    assert [row["card_id"] for row in snapshot["generated_cards"]] == ["G1"]
    terminal = {row["fact_id"]: row for row in snapshot["terminal_resolutions"]}
    assert terminal["C01-M1"]["generated_card_ids"] == ["G1", "G2"]
    assert terminal["C02-M1"]["duplicate_of"]["existing_note_id"] == 99
    assert terminal["C02-M1"]["duplicate_of"]["generated_card_id"] is None
    assert terminal["C03-M1"]["unresolved_reason"] == "No grounded atomic cloze."
    assert "C04-M1" not in terminal
    assert snapshot["forbidden_cloze_targets_by_fact"] == {
        "C01-M1": ["alpha"],
        "C02-M1": [],
        "C03-M1": ["gamma"],
        "C04-M1": ["delta"],
    }
    assert "global-only" not in {
        target
        for targets in snapshot["forbidden_cloze_targets_by_fact"].values()
        for target in targets
    }
    assert snapshot["selection_metadata"] == [
        item.model_dump(mode="json") for item in selection_metadata
    ]
    assert snapshot["selection_order"] == [item.identity for item in selection_metadata]
    assert snapshot["selected_count"] == 11
    assert snapshot["below_warning_floor"] is True
    assert snapshot["semantic_review_required_card_ids"] == ["G5"]
    assert snapshot["historical_yes_rates"] == [0.25]
    assert repository.history_calls == []
    assert snapshot["source_passage_ids"] == [passage_id]
    assert snapshot["classifications"] == [
        {"nid": note_id, "verdict": "keep"} for note_id in selected_existing
    ]
    assert snapshot["uncovered_after_s5"] == ["C01", "C02", "C03", "C04"]
    assert snapshot["residual_ran_for"] == ["C01", "C02", "C03", "C04"]
    assert snapshot["coverage"] == {
        "C01": "covered",
        "C02": "uncovered",
        "C03": "intentional_gap",
        "C04": "uncovered",
    }
    assert snapshot["overflow_acknowledgement"] == acknowledgement.as_dict()
    assert repository.acknowledgement_calls == [
        {
            "job_id": context.job.id,
            "review_revision": 7,
            "selected_note_ids": selected_existing,
            "selected_generated_ids": ("G1",),
            "cap": 70,
            "document": acknowledgement.as_dict(),
        }
    ]


class ReadyRuntime:
    async def ensure_running(self) -> SimpleNamespace:
        return SimpleNamespace(
            reachable=True,
            ankiconnect_version=6,
            active_profile="Acceptance",
            collection_accessible=True,
            sync_available=True,
            blocking_reason=None,
        )


@pytest.mark.parametrize(
    "contract_version",
    [PipelineContractVersion.CARD_CENTRIC_V1, PipelineContractVersion.CARD_CENTRIC_V2],
)
def test_card_centric_source_index_build_keeps_event_loop_responsive(
    contract_version: PipelineContractVersion,
) -> None:
    entered = threading.Event()
    release = threading.Event()
    probe_completed = threading.Event()
    probe_done = asyncio.Event()
    coordination: dict[str, bool] = {}
    list_notes_thread_id: list[int] = []
    probe_ran_while_scan_blocked: list[bool] = []
    extraction_calls: list[tuple[tuple[int, ...], int | None]] = []
    loop_thread_id = threading.get_ident()
    passages = (
        SourcePassage.create(
            revision_id=7,
            lecture_id=12,
            artifact_id="summary-7",
            source_kind=SourceKind.SUMMARY,
            locator="summary:1",
            text="Summary evidence.",
        ),
        SourcePassage.create(
            revision_id=8,
            lecture_id=12,
            artifact_id="transcript-8",
            source_kind=SourceKind.TRANSCRIPT,
            locator="00:00",
            text="Transcript evidence.",
        ),
        SourcePassage.create(
            revision_id=9,
            lecture_id=12,
            artifact_id="vision-9",
            source_kind=SourceKind.VISION,
            locator="slide:1",
            text="Vision-only evidence.",
        ),
    )

    def note(
        note_id: int,
        *,
        tags: tuple[str, ...],
        deck_names: tuple[str, ...],
    ) -> NormalizedNote:
        return NormalizedNote(
            note_id=note_id,
            model_name="AnKingOverhaul",
            text=f"Card {note_id}",
            extra="Fixture extra.",
            raw_fields={"Text": f"Card {note_id}"},
            tags=tags,
            card_ids=(note_id + 1000,),
            media=(),
            token_signature=f"card {note_id}",
            content_sha256=f"{note_id:064x}",
            deck_names=deck_names,
        )

    def list_notes() -> list[NormalizedNote]:
        list_notes_thread_id.append(threading.get_ident())
        entered.set()
        assert release.wait(timeout=2)
        return [
            note(20, tags=("#AK_Step::Heme",), deck_names=("AnKing",)),
            note(10, tags=("#Pathoma",), deck_names=("AnKing",)),
            note(30, tags=("#AK_Step::Heme",), deck_names=("Other",)),
        ]

    def extract(
        revision_ids: tuple[int, ...],
        *,
        summary_outline_id: int | None,
    ) -> tuple[SourcePassage, ...]:
        extraction_calls.append((revision_ids, summary_outline_id))
        return passages

    async def probe() -> None:
        probe_ran_while_scan_blocked.append(not release.is_set())
        probe_completed.set()
        probe_done.set()

    async def exercise() -> StageProduct:
        loop = asyncio.get_running_loop()
        runner = CurationServicesRunner.__new__(CurationServicesRunner)
        runner.source_extractor = SimpleNamespace(extract=extract)
        runner.companion = SimpleNamespace(list_notes=list_notes)
        context = SimpleNamespace(
            job=SimpleNamespace(
                source_revision_ids=(7, 8, 9),
                summary_outline_id=99,
                lecture_id=12,
                pipeline_contract_version=contract_version,
                index_snapshot_id="index-snapshot",
                source_revision_hashes={7: "a" * 64, 8: "b" * 64, 9: "c" * 64},
                summary_outline_sha256="d" * 64,
                deck_allowlist=("AnKing",),
                tag_allowlist=("heme",),
                companion_generation="companion-snapshot",
            )
        )
        source_index_task = asyncio.create_task(runner._source_index(context))

        def coordinate() -> None:
            coordination["scan_entered"] = entered.wait(timeout=2)
            if not coordination["scan_entered"]:
                return
            loop.call_soon_threadsafe(lambda: asyncio.create_task(probe()))
            if not probe_completed.wait(timeout=2):
                release.set()

        coordinator = threading.Thread(target=coordinate)
        coordinator.start()
        try:
            await asyncio.wait_for(probe_done.wait(), timeout=3)
            assert coordination["scan_entered"]
            assert probe_ran_while_scan_blocked == [True]
            assert list_notes_thread_id and list_notes_thread_id[0] != loop_thread_id
            release.set()
            return await source_index_task
        finally:
            release.set()
            if not source_index_task.done():
                await asyncio.wait_for(source_index_task, timeout=2)
            coordinator.join(timeout=2)
            assert not coordinator.is_alive()

    product = asyncio.run(exercise())

    assert extraction_calls == [((7, 8, 9), 99)]
    assert product.kind == "card_centric_source_index"
    assert [card["note_id"] for card in product.payload["cards"]] == [20, 10, 30]
    assert [passage["source_kind"] for passage in product.payload["source_index"]["passages"]] == [
        "summary",
        "transcript",
    ]
    assert product.payload["source_index"]["snapshot_id"] == "index-snapshot"
    census = product.payload["census"]
    assert {
        key: census[key]
        for key in (
            "snapshot_id",
            "denominator_count",
            "tagged_count",
            "other_system_tagged_count",
            "untagged_count",
            "deck_excluded_count",
            "excluded_count",
            "mapping",
        )
    } == {
        "snapshot_id": "companion-snapshot",
        "denominator_count": 2,
        "tagged_count": 1,
        "other_system_tagged_count": 0,
        "untagged_count": 1,
        "deck_excluded_count": 1,
        "excluded_count": 1,
        "mapping": {
            "10": "untagged",
            "20": "target_tagged",
            "30": "deck_excluded",
        },
    }


def _card_record(note_id: int, tags: tuple[str, ...]) -> CardRecord:
    return CardRecord(
        note_id=note_id,
        content_sha256=f"{note_id:064x}",
        text=f"Card {note_id}",
        extra="",
        tags=tags,
        deck_names=("AnKing",),
    )


def _tag_scope_product(cards: tuple[CardRecord, ...]):
    census = build_snapshot_census(
        cards,
        deck_allowlist=("AnKing",),
        scope_tokens=("heme",),
        snapshot_id="snapshot-1",
    )
    runner = CurationServicesRunner.__new__(CurationServicesRunner)
    context = SimpleNamespace(
        job=SimpleNamespace(tag_allowlist=("heme",)),
        prior_payloads={
            CurationStage.SOURCE_INDEX: {
                "cards": [card.model_dump(mode="json") for card in cards],
                "census": census.model_dump(mode="json"),
            }
        },
    )
    return asyncio.run(runner._card_tag_scope(context))


def test_card_tag_scope_continues_with_gaps_only_residual_at_warning_threshold() -> None:
    cards = tuple(
        _card_record(note_id, ("#AK_Step::Heme",) if note_id <= 97 else ())
        for note_id in range(1, 101)
    )

    product = _tag_scope_product(cards)

    assert product.blocking_error is None
    assert product.payload["residual_mode"] == "gaps_only"


def test_card_tag_scope_uses_unconditional_residual_at_fifteen_percent() -> None:
    cards = tuple(
        _card_record(note_id, ("#AK_Step::Heme",) if note_id <= 17 else ())
        for note_id in range(1, 21)
    )
    census = build_snapshot_census(
        cards,
        deck_allowlist=("AnKing",),
        scope_tokens=("heme",),
        snapshot_id="snapshot-1",
    )
    product = _tag_scope_product(cards)

    assert product.blocking_error is None
    assert census.trust.untagged_rate == 0.15
    assert product.payload["scope"]["scoped_note_ids"] == list(range(1, 18))
    assert product.payload["residual_mode"] == "all_concepts"


def test_card_residual_targets_every_concept_only_for_unconditional_mode() -> None:
    ledger = CardConceptLedger(
        lecture_entity_count=2,
        concepts=(
            CardConcept(
                concept_id="C01",
                canonical_statement="Covered concept",
                primary_entity="Covered",
                depth="surface",
                emphasis_flag=False,
                importance="low",
            ),
            CardConcept(
                concept_id="C02",
                canonical_statement="Missing concept",
                primary_entity="Missing",
                depth="deep",
                emphasis_flag=False,
                importance="high",
            ),
        ),
    )
    coverage = {
        "C01": {"status": "covered", "evidence": [{"note_id": 1}]},
        "C02": {"status": "uncovered", "evidence": []},
    }

    assert [item.concept_id for item in _card_residual_targets(ledger, coverage, "gaps_only")] == [
        "C02"
    ]
    assert [
        item.concept_id for item in _card_residual_targets(ledger, coverage, "all_concepts")
    ] == ["C01", "C02"]


def test_v2_s4c_replaces_needs_review_and_s6_materializes_residual_candidates() -> None:
    passage = SourcePassage.create(
        revision_id=7,
        lecture_id=12,
        artifact_id="slides-7",
        source_kind=SourceKind.SLIDE,
        locator="slide:1",
        text="Evidence for the fixture concept.",
        slide_number=1,
    )
    source = build_source_index(
        (passage,), snapshot_id="snapshot-1", source_revision_hashes={7: "a" * 64}
    )
    cards = tuple(_card_record(note_id, ("#AK_Step::Heme",)) for note_id in (1, 2, 3))
    scope = TagScopeResult(
        snapshot_id="snapshot-1",
        filters_sha256="b" * 64,
        scoped_note_ids=(1, 2),
        unscoped_note_ids=(3,),
    )
    context = SimpleNamespace(
        prior_payloads={
            CurationStage.SOURCE_INDEX: {"cards": [card.model_dump(mode="json") for card in cards]},
            CurationStage.CARD_TAG_SCOPE: {"scope": scope.model_dump(mode="json")},
        }
    )
    thorough_and_residual = (
        CardClassification(
            note_id=1,
            verdict="YES",
            primary_subject="fixture",
            reason="S4c terminal",
            covered_concept_ids=("C01",),
            supporting_passage_ids=(source.passages[0].passage_id,),
        ),
        CardClassification(
            note_id=3,
            verdict="YES",
            primary_subject="fixture",
            reason="S6 residual",
            covered_concept_ids=("C01",),
            supporting_passage_ids=(source.passages[0].passage_id,),
        ),
    )
    fast = (
        FastCardClassification(note_id=1, verdict="NEEDS_REVIEW", reason="route to S4c"),
        FastCardClassification(note_id=2, verdict="LIKELY_NO", reason="not taught"),
    )

    candidates = _v2_card_candidates(context, thorough_and_residual, fast, (), {1, 3}, source)
    audit_rows = _v2_reconciliation_classifications(thorough_and_residual, fast, (), scope)

    assert [candidate.note_id for candidate in candidates] == [1, 2, 3]
    assert candidates[-1].selected is True
    assert candidates[-1].provenance["card_centric_v2"]["classification_kind"] == "residual"
    assert [(row.nid, row.verdict) for row in audit_rows] == [(1, "keep"), (2, "drop")]


def test_v2_s6_result_replaces_prefilter_fallback_before_t6() -> None:
    residual = CardClassification(
        note_id=9, verdict="MAYBE", primary_subject="fixture", reason="S6 terminal"
    )

    assert _effective_v2_fallback_note_ids((9,), (residual,)) == ()


def test_v2_fast_classifier_receives_ledger_definitions_for_multi_concept_grounding(
    tmp_path: Path,
) -> None:
    passage = SourcePassage.create(
        revision_id=7,
        lecture_id=12,
        artifact_id="slides-7",
        source_kind=SourceKind.SLIDE,
        locator="slide:1",
        text="Factor deficiency prolongs the assay and mixing corrects it.",
        slide_number=1,
    )
    source = build_source_index(
        (passage,), snapshot_id="snapshot-1", source_revision_hashes={7: "a" * 64}
    )
    card = CardRecord(
        note_id=1,
        content_sha256="1" * 64,
        text="Factor deficiency prolongs the assay; mixing corrects it.",
        extra="",
        tags=("#AK_Step::Heme",),
        deck_names=("AnKing",),
    )
    ledger = CardConceptLedger(
        lecture_entity_count=2,
        concepts=(
            CardConcept(
                concept_id="C01",
                canonical_statement="Factor deficiency prolongs the assay.",
                primary_entity="factor deficiency",
                aliases=("clotting factor deficiency",),
                depth="deep",
                emphasis_flag=False,
                importance="high",
            ),
            CardConcept(
                concept_id="C02",
                canonical_statement="Correction on mixing supports a deficiency.",
                primary_entity="mixing study correction",
                aliases=("mixing correction",),
                depth="medium",
                emphasis_flag=False,
                importance="medium",
            ),
        ),
    )
    prefilter = SemanticPreFilterResult(
        pre_filtered_note_ids=(1,),
        pre_excluded_note_ids=(),
        threshold=0.42,
        similarity_stats={"min": 0.9, "max": 0.9, "mean": 0.9, "median": 0.9},
    )
    scope = TagScopeResult(
        snapshot_id="snapshot-1",
        filters_sha256="b" * 64,
        scoped_note_ids=(1,),
        unscoped_note_ids=(),
    )

    class CapturingStructuredService:
        payload: dict[str, object] | None = None
        instruction: str | None = None
        invalid_concept_id = False

        def generate_json(self, instruction, input_text, **kwargs):
            self.instruction = instruction
            self.payload = json.loads(input_text)
            assert kwargs["output_model"] is FastClassificationResult
            value = FastClassificationResult(
                results=(
                    FastCardClassification(
                        note_id=1,
                        verdict="LIKELY_YES",
                        grounded_concept_ids=(
                            ("C99",) if self.invalid_concept_id else ("C01", "C02")
                        ),
                        supporting_passage_ids=(source.passages[0].passage_id,),
                        reason="Both supplied concept definitions are supported.",
                    ),
                )
            )
            return StructuredJSONResult(
                value=value,
                raw_text=value.model_dump_json(),
                provider=kwargs["provider"],
                model=kwargs["model"],
                request_id="fast-v2-request",
                input_tokens=30,
                output_tokens=15,
                cost_microusd=8,
            )

    structured = CapturingStructuredService()
    runner = CurationServicesRunner.__new__(CurationServicesRunner)
    runner.structured = structured
    runner.prompts = AnkiPromptCatalogService()
    fast_prompt = AnkiPromptLibrary(runner.prompts.bundled_directory).load(
        "card-centric-fast-classifier"
    )
    # Simulate a catalog mutation after S0. S4b must use the frozen content,
    # and would fail here if it attempted to reread this live replacement.
    runner.prompts = AnkiPromptCatalogService(bundled_directory=tmp_path)
    context = SimpleNamespace(
        job=SimpleNamespace(
            pipeline_contract_version=PipelineContractVersion.CARD_CENTRIC_V2,
            resolved_model_config=SimpleNamespace(
                fast_classify_s4b=SimpleNamespace(provider="openai", model="gpt-4o-mini"),
                canonical_document=lambda: {"fast_classify_s4b": "gpt-4o-mini"},
            ),
        ),
        prior_payloads={
            CurationStage.PREFLIGHT: {
                "prompt_snapshot": [
                    {
                        "id": fast_prompt.metadata.id,
                        "version": fast_prompt.metadata.version,
                        "prompt_hash": fast_prompt.prompt_hash,
                        "content": fast_prompt.content,
                        "metadata": fast_prompt.metadata.model_dump(mode="json", by_alias=True),
                    }
                ]
            },
            CurationStage.SOURCE_INDEX: {
                "source_index": source.model_dump(mode="json"),
                "cards": [card.model_dump(mode="json")],
            },
            CurationStage.CARD_LEDGER: {"ledger": ledger.model_dump(mode="json")},
            CurationStage.CARD_PREFILTER: prefilter.model_dump(mode="json"),
            CurationStage.CARD_TAG_SCOPE: {"scope": scope.model_dump(mode="json")},
        },
    )

    product = asyncio.run(runner._card_fast_classify(context))

    assert structured.payload is not None
    assert structured.instruction == fast_prompt.content
    assert structured.payload["allowed_concept_ids"] == ["C01", "C02"]
    assert structured.payload["concept_definitions"] == [
        {
            "concept_id": "C01",
            "canonical_statement": "Factor deficiency prolongs the assay.",
            "primary_entity": "factor deficiency",
            "aliases": ["clotting factor deficiency"],
        },
        {
            "concept_id": "C02",
            "canonical_statement": "Correction on mixing supports a deficiency.",
            "primary_entity": "mixing study correction",
            "aliases": ["mixing correction"],
        },
    ]
    assert product.payload["fast_classifier"]["results"][0]["grounded_concept_ids"] == [
        "C01",
        "C02",
    ]
    structured.invalid_concept_id = True
    degraded = asyncio.run(runner._card_fast_classify(context))
    assert degraded.payload["fast_classifier"]["results"] == [
        {
            "contract_version": 1,
            "note_id": 1,
            "verdict": "NEEDS_REVIEW",
            "grounded_concept_ids": [],
            "supporting_passage_ids": [],
            "flags": [],
            "reason": "S4b degraded batch: invented_concept_id",
        }
    ]


def _fast_failure_harness(tmp_path: Path, mode: str):
    passage = SourcePassage.create(
        revision_id=7,
        lecture_id=12,
        artifact_id="slides-7",
        source_kind=SourceKind.SLIDE,
        locator="slide:1",
        text="Factor deficiency prolongs the assay.",
        slide_number=1,
    )
    source = build_source_index(
        (passage,), snapshot_id="snapshot-1", source_revision_hashes={7: "a" * 64}
    )
    cards = tuple(_card_record(note_id, ("#AK_Step::Heme",)) for note_id in (1, 2))
    ledger = CardConceptLedger(
        lecture_entity_count=1,
        concepts=(
            CardConcept(
                concept_id="C01",
                canonical_statement="Factor deficiency prolongs the assay.",
                primary_entity="factor deficiency",
                depth="deep",
                emphasis_flag=False,
                importance="high",
            ),
        ),
    )
    prefilter = SemanticPreFilterResult(
        pre_filtered_note_ids=(1, 2),
        pre_excluded_note_ids=(),
        threshold=0.42,
        similarity_stats={"min": 0.8, "max": 0.9, "mean": 0.85, "median": 0.85},
    )
    scope = TagScopeResult(
        snapshot_id="snapshot-1",
        filters_sha256="b" * 64,
        scoped_note_ids=(1, 2),
        unscoped_note_ids=(),
    )

    class FaultInjectingStructuredService:
        def generate_json(self, _instruction, _input_text, **kwargs):
            generation = GeneratedText(
                text="invalid fast output",
                provider=kwargs["provider"],
                model=kwargs["model"],
                request_id=f"fast-{mode}",
                input_tokens=30,
                output_tokens=15,
                cost_microusd=8,
            )
            if mode in {"malformed", "duplicate"}:
                raise StructuredOutputError(
                    f"fast {mode} output",
                    raw_text=generation.text,
                    generation=generation,
                )
            if mode == "network":
                raise LLMRequestError("temporary network failure", source=DiagnosticSource.NETWORK)
            rows = [
                FastCardClassification(
                    note_id=note_id,
                    verdict="LIKELY_YES",
                    grounded_concept_ids=("C01",),
                    supporting_passage_ids=(source.passages[0].passage_id,),
                    reason="grounded",
                )
                for note_id in (1, 2)
            ]
            if mode == "missing":
                rows.pop()
            elif mode == "extra":
                rows.append(FastCardClassification(note_id=99, verdict="LIKELY_NO", reason="extra"))
            elif mode == "invented_concept":
                rows[0] = rows[0].model_copy(update={"grounded_concept_ids": ("C99",)})
            elif mode == "invented_passage":
                rows[0] = rows[0].model_copy(update={"supporting_passage_ids": ("missing",)})
            elif mode == "blank_reason":
                rows[0] = rows[0].model_copy(update={"reason": ""})
            elif mode == "ungrounded_yes":
                rows[0] = rows[0].model_copy(
                    update={"grounded_concept_ids": (), "supporting_passage_ids": ()}
                )
            value = FastClassificationResult(results=tuple(rows))
            return StructuredJSONResult(
                value=value,
                raw_text=value.model_dump_json(),
                provider=generation.provider,
                model=generation.model,
                request_id=generation.request_id,
                input_tokens=generation.input_tokens,
                output_tokens=generation.output_tokens,
                cost_microusd=generation.cost_microusd,
            )

    prompts = AnkiPromptCatalogService()
    fast_prompt = AnkiPromptLibrary(prompts.bundled_directory).load("card-centric-fast-classifier")
    thorough_prompt = AnkiPromptLibrary(prompts.bundled_directory).load("card-centric-classifier")
    runner = CurationServicesRunner.__new__(CurationServicesRunner)
    runner.structured = FaultInjectingStructuredService()
    runner.prompts = AnkiPromptCatalogService(bundled_directory=tmp_path)
    context = SimpleNamespace(
        job=SimpleNamespace(
            pipeline_contract_version=PipelineContractVersion.CARD_CENTRIC_V2,
            resolved_model_config=SimpleNamespace(
                fast_classify_s4b=SimpleNamespace(provider="openai", model="gpt-4o-mini"),
                canonical_document=lambda: {"fast_classify_s4b": "gpt-4o-mini"},
            ),
        ),
        prior_payloads={
            CurationStage.PREFLIGHT: {
                "prompt_snapshot": [
                    {
                        "id": fast_prompt.metadata.id,
                        "version": fast_prompt.metadata.version,
                        "prompt_hash": fast_prompt.prompt_hash,
                        "content": fast_prompt.content,
                        "metadata": fast_prompt.metadata.model_dump(mode="json", by_alias=True),
                    },
                    {
                        "id": thorough_prompt.metadata.id,
                        "version": thorough_prompt.metadata.version,
                        "prompt_hash": thorough_prompt.prompt_hash,
                        "content": thorough_prompt.content,
                        "metadata": thorough_prompt.metadata.model_dump(mode="json", by_alias=True),
                    },
                ]
            },
            CurationStage.SOURCE_INDEX: {
                "source_index": source.model_dump(mode="json"),
                "cards": [card.model_dump(mode="json") for card in cards],
            },
            CurationStage.CARD_LEDGER: {"ledger": ledger.model_dump(mode="json")},
            CurationStage.CARD_PREFILTER: prefilter.model_dump(mode="json"),
            CurationStage.CARD_TAG_SCOPE: {"scope": scope.model_dump(mode="json")},
        },
    )
    return runner, context


@pytest.mark.parametrize(
    ("mode", "reason_code"),
    [
        ("missing", "partition_mismatch"),
        ("extra", "partition_mismatch"),
        ("malformed", "structured_output_invalid"),
        ("duplicate", "structured_output_invalid"),
        ("invented_concept", "invented_concept_id"),
        ("invented_passage", "invented_passage_id"),
        ("blank_reason", "blank_reason"),
        ("ungrounded_yes", "ungrounded_likely_yes"),
    ],
)
def test_v2_fast_classifier_degrades_invalid_batches_to_thorough_review(
    tmp_path: Path, mode: str, reason_code: str
) -> None:
    runner, context = _fast_failure_harness(tmp_path, mode)

    product = asyncio.run(runner._card_fast_classify(context))

    assert [
        (row["note_id"], row["verdict"]) for row in product.payload["fast_classifier"]["results"]
    ] == [(1, "NEEDS_REVIEW"), (2, "NEEDS_REVIEW")]
    assert product.payload["degraded_batches"] == [
        {"batch_index": 0, "note_ids": [1, 2], "reason_code": reason_code}
    ]
    assert product.payload["degraded_note_count"] == 2
    assert product.usage is not None


def test_v2_fast_classifier_preserves_retryable_provider_failures(tmp_path: Path) -> None:
    runner, context = _fast_failure_harness(tmp_path, "network")

    with pytest.raises(LLMRequestError, match="temporary network failure"):
        asyncio.run(runner._card_fast_classify(context))


def test_v2_degraded_fast_batch_is_sent_wholly_to_s4c(tmp_path: Path, monkeypatch) -> None:
    runner, context = _fast_failure_harness(tmp_path, "missing")
    fast_product = asyncio.run(runner._card_fast_classify(context))
    context.prior_payloads[CurationStage.CARD_FAST_CLASSIFY] = fast_product.payload
    context.job.resolved_model_config.classify_s4 = SimpleNamespace(
        provider="openai", model="gpt-4o-mini"
    )
    runner.structured = SimpleNamespace(generator=SimpleNamespace())
    runner.prompts = AnkiPromptCatalogService()
    seen: list[int] = []

    async def fake_classify(_self, cards, **_kwargs):
        seen.extend(card.note_id for card in cards)
        return ClassifierResult(
            results=tuple(
                CardClassification(
                    note_id=card.note_id,
                    verdict="MAYBE",
                    primary_subject="fixture",
                    reason="thorough fallback review",
                )
                for card in cards
            ),
            telemetry=ClassifierTelemetry(
                batch_count=0,
                cache_prefix_sha256="c" * 64,
                cache_mode="ordinary_prefix",
                provider="openai",
                model="gpt-4o-mini",
                request_ids=(),
                batches=(),
            ),
        )

    monkeypatch.setattr(stages_module.CardCentricClassifier, "classify", fake_classify)

    thorough_product = asyncio.run(runner._card_classify(context))

    assert seen == [1, 2]
    assert thorough_product.payload["thorough_count"] == 2
    assert [row["note_id"] for row in thorough_product.payload["classifier"]["results"]] == [1, 2]


def test_v2_internal_prompts_are_read_only_from_the_pinned_preflight_snapshot() -> None:
    prompt_specs = {
        "card-centric-ledger-v2": "lcl_v2",
        "card-centric-fast-classifier": "card_centric_fast_classify_v2",
        "card-centric-classifier": "card_centric_classify_v1",
        "card-centric-gap-v2": "gap_cards_v2",
    }
    snapshot = []
    for prompt_id, schema in prompt_specs.items():
        content = f"Pinned {prompt_id} instruction"
        snapshot.append(
            {
                "id": prompt_id,
                "version": "2.0.0",
                "prompt_hash": hashlib.sha256(content.encode()).hexdigest()[:12],
                "content": content,
                "metadata": {
                    "id": prompt_id,
                    "version": "2.0.0",
                    "schema": schema,
                    "response_format": "json",
                },
            }
        )
    context = SimpleNamespace(
        prior_payloads={CurationStage.PREFLIGHT: {"prompt_snapshot": snapshot}}
    )

    assert {
        prompt_id: _pinned_card_v2_prompt(context, prompt_id) for prompt_id in prompt_specs
    } == {prompt_id: f"Pinned {prompt_id} instruction" for prompt_id in prompt_specs}


def test_v2_internal_prompt_rejects_a_malformed_pinned_snapshot() -> None:
    context = SimpleNamespace(
        prior_payloads={
            CurationStage.PREFLIGHT: {
                "prompt_snapshot": [
                    {
                        "id": "card-centric-fast-classifier",
                        "version": "2.0.0",
                        "prompt_hash": "not-a-content-hash",
                        "content": "tampered",
                        "metadata": {
                            "id": "card-centric-fast-classifier",
                            "version": "2.0.0",
                            "schema": "card_centric_fast_classify_v2",
                            "response_format": "json",
                        },
                    }
                ]
            }
        }
    )

    with pytest.raises(stages_module.PinnedInputChanged, match="snapshot is malformed"):
        _pinned_card_v2_prompt(context, "card-centric-fast-classifier")


class CardGapFillStructuredService:
    def __init__(self, batch: CardGapBatch | tuple[CardGapBatch, ...]) -> None:
        self.batches = list(batch if isinstance(batch, tuple) else (batch,))
        self.instructions: list[str] = []
        self.inputs: list[dict[str, object]] = []

    def generate_json(
        self,
        instruction: str,
        input_text: str,
        *,
        output_model: type[CardGapBatch],
        provider: ProviderName,
        model: str,
        **_: object,
    ) -> StructuredJSONResult[CardGapBatch]:
        assert output_model is CardGapBatch
        self.instructions.append(instruction)
        self.inputs.append(json.loads(input_text))
        batch = self.batches[0] if len(self.batches) == 1 else self.batches.pop(0)
        return StructuredJSONResult(
            value=batch,
            raw_text=batch.model_dump_json(),
            provider=provider,
            model=model,
            request_id="card-gap-fill-request",
            input_tokens=30,
            output_tokens=15,
            cost_microusd=8,
        )


def _card_gap_fill_harness(
    batch: CardGapBatch | tuple[CardGapBatch, ...],
) -> tuple[CurationServicesRunner, SimpleNamespace, CardGapFillStructuredService]:
    passage = SourcePassage.create(
        revision_id=7,
        lecture_id=12,
        artifact_id="slides-7",
        source_kind=SourceKind.SLIDE,
        locator="slide:3",
        text="Alpha, beta, and gamma are grounded in this slide.",
        slide_number=3,
    )
    source = build_source_index(
        (passage,),
        snapshot_id="source-index-1",
        source_revision_hashes={7: "a" * 64},
    )
    ledger = CardConceptLedger(
        lecture_entity_count=1,
        concepts=(
            CardConcept(
                concept_id="C01",
                canonical_statement="Alpha, beta, and gamma are grounded.",
                primary_entity="alpha",
                aliases=("a",),
                depth="deep",
                emphasis_flag=True,
                importance="high",
                suggested_fact_count=3,
                fact_descriptions=("Alpha.", "Beta.", "Gamma."),
                forbidden_cloze_targets_by_fact=(("alpha",), ("beta",), ("gamma",)),
            ),
        ),
    )
    metadata = CanonicalJsonObject.from_mapping({"exam": "block-1"})
    pinned = PinnedLectureMetadata(
        lecture_id=12,
        title="Pinned anemia title",
        metadata=metadata,
        metadata_sha256=hashlib.sha256(
            json.dumps(
                {
                    "lecture_id": 12,
                    "title": "Pinned anemia title",
                    "metadata": metadata.as_dict(),
                },
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest(),
    )
    prompt = "Pinned card gap prompt"
    structured = CardGapFillStructuredService(batch)
    runner = CurationServicesRunner.__new__(CurationServicesRunner)
    runner.structured = structured
    context = SimpleNamespace(
        job=SimpleNamespace(
            lecture_id=12,
            pipeline_contract_version=PipelineContractVersion.CARD_CENTRIC_V2,
            resolved_model_config=SimpleNamespace(
                gap_fill_s7=SimpleNamespace(provider="openai", model="gpt-5.6-terra")
            ),
        ),
        replay_inputs={"pinned_lecture": pinned.model_dump(mode="json")},
        prior_payloads={
            CurationStage.PREFLIGHT: {
                "prompt_snapshot": [
                    {
                        "id": "card-centric-gap-v2",
                        "version": "2.0.0",
                        "prompt_hash": hashlib.sha256(prompt.encode()).hexdigest()[:12],
                        "content": prompt,
                        "metadata": {
                            "id": "card-centric-gap-v2",
                            "version": "2.0.0",
                            "schema": "gap_cards_v2",
                            "response_format": "json",
                        },
                    }
                ],
            },
            CurationStage.SOURCE_INDEX: {"source_index": source.model_dump(mode="json")},
            CurationStage.CARD_LEDGER: {"ledger": ledger.model_dump(mode="json")},
        },
    )
    return runner, context, structured


def _generated_card_gap(
    fact_id: str,
    passage_id: str,
    *,
    split: bool = False,
    split_index: int | None = None,
) -> CardGapOutput:
    return CardGapOutput(
        fact_id=fact_id,
        status="generated",
        text="{{c1::<b>delta</b>}} is grounded.",
        extra="Grounded explanation.",
        note_type="AnKingOverhaul (AnKing Step Deck / AnKingMed)",
        source_passage_ids=(passage_id,),
        split=split,
        split_index=split_index,
    )


def test_card_gap_fill_v2_uses_pinned_metadata_per_fact_targets_and_split_indices(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    batch = CardGapBatch(
        resolutions=(
            _generated_card_gap("C01-M1", _CARD_GAP_FILL_PASSAGE_ID, split=True, split_index=1),
            _generated_card_gap("C01-M1", _CARD_GAP_FILL_PASSAGE_ID, split=True, split_index=2),
            _generated_card_gap("C01-M2", _CARD_GAP_FILL_PASSAGE_ID),
            CardGapOutput(fact_id="C01-M3", status="unresolved", reason="No atomic card."),
        )
    )
    runner, context, structured = _card_gap_fill_harness(batch)
    monkeypatch.setattr(
        stages_module,
        "_merged_card_coverage",
        lambda _: {"C01": {"status": "uncovered", "evidence": []}},
    )

    product = asyncio.run(runner._card_gap_fill(context))

    assert len(structured.inputs) == 1
    sent = structured.inputs[0]
    assert [fact["fact_id"] for fact in sent["missing_facts"]] == [
        "C01-M1",
        "C01-M2",
        "C01-M3",
    ]
    assert "forbidden_cloze_targets" not in sent
    assert sent["forbidden_cloze_targets_by_fact"] == [
        {"fact_id": "C01-M1", "targets": ["alpha"]},
        {"fact_id": "C01-M2", "targets": ["beta"]},
        {"fact_id": "C01-M3", "targets": ["gamma"]},
    ]
    assert sent["lecture_title"] == "Pinned anemia title"
    assert structured.instructions == ["Pinned card gap prompt"]
    assert [row["split_index"] for row in product.payload["resolutions"]] == [1, 2, None, None]


def test_card_gap_fill_v2_requests_only_uncovered_facts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    batch = CardGapBatch(
        resolutions=(_generated_card_gap("C01-M2", _CARD_GAP_FILL_PASSAGE_ID),)
    )
    runner, context, structured = _card_gap_fill_harness(batch)
    monkeypatch.setattr(
        stages_module,
        "_merged_card_coverage",
        lambda _: {
            "C01": {
                "status": "uncovered",
                "evidence": [],
                "facts": {
                    "C01-M1": {"status": "covered", "evidence": [{"note_id": 1}]},
                    "C01-M2": {"status": "uncovered", "evidence": []},
                    "C01-M3": {"status": "covered", "evidence": [{"note_id": 2}]},
                },
            }
        },
    )

    asyncio.run(runner._card_gap_fill(context))

    assert structured.inputs[0]["missing_facts"] == [
        {"fact_id": "C01-M2", "statement": "Beta."}
    ]


def test_card_gap_fill_v2_repairs_forbidden_cloze_targets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    invalid = CardGapBatch(
        resolutions=(
            CardGapOutput(
                fact_id="C01-M1",
                status="generated",
                text="{{c1::alpha}} is grounded.",
                extra="Grounded explanation.",
                note_type="AnKingOverhaul (AnKing Step Deck / AnKingMed)",
                source_passage_ids=(_CARD_GAP_FILL_PASSAGE_ID,),
            ),
            _generated_card_gap("C01-M2", _CARD_GAP_FILL_PASSAGE_ID),
            CardGapOutput(fact_id="C01-M3", status="unresolved", reason="No atomic card."),
        )
    )
    repaired = invalid.model_copy(
        update={
            "resolutions": (
                _generated_card_gap("C01-M1", _CARD_GAP_FILL_PASSAGE_ID),
                _generated_card_gap("C01-M2", _CARD_GAP_FILL_PASSAGE_ID),
                CardGapOutput(
                    fact_id="C01-M3", status="unresolved", reason="No atomic card."
                ),
            )
        }
    )
    runner, context, structured = _card_gap_fill_harness((invalid, repaired))
    monkeypatch.setattr(
        stages_module,
        "_merged_card_coverage",
        lambda _: {"C01": {"status": "uncovered", "evidence": []}},
    )

    product = asyncio.run(runner._card_gap_fill(context))

    assert len(structured.inputs) == 2
    assert "forbidden targets for facts: C01-M1: alpha" in structured.inputs[1][
        "validation_error"
    ]
    assert len(product.payload["resolutions"]) == 3


def test_card_gap_fill_v2_repairs_inadmissible_evidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    valid = CardGapBatch(
        resolutions=(
            _generated_card_gap("C01-M1", _CARD_GAP_FILL_PASSAGE_ID),
            _generated_card_gap("C01-M2", _CARD_GAP_FILL_PASSAGE_ID),
            CardGapOutput(fact_id="C01-M3", status="unresolved", reason="No atomic card."),
        )
    )
    invalid = valid.model_copy(
        update={
            "resolutions": (
                _generated_card_gap("C01-M1", "SLD:12:9999:P:fabricated"),
                *valid.resolutions[1:],
            )
        }
    )
    runner, context, structured = _card_gap_fill_harness((invalid, valid))
    monkeypatch.setattr(
        stages_module,
        "_merged_card_coverage",
        lambda _: {"C01": {"status": "uncovered", "evidence": []}},
    )

    product = asyncio.run(runner._card_gap_fill(context))

    assert "must cite admissible lecture evidence" in structured.inputs[1][
        "validation_error"
    ]
    assert len(product.payload["resolutions"]) == 3


def test_card_gap_fill_v2_repairs_missing_cloze(monkeypatch: pytest.MonkeyPatch) -> None:
    valid = CardGapBatch(
        resolutions=(
            _generated_card_gap("C01-M1", _CARD_GAP_FILL_PASSAGE_ID),
            _generated_card_gap("C01-M2", _CARD_GAP_FILL_PASSAGE_ID),
            CardGapOutput(fact_id="C01-M3", status="unresolved", reason="No atomic card."),
        )
    )
    invalid = valid.model_copy(
        update={
            "resolutions": (
                valid.resolutions[0].model_copy(update={"text": "<b>alpha</b> is grounded."}),
                *valid.resolutions[1:],
            )
        }
    )
    runner, context, structured = _card_gap_fill_harness((invalid, valid))
    monkeypatch.setattr(
        stages_module,
        "_merged_card_coverage",
        lambda _: {"C01": {"status": "uncovered", "evidence": []}},
    )

    product = asyncio.run(runner._card_gap_fill(context))

    assert "contains no cloze deletion" in structured.inputs[1]["validation_error"]
    assert len(product.payload["resolutions"]) == 3


@pytest.mark.parametrize(
    "batch",
    (
        CardGapBatch(
            resolutions=(
                _generated_card_gap(
                    "C01-M1", _CARD_GAP_FILL_PASSAGE_ID, split=True, split_index=1
                ),
                _generated_card_gap(
                    "C01-M1", _CARD_GAP_FILL_PASSAGE_ID, split=True, split_index=3
                ),
                _generated_card_gap("C01-M2", _CARD_GAP_FILL_PASSAGE_ID),
                CardGapOutput(fact_id="C01-M3", status="unresolved", reason="No atomic card."),
            )
        ),
        CardGapBatch(
            resolutions=(
                _generated_card_gap(
                    "C01-M1", _CARD_GAP_FILL_PASSAGE_ID, split=True, split_index=2
                ),
                _generated_card_gap(
                    "C01-M1", _CARD_GAP_FILL_PASSAGE_ID, split=True, split_index=1
                ),
                _generated_card_gap("C01-M2", _CARD_GAP_FILL_PASSAGE_ID),
                CardGapOutput(fact_id="C01-M3", status="unresolved", reason="No atomic card."),
            )
        ),
        CardGapBatch(
            resolutions=(
                _generated_card_gap("C01-M1", _CARD_GAP_FILL_PASSAGE_ID),
                CardGapOutput(fact_id="C01-M1", status="unresolved", reason="Conflict."),
                _generated_card_gap("C01-M2", _CARD_GAP_FILL_PASSAGE_ID),
                CardGapOutput(fact_id="C01-M3", status="unresolved", reason="No atomic card."),
            )
        ),
    ),
)
def test_card_gap_fill_v2_rejects_malformed_terminal_output(
    monkeypatch: pytest.MonkeyPatch,
    batch: CardGapBatch,
) -> None:
    runner, context, _ = _card_gap_fill_harness(batch)
    monkeypatch.setattr(
        stages_module,
        "_merged_card_coverage",
        lambda _: {"C01": {"status": "uncovered", "evidence": []}},
    )

    with pytest.raises(stages_module.PinnedInputChanged, match="exclusive|sequential"):
        asyncio.run(runner._card_gap_fill(context))


def test_card_gap_fill_v2_requires_p1_pinned_metadata_hook(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    batch = CardGapBatch(
        resolutions=(CardGapOutput(fact_id="C01-M1", status="unresolved", reason="No card."),)
    )
    runner, context, structured = _card_gap_fill_harness(batch)
    stale_preflight_value = context.replay_inputs.pop("pinned_lecture")
    context.prior_payloads[CurationStage.PREFLIGHT]["pinned_lecture_metadata"] = (
        stale_preflight_value
    )
    monkeypatch.setattr(
        stages_module,
        "_merged_card_coverage",
        lambda _: {"C01": {"status": "uncovered", "evidence": []}},
    )

    with pytest.raises(stages_module.PinnedInputChanged, match="P1 pinned lecture metadata"):
        asyncio.run(runner._card_gap_fill(context))
    assert structured.inputs == []


def test_v2_residual_classifies_a_prefilter_fallback(monkeypatch) -> None:
    passage = SourcePassage.create(
        revision_id=7,
        lecture_id=12,
        artifact_id="slides-7",
        source_kind=SourceKind.SLIDE,
        locator="slide:1",
        text="Evidence for fallback.",
        slide_number=1,
    )
    source = build_source_index(
        (passage,), snapshot_id="snapshot-1", source_revision_hashes={7: "a" * 64}
    )
    card = _card_record(1, ("#AK_Step::Heme",))
    ledger = CardConceptLedger(
        lecture_entity_count=1,
        concepts=(
            CardConcept(
                concept_id="C01",
                canonical_statement="fact",
                primary_entity="Fallback",
                depth="deep",
                emphasis_flag=False,
                importance="high",
            ),
        ),
    )
    scope = TagScopeResult(
        snapshot_id="snapshot-1",
        filters_sha256="b" * 64,
        scoped_note_ids=(1,),
        unscoped_note_ids=(),
    )
    empty_classifier = ClassifierResult(
        results=(),
        telemetry=ClassifierTelemetry(
            batch_count=0,
            cache_prefix_sha256="c" * 64,
            cache_mode="ordinary_prefix",
            provider="openai",
            model="fixture",
            request_ids=(),
            batches=(),
        ),
    )
    seen: list[int] = []

    async def fake_classify(_self, cards, **_kwargs):
        seen.extend(card.note_id for card in cards)
        return ClassifierResult(
            results=(
                CardClassification(
                    note_id=1,
                    verdict="YES",
                    primary_subject="fixture",
                    reason="residual",
                    covered_concept_ids=("C01",),
                    supporting_passage_ids=(source.passages[0].passage_id,),
                ),
            ),
            telemetry=empty_classifier.telemetry,
        )

    class FakeSemantic:
        async def search(self, _queries, **_kwargs):
            return [[SemanticHit(note_id=1, score=0.9, content_hash=card.content_sha256)]]

    monkeypatch.setattr(stages_module.CardCentricClassifier, "classify", fake_classify)
    runner = CurationServicesRunner.__new__(CurationServicesRunner)
    runner.semantic = FakeSemantic()
    runner.structured = SimpleNamespace(generator=SimpleNamespace())
    runner.prompts = AnkiPromptCatalogService()
    context = SimpleNamespace(
        job=SimpleNamespace(
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
            CurationStage.CARD_CLASSIFY: {"classifier": empty_classifier.model_dump(mode="json")},
        },
    )

    product = asyncio.run(runner._card_residual(context))

    assert seen == [1]
    assert product.payload["audits"][0]["classified_note_ids"] == [1]


def test_priority_candidate_groups_preserve_deck_order() -> None:
    candidates = (
        Candidate(
            note_id=2,
            content_hash="2" * 64,
            best_concept_id="c1",
            provenance={"deck_priority": 1},
            scores={},
            predicted_band="unjudged",
            verdict="pending",
            confidence=0,
            reason="retrieved",
            context_trap=False,
            recall_direction="unknown",
            mnemonic_classification="unknown",
            dedupe_disposition="pending",
            selected=False,
        ),
        Candidate(
            note_id=1,
            content_hash="1" * 64,
            best_concept_id="c1",
            provenance={"deck_priority": 0},
            scores={},
            predicted_band="unjudged",
            verdict="pending",
            confidence=0,
            reason="retrieved",
            context_trap=False,
            recall_direction="unknown",
            mnemonic_classification="unknown",
            dedupe_disposition="pending",
            selected=False,
        ),
    )
    assert [group[0].note_id for group in _priority_candidate_groups(candidates)] == [
        1,
        2,
    ]


class FakeLLMSettings:
    def __init__(self, model: str) -> None:
        self.model = model
        self.requested_providers: list[ProviderName] = []

    def get(self, provider: ProviderName) -> SimpleNamespace:
        self.requested_providers.append(provider)
        return SimpleNamespace(model=self.model)


def test_legacy_job_without_pinned_model_falls_back_to_provider_card_model() -> None:
    runner = CurationServicesRunner.__new__(CurationServicesRunner)
    runner.llm_settings = FakeLLMSettings("gpt-5.2")
    context = SimpleNamespace(
        job=SimpleNamespace(provider="openai", model=""),
    )

    resolved = runner._model(context)

    assert resolved == "gpt-5.2"
    assert runner.llm_settings.requested_providers == [ProviderName.OPENAI]


def test_pinned_job_model_is_used_without_consulting_provider_settings() -> None:
    runner = CurationServicesRunner.__new__(CurationServicesRunner)
    runner.llm_settings = FakeLLMSettings("gpt-5.2")
    context = SimpleNamespace(
        job=SimpleNamespace(provider="openai", model="gpt-4o-mini"),
    )

    resolved = runner._model(context)

    assert resolved == "gpt-4o-mini"
    assert runner.llm_settings.requested_providers == []


def test_preflight_snapshots_all_prompts_for_the_job() -> None:
    runner = CurationServicesRunner.__new__(CurationServicesRunner)
    runner.runtime = ReadyRuntime()
    runner.prompts = AnkiPromptCatalogService()
    runner.prompt_sync = StaticPromptSynchronizer()
    context = SimpleNamespace(
        job=SimpleNamespace(
            lcl_prompt_version="lecture-concept-ledger",
            judgment_rubric_version="coverage-rubric",
            gap_prompt_version="gap-card-generation",
        )
    )

    product = asyncio.run(runner._preflight(context))

    prompts = {item["id"]: item for item in product.payload["prompt_snapshot"]}
    assert set(prompts) == {
        "lecture-concept-ledger",
        "coverage-rubric",
        "card-relevance-audit",
        "gap-card-generation",
        "paraphrase-expansion",
    }
    assert all(len(item["prompt_hash"]) == 12 for item in prompts.values())
    assert all(item["content"] for item in prompts.values())
    assert product.payload["prompt_sync_stale"] is False


class V2StageStructuredService:
    def __init__(self, ledger: LectureConceptLedgerV2) -> None:
        self.ledger = ledger

    def generate_json(
        self,
        instruction: str,
        input_text: str,
        *,
        output_model: type[LectureConceptLedgerV2],
        provider: ProviderName,
        model: str,
    ) -> StructuredJSONResult[LectureConceptLedgerV2]:
        del instruction, input_text
        assert output_model is LectureConceptLedgerV2
        return StructuredJSONResult(
            value=self.ledger,
            raw_text=self.ledger.model_dump_json(),
            provider=provider,
            model=model,
            request_id="lcl-v2-request",
            input_tokens=40,
            output_tokens=20,
            cost_microusd=7,
        )


def test_lcl_stage_activates_schema_from_pinned_prompt_metadata() -> None:
    slide = SourcePassage.create(
        revision_id=7,
        lecture_id=12,
        artifact_id="slides-7",
        source_kind=SourceKind.SLIDE,
        locator="slide:3",
        text="Iron deficiency causes low ferritin.",
        slide_number=3,
    )
    transcript = SourcePassage.create(
        revision_id=8,
        lecture_id=12,
        artifact_id="transcript-8",
        source_kind=SourceKind.TRANSCRIPT,
        locator="transcript:1:12-24",
        text="Iron deficiency depletes iron stores.",
        start_seconds=12,
        end_seconds=24,
    )
    ledger = LectureConceptLedgerV2(
        lecture_entity_count=2,
        concepts=(
            LectureConceptV2(
                concept_id="C01",
                canonical_statement="Iron deficiency causes low ferritin.",
                hypothetical_card="Iron deficiency causes {{c1::low ferritin}}.",
                primary_entity="iron deficiency",
                aliases=("low ferritin",),
                paraphrases=(
                    "iron deficiency low ferritin",
                    "iron deficiency depleted stores",
                    "iron deficiency laboratory findings",
                ),
                depth="deep",
                emphasis_flag=False,
                importance="high",
                passage_ids=(slide.source_id, transcript.source_id),
            ),
        ),
        intentionally_uncited=(),
    )
    runner = CurationServicesRunner.__new__(CurationServicesRunner)
    runner.structured = V2StageStructuredService(ledger)
    context = SimpleNamespace(
        job=SimpleNamespace(
            lcl_prompt_version="lecture-concept-ledger",
            provider="openai",
            model="gpt-5.2",
        ),
        prior_payloads={
            CurationStage.PREFLIGHT: {
                "prompt_snapshot": [
                    {
                        "id": "lecture-concept-ledger",
                        "content": "# V2 ledger prompt",
                        "prompt_hash": "123456789abc",
                        "metadata": {"schema": "lcl_v2"},
                    }
                ]
            },
            CurationStage.SOURCE_INDEX: {
                "passages": [
                    stages_module._passage_payload(slide),
                    stages_module._passage_payload(transcript),
                ]
            },
        },
    )

    product = asyncio.run(runner._lcl(context))

    assert product.payload["ledger"] == ledger.model_dump(mode="json")
    assert product.payload["prompt_hash"] == "123456789abc"
    assert product.payload["schema_name"] == "lcl_v2"


def test_downstream_ledger_reader_adapts_v2_artifact() -> None:
    passage = SourcePassage.create(
        revision_id=7,
        lecture_id=12,
        artifact_id="slides-7",
        source_kind=SourceKind.SLIDE,
        locator="slide:3",
        text="Iron deficiency causes low ferritin.",
        slide_number=3,
    )
    ledger = LectureConceptLedgerV2(
        lecture_entity_count=2,
        concepts=(
            LectureConceptV2(
                concept_id="C01",
                canonical_statement="Iron deficiency causes low ferritin.",
                hypothetical_card="Iron deficiency causes {{c1::low ferritin}}.",
                primary_entity="iron deficiency",
                aliases=("low ferritin",),
                paraphrases=(
                    "iron deficiency low ferritin",
                    "iron deficiency depleted stores",
                    "iron deficiency laboratory findings",
                ),
                depth="deep",
                emphasis_flag=True,
                importance="high",
                passage_ids=(passage.source_id,),
            ),
        ),
        intentionally_uncited=(),
    )
    context = SimpleNamespace(
        prior_payloads={
            CurationStage.SOURCE_INDEX: {"passages": [stages_module._passage_payload(passage)]},
            CurationStage.LCL: {
                "ledger": ledger.model_dump(mode="json"),
                "schema_name": "lcl_v2",
            },
        }
    )

    runtime = stages_module._ledger(context)

    assert runtime.concepts[0].statement == ("Iron deficiency causes low ferritin.")
    assert runtime.concepts[0].source_refs[0].passage_id == passage.passage_id
    assert runtime.concepts[0].primary_entity == "iron deficiency"


class CoverageCache:
    def __init__(self) -> None:
        self.records: dict[str, JudgmentCacheRecord] = {}

    def get_judgment_cache(
        self,
        cache_key: str,
    ) -> JudgmentCacheRecord | None:
        return self.records.get(cache_key)

    def save_judgment_cache(self, record: JudgmentCacheRecord) -> None:
        self.records.setdefault(record.cache_key, record)


class CompanionNotes:
    def __init__(self, note: NormalizedNote) -> None:
        self.note = note

    def get_note(self, note_id: int) -> NormalizedNote | None:
        return self.note if note_id == self.note.note_id else None


class V2CoverageStructuredService:
    def __init__(self, judgment: CoverageJudgmentV2) -> None:
        self.judgment = judgment

    def generate_json(
        self,
        instruction: str,
        input_text: str,
        *,
        output_model: type[CoverageJudgmentV2],
        provider: ProviderName,
        model: str,
    ) -> StructuredJSONResult[CoverageJudgmentV2]:
        del instruction, input_text
        assert output_model is CoverageJudgmentV2
        return StructuredJSONResult(
            value=self.judgment,
            raw_text=self.judgment.model_dump_json(),
            provider=provider,
            model=model,
            request_id="coverage-v2-request",
            input_tokens=30,
            output_tokens=15,
            cost_microusd=8,
        )


def test_judgment_stage_activates_v2_coverage_from_prompt_metadata() -> None:
    passage = SourcePassage.create(
        revision_id=7,
        lecture_id=12,
        artifact_id="slides-7",
        source_kind=SourceKind.SLIDE,
        locator="slide:3",
        text="Iron deficiency causes low ferritin.",
        slide_number=3,
    )
    ledger = LectureConceptLedgerV2(
        lecture_entity_count=2,
        concepts=(
            LectureConceptV2(
                concept_id="C01",
                canonical_statement="Iron deficiency causes low ferritin.",
                hypothetical_card="Iron deficiency causes {{c1::low ferritin}}.",
                primary_entity="iron deficiency",
                aliases=("low ferritin",),
                paraphrases=(
                    "iron deficiency low ferritin",
                    "iron deficiency depleted stores",
                    "iron deficiency laboratory findings",
                ),
                depth="deep",
                emphasis_flag=False,
                importance="high",
                passage_ids=(passage.source_id,),
            ),
        ),
        intentionally_uncited=(),
    )
    judgment = CoverageJudgmentV2(
        concept_id="C01",
        supporting_note_ids=(1,),
        missing_facts=(
            MissingFactV2(
                fact_id="C01-M1",
                statement="Iron stores fall before microcytosis.",
                passage_ids=(passage.source_id,),
            ),
        ),
        rationale="The note omits the laboratory sequence.",
    )
    note = NormalizedNote(
        note_id=1,
        model_name="AnKingOverhaul",
        text="Iron deficiency causes low ferritin.",
        extra="Ferritin reflects iron stores.",
        raw_fields={"Text": "Iron deficiency causes low ferritin."},
        tags=("#Pathoma",),
        card_ids=(101,),
        media=(),
        token_signature="iron deficiency ferritin",
        content_sha256="1" * 64,
    )
    candidate = Candidate(
        note_id=1,
        content_hash="1" * 64,
        best_concept_id="C01",
        provenance={},
        scores={"boosted_score": 0.9},
        predicted_band="unjudged",
        verdict="pending",
        confidence=0,
        reason="retrieved",
        context_trap=False,
        recall_direction="unknown",
        mnemonic_classification="unknown",
        dedupe_disposition="pending",
        selected=False,
        retrieval_pass=RetrievalPass.PASS_1,
    )
    runner = CurationServicesRunner.__new__(CurationServicesRunner)
    runner.structured = V2CoverageStructuredService(judgment)
    runner.repository = CoverageCache()
    runner.companion = CompanionNotes(note)
    context = SimpleNamespace(
        job=SimpleNamespace(
            judgment_rubric_version="coverage-rubric",
            provider="openai",
            model="gpt-5.2",
        ),
        prior_payloads={
            CurationStage.PREFLIGHT: {
                "prompt_snapshot": [
                    {
                        "id": "coverage-rubric",
                        "content": "# Coverage rubric V2",
                        "prompt_hash": "123456789abc",
                        "metadata": {"schema": "coverage_v2"},
                    }
                ]
            },
            CurationStage.SOURCE_INDEX: {"passages": [stages_module._passage_payload(passage)]},
            CurationStage.LCL: {
                "ledger": ledger.model_dump(mode="json"),
                "schema_name": "lcl_v2",
            },
            CurationStage.RETRIEVAL_PASS_1: {
                "groups": {"C01": [stages_module._candidate_payload(candidate)]}
            },
        },
    )

    product = asyncio.run(runner._judgment_pass_1(context))

    assert product.payload["schema_name"] == "coverage_v2"
    assert product.payload["judgments"]["C01"]["judgment"] == (judgment.model_dump(mode="json"))
    assert product.candidates is not None
    assert product.candidates[0].predicted_band == "partial"


def test_downstream_coverage_reader_adapts_v2_artifact() -> None:
    judgment = CoverageJudgmentV2(
        concept_id="C01",
        supporting_note_ids=(1,),
        missing_facts=(
            MissingFactV2(
                fact_id="C01-M1",
                statement="Iron stores fall before microcytosis.",
                passage_ids=("TRX:07:0198",),
            ),
        ),
        rationale="The note omits the laboratory sequence.",
    )
    context = SimpleNamespace(
        prior_payloads={
            CurationStage.JUDGMENT_PASS_1: {
                "schema_name": "coverage_v2",
                "judgments": {"C01": {"judgment": judgment.model_dump(mode="json")}},
            }
        }
    )

    runtime = stages_module._coverage_judgment(
        context,
        CurationStage.JUDGMENT_PASS_1,
        "C01",
    )

    assert runtime.status == "partial"
    assert runtime.missing_fact_records[0].fact_id == "C01-M1"


class AuditRepository(CoverageCache):
    def __init__(self, candidate: Candidate) -> None:
        super().__init__()
        self.candidate = candidate
        self.audit_records: dict[str, AuditCacheRecord] = {}

    def list_candidates(self, job_id: object) -> list[Candidate]:
        del job_id
        return [self.candidate]

    def lecture_title(self, lecture_id: int) -> str:
        assert lecture_id == 12
        return "Heme Exam 1 Lecture 7: Anemia IV"

    def get_audit_cache(self, cache_key: str) -> AuditCacheRecord | None:
        return self.audit_records.get(cache_key)

    def save_audit_cache(self, record: AuditCacheRecord) -> None:
        self.audit_records.setdefault(record.cache_key, record)


class AuditStructuredService:
    def __init__(self, verdict: AuditVerdictV2) -> None:
        self.verdict = verdict

    def generate_json(
        self,
        instruction: str,
        input_text: str,
        *,
        output_model: type[AuditBatchV2],
        provider: ProviderName,
        model: str,
    ) -> StructuredJSONResult[AuditBatchV2]:
        del instruction, input_text
        assert output_model is AuditBatchV2
        batch = AuditBatchV2(verdicts=(self.verdict,))
        return StructuredJSONResult(
            value=batch,
            raw_text=batch.model_dump_json(),
            provider=provider,
            model=model,
            request_id="audit-request",
            input_tokens=100,
            output_tokens=20,
            cost_microusd=30,
        )


def _audit_stage_fixture() -> tuple[
    SourcePassage,
    LectureConceptLedgerV2,
    Candidate,
    NormalizedNote,
]:
    passage = SourcePassage.create(
        revision_id=7,
        lecture_id=12,
        artifact_id="slides-7",
        source_kind=SourceKind.SLIDE,
        locator="slide:3",
        text="Iron deficiency causes low ferritin.",
        slide_number=3,
    )
    ledger = LectureConceptLedgerV2(
        lecture_entity_count=2,
        concepts=(
            LectureConceptV2(
                concept_id="C01",
                canonical_statement="Iron deficiency causes low ferritin.",
                hypothetical_card="Iron deficiency causes {{c1::low ferritin}}.",
                primary_entity="iron deficiency",
                aliases=("low ferritin",),
                paraphrases=(
                    "iron deficiency low ferritin",
                    "iron deficiency depleted stores",
                    "iron deficiency laboratory findings",
                ),
                depth="deep",
                emphasis_flag=False,
                importance="high",
                passage_ids=(passage.source_id,),
            ),
        ),
        intentionally_uncited=(),
    )
    candidate = Candidate(
        note_id=1,
        content_hash="1" * 64,
        best_concept_id="C01",
        provenance={"query": "hidden retrieval reason"},
        scores={"boosted_score": 0.9},
        predicted_band="covered",
        verdict="include",
        confidence=1,
        reason="old coverage rationale",
        context_trap=False,
        recall_direction="unknown",
        mnemonic_classification="unknown",
        dedupe_disposition="pending",
        selected=True,
        retrieval_pass=RetrievalPass.PASS_1,
    )
    note = NormalizedNote(
        note_id=1,
        model_name="AnKingOverhaul",
        text="Hemophilia A is inherited in an X-linked recessive pattern.",
        extra="Factor VIII deficiency.",
        raw_fields={"Text": "Hemophilia A is X-linked recessive."},
        tags=("#Pathoma",),
        card_ids=(101,),
        media=(),
        token_signature="hemophilia x linked",
        content_sha256="1" * 64,
    )
    return passage, ledger, candidate, note


def test_card_audit_stage_replaces_coverage_selection_with_blind_verdict() -> None:
    passage, ledger, candidate, note = _audit_stage_fixture()
    verdict = AuditVerdictV2(
        nid=1,
        verdict="drop",
        primary_subject="hemophilia A",
        support="none",
        reason="Different disease sharing only an inheritance pattern",
        structure_issue=("context_trap",),
    )
    runner = CurationServicesRunner.__new__(CurationServicesRunner)
    runner.structured = AuditStructuredService(verdict)
    runner.repository = AuditRepository(candidate)
    runner.companion = CompanionNotes(note)
    context = SimpleNamespace(
        job=SimpleNamespace(
            id="job-1",
            lecture_id=12,
            provider="openai",
            model="gpt-5.2",
        ),
        prior_payloads={
            CurationStage.PREFLIGHT: {
                "prompt_snapshot": [
                    {
                        "id": "card-relevance-audit",
                        "content": "# Blind audit",
                        "prompt_hash": "123456789abc",
                        "metadata": {
                            "schema": "audit_verdict_v2",
                            "batch_size": 30,
                        },
                    }
                ]
            },
            CurationStage.SOURCE_INDEX: {"passages": [stages_module._passage_payload(passage)]},
            CurationStage.LCL: {
                "ledger": ledger.model_dump(mode="json"),
                "schema_name": "lcl_v2",
            },
        },
    )

    product = asyncio.run(runner._card_audit(context))

    assert product.payload["verdicts"] == [verdict.model_dump(mode="json")]
    assert product.candidates is not None
    audited = product.candidates[0]
    assert audited.verdict == "drop"
    assert audited.selected is False
    assert audited.context_trap is True
    assert audited.provenance["audit"]["primary_subject"] == "hemophilia A"


class MissingCoverageStructuredService:
    def __init__(self, judgment: CoverageJudgmentV2) -> None:
        self.judgment = judgment
        self.calls = 0
        self.inputs: list[str] = []

    def generate_json(
        self,
        instruction: str,
        input_text: str,
        *,
        output_model: type[CoverageJudgmentV2],
        provider: ProviderName,
        model: str,
    ) -> StructuredJSONResult[CoverageJudgmentV2]:
        del instruction
        assert output_model is CoverageJudgmentV2
        self.calls += 1
        self.inputs.append(input_text)
        return StructuredJSONResult(
            value=self.judgment,
            raw_text=self.judgment.model_dump_json(),
            provider=provider,
            model=model,
            request_id="recompute-request",
            input_tokens=25,
            output_tokens=15,
            cost_microusd=9,
        )


def test_coverage_recompute_creates_missing_fact_after_audit_drop() -> None:
    passage, ledger, candidate, note = _audit_stage_fixture()
    original = CoverageJudgmentV2(
        concept_id="C01",
        supporting_note_ids=(1,),
        missing_facts=(),
        rationale="The candidate appears to cover the concept.",
    )
    recomputed = CoverageJudgmentV2(
        concept_id="C01",
        supporting_note_ids=(),
        missing_facts=(
            MissingFactV2(
                fact_id="C01-M1",
                statement="Iron deficiency causes low ferritin.",
                passage_ids=(passage.source_id,),
            ),
        ),
        rationale="No audited candidate covers this lecture fact.",
    )
    structured = MissingCoverageStructuredService(recomputed)
    audited_candidate = replace(candidate, verdict="drop", selected=False)
    repository = AuditRepository(audited_candidate)
    runner = CurationServicesRunner.__new__(CurationServicesRunner)
    runner.structured = structured
    runner.repository = repository
    runner.companion = CompanionNotes(note)
    context = SimpleNamespace(
        job=SimpleNamespace(
            id="job-1",
            judgment_rubric_version="coverage-rubric",
            provider="openai",
            model="gpt-5.2",
        ),
        prior_payloads={
            CurationStage.PREFLIGHT: {
                "prompt_snapshot": [
                    {
                        "id": "coverage-rubric",
                        "content": "# Coverage rubric V2",
                        "prompt_hash": "123456789abc",
                        "metadata": {"schema": "coverage_v2"},
                    }
                ]
            },
            CurationStage.SOURCE_INDEX: {"passages": [stages_module._passage_payload(passage)]},
            CurationStage.LCL: {
                "ledger": ledger.model_dump(mode="json"),
                "schema_name": "lcl_v2",
            },
            CurationStage.JUDGMENT_PASS_1: {
                "schema_name": "coverage_v2",
                "judgments": {"C01": {"judgment": original.model_dump(mode="json")}},
            },
            CurationStage.JUDGMENT_PASS_2: {
                "schema_name": "coverage_v2",
                "judgments": {},
            },
            CurationStage.CARD_AUDIT: {
                "verdicts": [
                    AuditVerdictV2(
                        nid=1,
                        verdict="drop",
                        primary_subject="hemophilia A",
                        support="none",
                        reason="Different disease",
                        structure_issue=(),
                    ).model_dump(mode="json")
                ]
            },
        },
    )

    product = asyncio.run(runner._coverage_recompute(context))

    assert structured.calls == 1
    assert product.payload["schema_name"] == "coverage_v2"
    assert product.payload["judgments"]["C01"]["recomputed"] is True
    assert product.payload["judgments"]["C01"]["judgment"] == (recomputed.model_dump(mode="json"))


class MultipleCompanionNotes:
    def __init__(self, notes: tuple[NormalizedNote, ...]) -> None:
        self.notes = {note.note_id: note for note in notes}

    def get_note(self, note_id: int) -> NormalizedNote | None:
        return self.notes.get(note_id)


class MultipleAuditRepository(CoverageCache):
    def __init__(self, candidates: tuple[Candidate, ...]) -> None:
        super().__init__()
        self.candidates = candidates

    def list_candidates(self, job_id: object) -> list[Candidate]:
        del job_id
        return list(self.candidates)


def test_coverage_recompute_combines_surviving_supports_from_both_passes() -> None:
    passage, ledger, first_candidate, first_note = _audit_stage_fixture()
    second_candidate = replace(
        first_candidate,
        note_id=2,
        content_hash="2" * 64,
        retrieval_pass=RetrievalPass.PASS_2_RESCUE,
    )
    second_note = replace(
        first_note,
        note_id=2,
        content_sha256="2" * 64,
        text="Iron deficiency depletes iron stores before microcytosis.",
    )
    first = CoverageJudgmentV2(
        concept_id="C01",
        supporting_note_ids=(1,),
        missing_facts=(
            MissingFactV2(
                fact_id="C01-M1",
                statement="Iron stores fall before microcytosis.",
                passage_ids=(passage.source_id,),
            ),
        ),
        rationale="The first card covers ferritin but not the sequence.",
    )
    second = CoverageJudgmentV2(
        concept_id="C01",
        supporting_note_ids=(2,),
        missing_facts=(
            MissingFactV2(
                fact_id="C01-M1",
                statement="Iron deficiency causes low ferritin.",
                passage_ids=(passage.source_id,),
            ),
        ),
        rationale="The rescue card covers the sequence but not ferritin.",
    )
    combined = CoverageJudgmentV2(
        concept_id="C01",
        supporting_note_ids=(1, 2),
        missing_facts=(),
        rationale="Together the audited cards cover the concept.",
    )
    structured = MissingCoverageStructuredService(combined)
    runner = CurationServicesRunner.__new__(CurationServicesRunner)
    runner.structured = structured
    runner.repository = MultipleAuditRepository((first_candidate, second_candidate))
    runner.companion = MultipleCompanionNotes((first_note, second_note))
    context = SimpleNamespace(
        job=SimpleNamespace(
            id="job-1",
            judgment_rubric_version="coverage-rubric",
            provider="openai",
            model="gpt-5.2",
        ),
        prior_payloads={
            CurationStage.PREFLIGHT: {
                "prompt_snapshot": [
                    {
                        "id": "coverage-rubric",
                        "content": "# Coverage rubric V2",
                        "prompt_hash": "123456789abc",
                        "metadata": {"schema": "coverage_v2"},
                    }
                ]
            },
            CurationStage.SOURCE_INDEX: {"passages": [stages_module._passage_payload(passage)]},
            CurationStage.LCL: {
                "ledger": ledger.model_dump(mode="json"),
                "schema_name": "lcl_v2",
            },
            CurationStage.JUDGMENT_PASS_1: {
                "schema_name": "coverage_v2",
                "judgments": {"C01": {"judgment": first.model_dump(mode="json")}},
            },
            CurationStage.JUDGMENT_PASS_2: {
                "schema_name": "coverage_v2",
                "judgments": {"C01": {"judgment": second.model_dump(mode="json")}},
            },
            CurationStage.CARD_AUDIT: {
                "verdicts": [
                    AuditVerdictV2(
                        nid=note_id,
                        verdict="keep",
                        primary_subject="iron deficiency",
                        support="slides",
                        reason="Directly supported by the lecture slide",
                        structure_issue=(),
                    ).model_dump(mode="json")
                    for note_id in (1, 2)
                ]
            },
        },
    )

    product = asyncio.run(runner._coverage_recompute(context))

    assert structured.calls == 1
    assert [
        candidate["note_id"] for candidate in json.loads(structured.inputs[0])["candidates"]
    ] == [1, 2]
    assert product.payload["judgments"]["C01"]["judgment"] == (combined.model_dump(mode="json"))


def test_audit_created_gap_localization_excludes_summary_only_evidence() -> None:
    slide = SourcePassage.create(
        revision_id=7,
        lecture_id=12,
        artifact_id="slides-7",
        source_kind=SourceKind.SLIDE,
        locator="slide:3",
        text="Iron deficiency causes low ferritin.",
        slide_number=3,
    )
    summary = SourcePassage.create(
        revision_id=9,
        lecture_id=12,
        artifact_id="summary-9",
        source_kind=SourceKind.SUMMARY,
        locator="summary:core:1",
        text="Iron deficiency causes low ferritin.",
        source_id="SUM:12:CORE:01",
    )
    ledger = LectureConceptLedgerV2(
        lecture_entity_count=1,
        concepts=(
            LectureConceptV2(
                concept_id="C01",
                canonical_statement="Iron deficiency causes low ferritin.",
                hypothetical_card="Iron deficiency causes {{c1::low ferritin}}.",
                primary_entity="iron deficiency",
                aliases=("low ferritin",),
                paraphrases=(
                    "iron deficiency low ferritin",
                    "iron deficiency depleted stores",
                    "iron deficiency laboratory findings",
                ),
                depth="deep",
                emphasis_flag=False,
                importance="high",
                passage_ids=(slide.source_id, summary.source_id),
            ),
        ),
        intentionally_uncited=(),
    )
    context = SimpleNamespace(
        prior_payloads={
            CurationStage.SOURCE_INDEX: {
                "passages": [
                    stages_module._passage_payload(slide),
                    stages_module._passage_payload(summary),
                ]
            },
            CurationStage.LCL: {
                "ledger": ledger.model_dump(mode="json"),
                "schema_name": "lcl_v2",
            },
        }
    )
    concept = stages_module._ledger(context).concepts[0]

    localization = stages_module._localization_from_concept(
        concept,
        (slide, summary),
    )

    assert localization.evidence == (slide,)


class GapStageRepository:
    def list_candidates(self, job_id: object) -> list[Candidate]:
        del job_id
        return []

    def lecture_title(self, lecture_id: int) -> str:
        assert lecture_id == 12
        raise AssertionError("v2 gap generation must not read a mutable live lecture title")

    def list_source_evidence(self, job_id: object) -> list[object]:
        del job_id
        return []


class V2GapStageStructuredService:
    def __init__(self, batch: GapBatchV2) -> None:
        self.batch = batch
        self.inputs: list[str] = []

    def generate_json(
        self,
        instruction: str,
        input_text: str,
        *,
        output_model: type[GapBatchV2],
        provider: ProviderName,
        model: str,
    ) -> StructuredJSONResult[GapBatchV2]:
        del instruction
        assert output_model is GapBatchV2
        self.inputs.append(input_text)
        return StructuredJSONResult(
            value=self.batch,
            raw_text=self.batch.model_dump_json(),
            provider=provider,
            model=model,
            request_id="gap-v2-request",
            input_tokens=30,
            output_tokens=15,
            cost_microusd=8,
        )


def test_gap_stage_routes_on_audited_missing_facts_not_display_outcome() -> None:
    passage, ledger, _, _ = _audit_stage_fixture()
    ledger = ledger.model_copy(update={"lecture_entity_count": 1})
    judgment = CoverageJudgmentV2(
        concept_id="C01",
        supporting_note_ids=(),
        missing_facts=(
            MissingFactV2(
                fact_id="C01-M1",
                statement="Iron deficiency causes low ferritin.",
                passage_ids=(passage.source_id,),
            ),
        ),
        rationale="No audited card covers ferritin.",
    )
    generated = GeneratedGapCardV2(
        fact_id="C01-M1",
        text="<b>Iron deficiency</b> causes {{c1::<b>low ferritin</b>}}.",
        extra="Ferritin reflects depleted iron stores.",
        note_type="AnKingOverhaul (AnKing Step Deck / AnKingMed)",
        source_passage_ids=(passage.source_id,),
        split=True,
        split_index=1,
        image_needed=None,
    )
    structured = V2GapStageStructuredService(
        GapBatchV2(
            resolutions=(
                generated,
                generated.model_copy(update={"split_index": 2}),
            )
        )
    )
    runner = CurationServicesRunner.__new__(CurationServicesRunner)
    runner.structured = structured
    runner.repository = GapStageRepository()
    runner.companion = MultipleCompanionNotes(())
    runner.embedder = SimpleNamespace()
    pinned_metadata = CanonicalJsonObject.from_mapping({"exam": "block-1"})
    pinned = PinnedLectureMetadata(
        lecture_id=12,
        title="Iron Deficiency Anemia",
        metadata=pinned_metadata,
        metadata_sha256=hashlib.sha256(
            json.dumps(
                {
                    "lecture_id": 12,
                    "title": "Iron Deficiency Anemia",
                    "metadata": pinned_metadata.as_dict(),
                },
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest(),
    )
    context = SimpleNamespace(
        job=SimpleNamespace(
            id="job-1",
            lecture_id=12,
            gap_prompt_version="gap-card-generation",
            provider="openai",
            model="gpt-5.6-terra",
        ),
        replay_inputs={"pinned_lecture": pinned.model_dump(mode="json")},
        prior_payloads={
            CurationStage.PREFLIGHT: {
                "prompt_snapshot": [
                    {
                        "id": "gap-card-generation",
                        "content": "# Gap generation V2",
                        "prompt_hash": "123456789abc",
                        "metadata": {"schema": "gap_cards_v2"},
                    }
                ]
            },
            CurationStage.SOURCE_INDEX: {"passages": [stages_module._passage_payload(passage)]},
            CurationStage.LCL: {
                "ledger": ledger.model_dump(mode="json"),
                "schema_name": "lcl_v2",
            },
            CurationStage.COVERAGE_RECOMPUTE: {
                "schema_name": "coverage_v2",
                "judgments": {"C01": {"judgment": judgment.model_dump(mode="json")}},
            },
            CurationStage.DEDUPE: {"outcomes": {"C01": "covered_audited"}},
            CurationStage.RESCUE: {"localizations": {}},
        },
    )

    product = asyncio.run(runner._generate_gaps(context))

    assert len(structured.inputs) == 1
    sent = json.loads(structured.inputs[0])
    assert [fact["fact_id"] for fact in sent["missing_facts"]] == ["C01-M1"]
    assert sent["forbidden_cloze_targets_by_fact"] == [
        {
            "fact_id": "C01-M1",
            "targets": ["Iron Deficiency Anemia", "iron deficiency"],
        }
    ]
    assert product.payload["forbidden_cloze_targets"] == [
        "Iron Deficiency Anemia",
        "iron deficiency",
    ]
    assert product.gap_cards is not None
    assert len(product.gap_cards) == 1
    assert product.gap_cards[0].provenance["fact_id"] == "C01-M1"


class ReconciliationStageRepository:
    def __init__(self, cards: tuple[GapCard, ...]) -> None:
        self.cards = cards

    def list_candidates(self, job_id: object) -> list[Candidate]:
        del job_id
        return []

    def list_gap_cards(self, job_id: object) -> list[GapCard]:
        del job_id
        return list(self.cards)


def _reconciliation_context(
    *,
    prompt_sync_stale: bool,
) -> tuple[SimpleNamespace, SourcePassage]:
    passage, ledger, _, _ = _audit_stage_fixture()
    judgment = CoverageJudgmentV2(
        concept_id="C01",
        supporting_note_ids=(),
        missing_facts=(
            MissingFactV2(
                fact_id="C01-M1",
                statement="Iron deficiency causes low ferritin.",
                passage_ids=(passage.source_id,),
            ),
        ),
        rationale="No existing card covers ferritin.",
    )
    return (
        SimpleNamespace(
            job=SimpleNamespace(id="job-1"),
            prior_payloads={
                CurationStage.PREFLIGHT: {
                    "prompt_sync_stale": prompt_sync_stale,
                    "prompt_snapshot": [],
                },
                CurationStage.SOURCE_INDEX: {"passages": [stages_module._passage_payload(passage)]},
                CurationStage.LCL: {
                    "ledger": ledger.model_dump(mode="json"),
                    "schema_name": "lcl_v2",
                },
                CurationStage.CONVERGENCE_PASS_5: {
                    "concepts": [
                        {
                            "concept_id": "C01",
                            "passes_run": 3,
                            "seen_note_ids": [],
                            "growth": [1.0, 0.1, 0.0],
                            "converged": True,
                        }
                    ]
                },
                CurationStage.CARD_AUDIT: {"verdicts": []},
                CurationStage.COVERAGE_RECOMPUTE: {
                    "schema_name": "coverage_v2",
                    "judgments": {"C01": {"judgment": judgment.model_dump(mode="json")}},
                },
                CurationStage.GAPS: {
                    "schema_name": "gap_cards_v2",
                    "unresolved": [],
                    "forbidden_cloze_targets": ["Iron Deficiency Anemia"],
                },
            },
        ),
        passage,
    )


def test_reconciliation_stage_allows_warning_only_report() -> None:
    context, _ = _reconciliation_context(prompt_sync_stale=True)
    runner = CurationServicesRunner.__new__(CurationServicesRunner)
    runner.repository = ReconciliationStageRepository(
        (
            GapCard(
                card_id="gap-1",
                concept_id="C01",
                text="<b>Iron deficiency</b> causes {{c1::<b>low ferritin</b>}}.",
                extra="Ferritin reflects depleted stores.",
                provenance={"fact_id": "C01-M1"},
            ),
        )
    )

    product = asyncio.run(runner._reconciliation(context))

    assert product.blocking_error is None
    assert product.payload["can_render_envelope"] is True
    assert [item["assertion_id"] for item in product.payload["warned"]] == ["A11"]
    assert product.payload["metrics"] == {
        "audit_keep": 0,
        "audit_drop": 0,
        "audit_uncertain": 0,
        "audit_drop_rate": 0.0,
        "unresolved_concepts": 0,
        "uncited_passage_ids": [],
        "prompt_sync_stale": True,
    }
    assert product.payload["snapshot"]["generated_cards"][0]["fact_id"] == ("C01-M1")


def test_reconciliation_stage_blocks_missing_fact_partition() -> None:
    context, _ = _reconciliation_context(prompt_sync_stale=False)
    runner = CurationServicesRunner.__new__(CurationServicesRunner)
    runner.repository = ReconciliationStageRepository(())

    product = asyncio.run(runner._reconciliation(context))

    assert product.payload["can_render_envelope"] is False
    assert {item["assertion_id"] for item in product.payload["failed"]} >= {
        "A1",
        "A2",
        "A4",
    }
    assert product.blocking_error == "Reconciliation failed: A1, A2, A4"


def test_card_reconciliation_error_includes_every_failed_finding() -> None:
    failed = ReconciliationReport(
        passed=(),
        failed=(
            AssertionFinding(
                assertion_id="A6",
                message="YES plus generated cards must total at least 10",
            ),
            AssertionFinding(
                assertion_id="selection_conservation",
                message=("Selected cards must be drawn from eligible existing or generated output"),
            ),
        ),
        warned=(),
        can_render_envelope=False,
    )
    passed = ReconciliationReport(
        passed=("A1",),
        failed=(),
        warned=(),
        can_render_envelope=True,
    )

    assert stages_module._card_reconciliation_error(failed) == (
        "Card-centric reconciliation failed: "
        "A6: YES plus generated cards must total at least 10 | "
        "selection_conservation: Selected cards must be drawn from eligible existing "
        "or generated output"
    )
    assert stages_module._card_reconciliation_error(passed) is None


@pytest.mark.parametrize("version", ("card_centric_v1", "card_centric_v2"))
def test_card_reconciliation_pending_mandatory_overflow_is_reviewable(version: str) -> None:
    selected = tuple(range(1, 71))
    generated = GeneratedResolution(
        card_id="G1",
        fact_id="C01-M1",
        text="The supported finding is {{c1::present}}.",
    )
    identities = [*(f"existing:{note_id}" for note_id in selected), "generated:G1"]
    metadata = tuple(
        SelectionMetadata(
            identity=identity,
            selected_position=position,
            tier=SelectionTier.T1,
            evidence_quality=EvidenceQuality.PRIMARY_SOURCE,
            mandatory=position == 71,
            marginal_value_reason=(
                MarginalValueReason.ONLY_VALID_REQUIRED_FACT if 66 <= position <= 70 else None
            ),
            overflow_reason="required fixture coverage" if position == 71 else None,
            manual_acknowledgement_required=position == 71,
        )
        for position, identity in enumerate(identities, start=1)
    )
    snapshot = CardCentricReconciliationInput(
        pipeline_contract_version=version,
        concept_ids=("C01",),
        coverage={"C01": "covered"},
        required_fact_ids=("C01-M1",),
        uncovered_after_s5=(),
        residual_ran_for=(),
        generated_cards=(generated,),
        raw_generated_cards=(generated,),
        canonical_generated_cards=(generated,),
        terminal_resolutions=(
            GeneratedFactResolution(
                fact_id="C01-M1",
                kind="generated",
                generated_card_ids=("G1",),
            ),
        ),
        terminal_resolutions_provided=version == "card_centric_v2",
        unresolved_fact_ids=(),
        expected_scoped_nids=selected,
        classifications=tuple(AuditResolution(nid=note_id, verdict="keep") for note_id in selected),
        eligible_yes_nids=selected,
        selected_nids=selected,
        selected_generated_card_ids=("G1",),
        generated_card_ids=("G1",),
        source_passage_ids=(),
        forbidden_cloze_targets=(),
        prompt_sync_stale=False,
        untagged_rate=0,
        mandatory_nids=selected if version == "card_centric_v1" else (),
        mandatory_generated_card_ids=("G1",) if version == "card_centric_v2" else (),
        covered_concept_ids_by_nid={note_id: ("C01",) for note_id in selected},
        generated_concept_id_by_card_id={"G1": "C01"},
        selection_metadata=metadata if version == "card_centric_v2" else (),
        selection_order=tuple(item.identity for item in metadata)
        if version == "card_centric_v2"
        else (),
        selected_count=71 if version == "card_centric_v2" else None,
        below_warning_floor=False if version == "card_centric_v2" else None,
    )

    report = reconcile_card_centric(snapshot)

    assert report.can_render_envelope is False
    assert {finding.assertion_id for finding in report.failed} == {"selection_cap"}
    assert stages_module._card_reconciliation_error(report, snapshot) is None


def test_card_reconciliation_nonmandatory_or_other_failure_overflow_stays_blocking() -> None:
    selected = tuple(range(1, 72))
    metadata = tuple(
        SelectionMetadata(
            identity=f"existing:{note_id}",
            selected_position=note_id,
            tier=SelectionTier.T1,
            evidence_quality=EvidenceQuality.PRIMARY_SOURCE,
            mandatory=note_id == 71,
            marginal_value_reason=(
                MarginalValueReason.ONLY_VALID_REQUIRED_FACT if 66 <= note_id <= 70 else None
            ),
            overflow_reason="required fixture coverage" if note_id == 71 else None,
            manual_acknowledgement_required=note_id == 71,
        )
        for note_id in selected
    )
    snapshot = CardCentricReconciliationInput(
        pipeline_contract_version="card_centric_v2",
        concept_ids=("C01",),
        coverage={"C01": "covered"},
        required_fact_ids=(),
        uncovered_after_s5=(),
        residual_ran_for=(),
        generated_cards=(),
        terminal_resolutions=(),
        terminal_resolutions_provided=True,
        unresolved_fact_ids=(),
        expected_scoped_nids=selected,
        classifications=tuple(AuditResolution(nid=note_id, verdict="keep") for note_id in selected),
        eligible_yes_nids=selected,
        selected_nids=selected,
        selected_generated_card_ids=(),
        generated_card_ids=(),
        source_passage_ids=(),
        forbidden_cloze_targets=(),
        prompt_sync_stale=False,
        untagged_rate=0,
        mandatory_nids=(71,),
        covered_concept_ids_by_nid={note_id: ("C01",) for note_id in selected},
        selection_metadata=metadata,
        selection_order=tuple(item.identity for item in metadata),
        selected_count=71,
        below_warning_floor=False,
    )
    nonmandatory = snapshot.model_copy(
        update={
            "selection_metadata": (
                *metadata[:-1],
                metadata[-1].model_copy(update={"mandatory": False}),
            )
        }
    )
    other_failure = snapshot.model_copy(update={"eligible_yes_nids": selected[:-1]})

    for candidate in (nonmandatory, other_failure):
        report = reconcile_card_centric(candidate)
        assert stages_module._card_reconciliation_error(report, candidate) is not None
