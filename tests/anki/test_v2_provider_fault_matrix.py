"""P4-B provider/semantic fault matrix for card_centric_v2."""

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from oms_hub.anki.card_centric import CardCentricClassifier, CardCentricValidationError
from oms_hub.anki.card_centric_contracts import (
    CardClassification,
    CardClassificationBatchOutput,
    CardConcept,
    CardConceptLedger,
    CardRecord,
    ClassifierResult,
    ClassifierTelemetry,
    FastCardClassification,
    FastClassificationResult,
    GeneratedCardResolution,
)
from oms_hub.anki.dedupe import DeduplicationService
from oms_hub.anki.domain import CurationStage, PipelineContractVersion, SourceKind
from oms_hub.anki.semantic.voyage import VoyageEmbeddingError
from oms_hub.anki.sources import SourcePassage
from oms_hub.anki.stages import CurationServicesRunner
from oms_hub.anki.worker import _is_retryable
from oms_hub.llm.domain import DiagnosticSource, ProviderName
from tests.anki.fixtures.card_centric_v2_faults import (
    RETRYABLE_PROVIDER_SOURCES,
    CountingOutageEmbeddingClient,
    FaultingEmbeddingClient,
    FaultingStructuredService,
    InvalidEmbeddingClient,
    malformed_structured_output,
    provider_fault,
)


def _source() -> tuple[object, CardRecord, CardConceptLedger]:
    from oms_hub.anki.card_centric import build_source_index

    passage = SourcePassage.create(
        revision_id=7,
        lecture_id=12,
        artifact_id="slides-7",
        source_kind=SourceKind.SLIDE,
        locator="slide:1",
        text="Heme synthesis begins in mitochondria.",
        slide_number=1,
    )
    source = build_source_index(
        (passage,), snapshot_id="snapshot-1", source_revision_hashes={7: "a" * 64}
    )
    card = CardRecord(
        note_id=1,
        content_sha256="b" * 64,
        text="Heme synthesis begins in mitochondria.",
        extra="",
        tags=("#heme",),
        deck_names=("AnKing",),
    )
    ledger = CardConceptLedger(
        lecture_entity_count=1,
        concepts=(
            CardConcept(
                concept_id="C01",
                canonical_statement="Heme synthesis begins in mitochondria.",
                primary_entity="heme synthesis",
                depth="deep",
                emphasis_flag=True,
                importance="high",
            ),
        ),
    )
    return source, card, ledger


@pytest.mark.parametrize(
    ("fault", "retryable"),
    [
        (TimeoutError("fixture timeout"), True),
        *((provider_fault(source), True) for source in RETRYABLE_PROVIDER_SOURCES),
        (malformed_structured_output(provider=ProviderName.OPENAI, model="fixture"), True),
        (provider_fault(DiagnosticSource.REQUEST), False),
    ],
    ids=("timeout", "network", "quota-rate-limit", "service", "malformed", "other"),
)
def test_s2_real_handler_propagates_provider_fault_to_worker(
    fault: Exception, retryable: bool
) -> None:
    """S2 invokes the real ledger handler before worker exception classification."""
    from oms_hub.anki.card_centric import build_source_index
    from oms_hub.anki.prompts import AnkiPromptLibrary

    summary = SourcePassage.create(
        revision_id=8,
        lecture_id=12,
        artifact_id="summary-8",
        source_kind=SourceKind.SUMMARY,
        locator="summary:1",
        text="Heme synthesis begins in mitochondria.",
    )
    source = build_source_index(
        (summary,), snapshot_id="snapshot-1", source_revision_hashes={8: "a" * 64}
    )
    prompt = AnkiPromptLibrary().load("card-centric-ledger-v2")
    runner = CurationServicesRunner.__new__(CurationServicesRunner)
    runner.structured = FaultingStructuredService(fault=fault)
    context = SimpleNamespace(
        job=SimpleNamespace(
            pipeline_contract_version=PipelineContractVersion.CARD_CENTRIC_V2,
            resolved_model_config=SimpleNamespace(
                ledger_s2=SimpleNamespace(provider="openai", model="fixture")
            ),
        ),
        prior_payloads={
            CurationStage.PREFLIGHT: {
                "prompt_snapshot": [
                    {
                        "id": prompt.metadata.id,
                        "version": prompt.metadata.version,
                        "prompt_hash": prompt.prompt_hash,
                        "content": prompt.content,
                        "metadata": prompt.metadata.model_dump(mode="json", by_alias=True),
                    }
                ]
            },
            CurationStage.SOURCE_INDEX: {"source_index": source.model_dump(mode="json")},
        },
    )

    with pytest.raises(type(fault)) as raised:
        asyncio.run(runner._card_ledger(context))

    assert raised.value is fault
    assert _is_retryable(raised.value) is retryable


@pytest.mark.parametrize(
    ("rows", "message"),
    [
        ((), "exactly partition"),
        (
            (
                CardClassification(
                    note_id=2,
                    verdict="NO",
                    primary_subject="fixture",
                    reason="invented identity",
                ),
            ),
            "exactly partition",
        ),
        (
            (
                CardClassification(
                    note_id=1,
                    verdict="NO",
                    primary_subject="fixture",
                    reason="first duplicate",
                ),
                CardClassification(
                    note_id=1,
                    verdict="NO",
                    primary_subject="fixture",
                    reason="second duplicate",
                ),
            ),
            "exactly partition",
        ),
        (
            (
                CardClassification(
                    note_id=1,
                    verdict="YES",
                    primary_subject="fixture",
                    reason="ungrounded",
                ),
            ),
            "ungrounded YES",
        ),
        (
            (
                CardClassification(
                    note_id=1,
                    verdict="NO",
                    primary_subject="fixture",
                    reason="invented concept",
                    covered_concept_ids=("C99",),
                ),
            ),
            "invented a concept ID",
        ),
        (
            (
                CardClassification(
                    note_id=1,
                    verdict="NO",
                    primary_subject="fixture",
                    reason="invented passage",
                    supporting_passage_ids=("P99",),
                ),
            ),
            "invented a supporting passage ID",
        ),
    ],
)
def test_s4c_s6_schema_valid_invalid_rows_block_before_eligibility(
    rows: tuple[CardClassification, ...], message: str
) -> None:
    source, card, _ledger = _source()
    classifier = CardCentricClassifier(
        SimpleNamespace(),
        instruction="fixture",
        capabilities=SimpleNamespace(prompt_prefix_caching=False),
    )

    with pytest.raises(CardCentricValidationError, match=message):
        classifier.validate_output(
            CardClassificationBatchOutput(results=rows),
            cards=(card,),
            source_index=source,
            concept_ids=("C01",),
        )


@pytest.mark.parametrize("stage", ("S4c", "S6"))
@pytest.mark.parametrize(
    ("fault", "retryable"),
    [
        (TimeoutError("fixture timeout"), True),
        *((provider_fault(source), True) for source in RETRYABLE_PROVIDER_SOURCES),
        (malformed_structured_output(provider=ProviderName.OPENAI, model="fixture"), True),
        (provider_fault(DiagnosticSource.REQUEST), False),
    ],
    ids=("timeout", "network", "quota-rate-limit", "service", "malformed", "other"),
)
def test_s4c_s6_classifier_adapter_propagates_provider_fault_to_worker(
    stage: str, fault: Exception, retryable: bool
) -> None:
    """Both thorough paths invoke the shared production classifier adapter."""
    source, card, _ledger = _source()
    classifier = CardCentricClassifier(
        FaultingStructuredService(fault=fault),
        instruction=f"fixture {stage}",
        capabilities=SimpleNamespace(prompt_prefix_caching=False),
    )

    with pytest.raises(type(fault)) as raised:
        asyncio.run(
            classifier.classify(
                (card,),
                source_index=source,
                concept_ids=("C01",),
                provider=ProviderName.OPENAI,
                model="fixture",
            )
        )

    assert raised.value is fault
    assert _is_retryable(raised.value) is retryable


@pytest.mark.parametrize(
    ("mode", "expected_reason", "retryable"),
    [
        ("malformed", "structured_output_invalid", True),
        ("partial", "partition_mismatch", None),
        ("extra", "partition_mismatch", None),
        ("ungrounded", "ungrounded_likely_yes", None),
        ("timeout", None, True),
        ("network", None, True),
        ("quota-rate-limit", None, True),
        ("service", None, True),
        ("other", None, False),
    ],
)
def test_s4b_optional_faults_degrade_the_complete_batch(
    tmp_path: Path, mode: str, expected_reason: str | None, retryable: bool | None
) -> None:
    from oms_hub.anki.card_centric_contracts import SemanticPreFilterResult, TagScopeResult
    from oms_hub.anki.prompt_catalog import AnkiPromptCatalogService
    from oms_hub.anki.prompts import AnkiPromptLibrary

    source, card, ledger = _source()
    fast_prompt = AnkiPromptLibrary().load("card-centric-fast-classifier")
    normal = FastCardClassification(
        note_id=1,
        verdict="LIKELY_YES",
        grounded_concept_ids=("C01",),
        supporting_passage_ids=(source.passages[0].passage_id,),
        reason="grounded",
    )
    if mode == "malformed":
        service = FaultingStructuredService(
            fault=malformed_structured_output(provider=ProviderName.OPENAI, model="fixture")
        )
    elif mode in {"timeout", "network", "quota-rate-limit", "service", "other"}:
        fault = (
            TimeoutError("fixture timeout")
            if mode == "timeout"
            else provider_fault(
                {
                    "network": DiagnosticSource.NETWORK,
                    "quota-rate-limit": DiagnosticSource.QUOTA,
                    "service": DiagnosticSource.SERVICE,
                    "other": DiagnosticSource.REQUEST,
                }[mode]
            )
        )
        service = FaultingStructuredService(fault=fault)
    else:
        rows = (normal,)
        if mode == "partial":
            rows = ()
        elif mode == "extra":
            rows = (normal, normal.model_copy(update={"note_id": 2}))
        elif mode == "ungrounded":
            rows = (
                normal.model_copy(
                    update={"grounded_concept_ids": (), "supporting_passage_ids": ()}
                ),
            )
        service = FaultingStructuredService(value=FastClassificationResult(results=rows))
    runner = CurationServicesRunner.__new__(CurationServicesRunner)
    runner.structured = service
    runner.prompts = AnkiPromptCatalogService(bundled_directory=tmp_path)
    context = SimpleNamespace(
        job=SimpleNamespace(
            pipeline_contract_version=PipelineContractVersion.CARD_CENTRIC_V2,
            resolved_model_config=SimpleNamespace(
                fast_classify_s4b=SimpleNamespace(provider="openai", model="fixture"),
                canonical_document=lambda: {},
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
            CurationStage.CARD_PREFILTER: SemanticPreFilterResult(
                pre_filtered_note_ids=(1,),
                pre_excluded_note_ids=(),
                threshold=0.5,
                similarity_stats={"min": 0.5, "max": 0.5, "mean": 0.5, "median": 0.5},
            ).model_dump(mode="json"),
            CurationStage.CARD_TAG_SCOPE: {
                "scope": TagScopeResult(
                    snapshot_id="snapshot-1",
                    filters_sha256="c" * 64,
                    scoped_note_ids=(1,),
                    unscoped_note_ids=(),
                ).model_dump(mode="json")
            },
        },
    )

    if expected_reason is None and retryable is not None:
        with pytest.raises(type(service.fault)) as raised:
            asyncio.run(runner._card_fast_classify(context))
        assert raised.value is service.fault
        assert _is_retryable(raised.value) is retryable
        return

    product = asyncio.run(runner._card_fast_classify(context))

    assert product.payload["degraded_batches"] == [
        {"batch_index": 0, "note_ids": [1], "reason_code": expected_reason}
    ]
    assert product.payload["fast_classifier"]["results"][0]["verdict"] == "NEEDS_REVIEW"


def test_expected_red_p3_h10_exhausted_semantic_outage_requires_manual_review() -> None:
    """H-10: retry exhaustion must not auto-declare a generated card unique."""
    source, card, _ledger = _source()
    outage = VoyageEmbeddingError("fixture outage after retries")
    existing = CardClassification(
        note_id=card.note_id,
        verdict="YES",
        primary_subject="fixture",
        reason="grounded existing card",
        covered_concept_ids=("C01",),
        supporting_passage_ids=(source.passages[0].passage_id,),
    )
    telemetry = ClassifierTelemetry(
        batch_count=0,
        cache_prefix_sha256="c" * 64,
        cache_mode="ordinary_prefix",
        provider="openai",
        model="fixture",
        request_ids=(),
        batches=(),
    )
    generated = GeneratedCardResolution(
        card_id="G01",
        concept_id="C01",
        fact_id="C01-M1",
        text="Heme synthesis begins clinically in {{c1::mitochondria}}.",
        source_passage_ids=(source.passages[0].passage_id,),
        evidence_ids=("E01",),
    )
    outage_client = CountingOutageEmbeddingClient(outage)
    runner = CurationServicesRunner.__new__(CurationServicesRunner)
    runner.embedder = outage_client

    def context_for(stage_attempt_count: int) -> SimpleNamespace:
        return SimpleNamespace(
            # P1/P3 integration hook: worker stage attempt count is supplied
            # to the handler; the handler may degrade only at this exhaustion boundary.
            stage_attempt_count=stage_attempt_count,
            max_stage_attempts=3,
            job=SimpleNamespace(
                pipeline_contract_version=PipelineContractVersion.CARD_CENTRIC_V2,
                gap_prompt_version="fixture-gap",
                resolved_model_config=SimpleNamespace(
                    gap_fill_s7=SimpleNamespace(provider="openai", model="fixture")
                ),
            ),
            prior_payloads={
                CurationStage.SOURCE_INDEX: {
                    "source_index": source.model_dump(mode="json"),
                    "cards": [card.model_dump(mode="json")],
                },
                CurationStage.CARD_CLASSIFY: {
                    "classifier": ClassifierResult(
                        results=(existing,), telemetry=telemetry
                    ).model_dump(mode="json")
                },
                CurationStage.CARD_RESIDUAL: {"classifier": None},
                CurationStage.CARD_FAST_CLASSIFY: {
                    "fast_classifier": FastClassificationResult(results=()).model_dump(mode="json"),
                    "fallback_note_ids": [],
                },
                CurationStage.CARD_GAP_FILL: {"resolutions": [generated.model_dump(mode="json")]},
            },
        )

    for attempt in (1, 2):
        with pytest.raises(VoyageEmbeddingError, match="fixture outage after retries"):
            asyncio.run(runner._card_dedupe(context_for(attempt)))

    product = asyncio.run(runner._card_dedupe(context_for(3)))

    assert outage_client.calls == 3
    assert product.payload["resolutions"][0]["status"] == "needs_review"
    assert product.payload["manual_review_required"] is True
    assert product.payload["automatic_envelope_eligible"] is False


@pytest.mark.parametrize(
    ("fault", "retryable"),
    [
        (TimeoutError("fixture timeout"), True),
        (VoyageEmbeddingError("fixture service outage"), True),
        (provider_fault(DiagnosticSource.QUOTA), True),
        (provider_fault(DiagnosticSource.SERVICE), True),
        (provider_fault(DiagnosticSource.REQUEST), False),
    ],
    ids=("timeout", "voyage-service", "quota-rate-limit", "service", "other"),
)
def test_s8_dedupe_adapter_propagates_semantic_provider_fault_to_worker(
    fault: Exception, retryable: bool
) -> None:
    proposal = _proposal("C01", "proposed")
    comparison = _proposal("C02", "existing")
    deduper = DeduplicationService(FaultingEmbeddingClient(fault))

    with pytest.raises(type(fault)) as raised:
        asyncio.run(deduper.classify(proposal, (), (proposal, comparison)))

    assert raised.value is fault
    assert _is_retryable(raised.value) is retryable


@pytest.mark.parametrize(
    ("fault", "retryable"),
    [
        (TimeoutError("fixture timeout"), True),
        *((provider_fault(source), True) for source in RETRYABLE_PROVIDER_SOURCES),
        (malformed_structured_output(provider=ProviderName.OPENAI, model="fixture"), True),
        (provider_fault(DiagnosticSource.REQUEST), False),
    ],
    ids=("timeout", "network", "quota-rate-limit", "service", "malformed", "other"),
)
def test_s7_real_handler_propagates_provider_fault_to_worker(
    fault: Exception, retryable: bool
) -> None:
    """S7 invokes its real per-concept provider boundary before classification."""
    from oms_hub.anki.prompts import AnkiPromptLibrary

    source, _card, ledger = _source()
    prompt = AnkiPromptLibrary().load("card-centric-gap-v2")
    empty = ClassifierResult(
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
    runner = CurationServicesRunner.__new__(CurationServicesRunner)
    runner.structured = FaultingStructuredService(fault=fault)
    runner.repository = SimpleNamespace(lecture_title=lambda _lecture_id: "Heme synthesis")
    context = SimpleNamespace(
        job=SimpleNamespace(
            lecture_id=12,
            pipeline_contract_version=PipelineContractVersion.CARD_CENTRIC_V2,
            resolved_model_config=SimpleNamespace(
                gap_fill_s7=SimpleNamespace(provider="openai", model="fixture")
            ),
        ),
        prior_payloads={
            CurationStage.PREFLIGHT: {
                "prompt_snapshot": [
                    {
                        "id": prompt.metadata.id,
                        "version": prompt.metadata.version,
                        "prompt_hash": prompt.prompt_hash,
                        "content": prompt.content,
                        "metadata": prompt.metadata.model_dump(mode="json", by_alias=True),
                    }
                ]
            },
            CurationStage.SOURCE_INDEX: {"source_index": source.model_dump(mode="json")},
            CurationStage.CARD_LEDGER: {"ledger": ledger.model_dump(mode="json")},
            CurationStage.CARD_COVERAGE: {
                "coverage": {"C01": {"status": "uncovered", "evidence": []}}
            },
            CurationStage.CARD_CLASSIFY: {"classifier": empty.model_dump(mode="json")},
            CurationStage.CARD_RESIDUAL: {"classifier": None},
            CurationStage.CARD_FAST_CLASSIFY: {
                "fast_classifier": FastClassificationResult(results=()).model_dump(mode="json"),
                "fallback_note_ids": [],
            },
        },
    )

    with pytest.raises(type(fault)) as raised:
        asyncio.run(runner._card_gap_fill(context))

    assert raised.value is fault
    assert _is_retryable(raised.value) is retryable


@pytest.mark.parametrize(
    "matrix",
    [
        ((1.0, 0.0),),
        ((float("nan"), 0.0), (1.0, 0.0)),
        ((0.0, 0.0), (1.0, 0.0)),
    ],
)
def test_s8_invalid_vectors_are_integrity_failures(matrix: tuple[tuple[float, ...], ...]) -> None:
    deduper = DeduplicationService(InvalidEmbeddingClient(matrix))
    proposal = _proposal("C01", "proposed")
    comparison = _proposal("C02", "existing")

    with pytest.raises(ValueError, match="deduplication embeddings"):
        asyncio.run(deduper.classify(proposal, (), (proposal, comparison)))


def _proposal(concept_id: str, text: str):
    from oms_hub.anki.gaps import GapCardProposal

    return GapCardProposal(
        concept_id=concept_id,
        note_type="Cloze",
        fields={"Text": text, "Extra": ""},
        source_refs=(),
        evidence_ids=("E01",),
        initial_tags=(),
        provider=ProviderName.OPENAI,
        model="fixture",
        prompt_version="fixture",
        confidence=1.0,
        content_hash="a" * 64,
        provenance={},
    )
