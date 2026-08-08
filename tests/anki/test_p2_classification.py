import asyncio
import hashlib
import json
import time
from types import SimpleNamespace

import pytest

from oms_hub.anki.card_centric import (
    CardCentricClassifier,
    CardCentricValidationError,
    build_source_index,
)
from oms_hub.anki.card_centric_contracts import (
    CardClassification,
    CardClassificationBatchOutput,
    CardConcept,
    CardConceptLedger,
    CardRecord,
    FastCardClassification,
    FastClassificationResult,
    SemanticPreFilterResult,
    TagScopeResult,
)
from oms_hub.anki.domain import (
    CurationStage,
    PipelineContractVersion,
    ResolvedClassifierExecution,
    ResolvedModelConfiguration,
    SourceKind,
)
from oms_hub.anki.sources import SourcePassage
from oms_hub.anki.stages import CurationServicesRunner, _classifier_generation_parameters
from oms_hub.llm.domain import DiagnosticSource, GeneratedText, LLMRequestError, ProviderName
from oms_hub.llm.structured import StructuredTextService


def _card(note_id: int) -> CardRecord:
    return CardRecord(
        note_id=note_id,
        content_sha256=f"{note_id:064x}",
        text=f"Card {note_id}",
        extra="",
        tags=("#AK_Step::Heme",),
        deck_names=("AnKing",),
    )


def test_p2_classifier_execution_has_a_canonical_identity_seam() -> None:
    execution = ResolvedClassifierExecution()
    configuration = ResolvedModelConfiguration.card_centric_v2_default(
        "anthropic", "configured-model"
    )

    assert execution.canonical_document() == {
        "fast_batch_size": 60,
        "fast_concurrency": 4,
        "thorough_batch_size": 30,
        "thorough_concurrency": 4,
        "thorough_retry_attempts": 2,
        "thinking_budget_tokens": 1024,
    }
    assert len(execution.generation_parameters_sha256()) == 64
    assert configuration.resolved_classifier_execution() == execution
    # P1/I0 owns adding this typed field to persisted job canonical documents.
    assert "classifier_execution" not in configuration.canonical_document()
    assert _classifier_generation_parameters(
        "openai", "configured-model", execution, prompt_id="card-centric-classifier"
    ) == {
        "provider": "openai",
        "model": "configured-model",
        "prompt_id": "card-centric-classifier",
        "execution": execution.canonical_document(),
        "execution_sha256": execution.generation_parameters_sha256(),
        "generation_options": {
            "cacheable_source_prefix": True,
            "thinking": "disabled",
            "thinking_budget_tokens": 1024,
        },
    }


def _source():
    passage = SourcePassage.create(
        revision_id=7,
        lecture_id=12,
        artifact_id="slides-7",
        source_kind=SourceKind.SLIDE,
        locator="slide:1",
        text="Factor deficiency prolongs the assay.",
        slide_number=1,
    )
    return build_source_index(
        (passage,), snapshot_id="snapshot-1", source_revision_hashes={7: "a" * 64}
    )


def _fast_context(cards: tuple[CardRecord, ...]):
    source = _source()
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
    content = "Pinned fast classifier"
    prompt = {
        "id": "card-centric-fast-classifier",
        "version": "2.0.0",
        "prompt_hash": hashlib.sha256(content.encode()).hexdigest()[:12],
        "content": content,
        "metadata": {
            "id": "card-centric-fast-classifier",
            "version": "2.0.0",
            "schema": "card_centric_fast_classify_v2",
            "response_format": "json",
        },
    }
    note_ids = tuple(card.note_id for card in cards)
    return SimpleNamespace(
        job=SimpleNamespace(
            pipeline_contract_version=PipelineContractVersion.CARD_CENTRIC_V2,
            resolved_model_config=ResolvedModelConfiguration.card_centric_v2_default(
                "anthropic", "configured-model"
            ),
        ),
        prior_payloads={
            CurationStage.PREFLIGHT: {"prompt_snapshot": [prompt]},
            CurationStage.SOURCE_INDEX: {
                "source_index": source.model_dump(mode="json"),
                "cards": [card.model_dump(mode="json") for card in cards],
            },
            CurationStage.CARD_LEDGER: {"ledger": ledger.model_dump(mode="json")},
            CurationStage.CARD_PREFILTER: SemanticPreFilterResult(
                pre_filtered_note_ids=note_ids,
                pre_excluded_note_ids=(),
                threshold=0.42,
                similarity_stats={"min": 0.8, "max": 0.9, "mean": 0.85, "median": 0.85},
            ).model_dump(mode="json"),
            CurationStage.CARD_TAG_SCOPE: {
                "scope": TagScopeResult(
                    snapshot_id="snapshot-1",
                    filters_sha256="b" * 64,
                    scoped_note_ids=note_ids,
                    unscoped_note_ids=(),
                ).model_dump(mode="json")
            },
        },
    )


class _FastService:
    def __init__(self, *, invalid_first_batch: bool = False, error: DiagnosticSource | None = None):
        self.invalid_first_batch = invalid_first_batch
        self.error = error
        self.completed: list[int] = []

    def generate_json(self, _instruction, input_text, **kwargs):
        cards = json.loads(input_text)["cards"]
        first_id = cards[0]["note_id"]
        if self.error is not None:
            raise LLMRequestError("provider fault", source=self.error)
        if first_id == 1:
            time.sleep(0.03)
        if self.invalid_first_batch and first_id == 1:
            rows = tuple(
                FastCardClassification(
                    note_id=card["note_id"], verdict="LIKELY_NO", reason="not taught"
                )
                for card in cards[:-1]
            )
        else:
            rows = tuple(
                FastCardClassification(
                    note_id=card["note_id"], verdict="LIKELY_NO", reason="not taught"
                )
                for card in cards
            )
        self.completed.append(first_id)
        return SimpleNamespace(
            value=FastClassificationResult(results=rows),
            request_id=f"fast-{first_id}",
            input_tokens=10,
            output_tokens=5,
            cost_microusd=1,
        )


def test_p2_s4b_batches_concurrently_and_aggregates_by_note_id() -> None:
    cards = tuple(_card(note_id) for note_id in range(1, 62))
    service = _FastService()
    runner = CurationServicesRunner.__new__(CurationServicesRunner)
    runner.structured = service

    product = asyncio.run(runner._card_fast_classify(_fast_context(cards)))

    assert service.completed == [61, 1]
    assert [row["note_id"] for row in product.payload["fast_classifier"]["results"]] == list(
        range(1, 62)
    )
    assert product.payload["degraded_batches"] == []
    assert product.payload["classifier_execution"] == _classifier_generation_parameters(
        "openai",
        "gpt-4o-mini",
        ResolvedClassifierExecution(),
        prompt_id="card-centric-fast-classifier",
    )


def test_p2_s4b_degrades_the_entire_invalid_batch() -> None:
    cards = tuple(_card(note_id) for note_id in range(1, 62))
    runner = CurationServicesRunner.__new__(CurationServicesRunner)
    runner.structured = _FastService(invalid_first_batch=True)

    product = asyncio.run(runner._card_fast_classify(_fast_context(cards)))

    rows = product.payload["fast_classifier"]["results"]
    assert [(row["note_id"], row["verdict"]) for row in rows[:60]] == [
        (note_id, "NEEDS_REVIEW") for note_id in range(1, 61)
    ]
    assert product.payload["degraded_batches"] == [
        {"batch_index": 0, "note_ids": list(range(1, 61)), "reason_code": "partition_mismatch"}
    ]


@pytest.mark.parametrize(
    "source",
    (DiagnosticSource.NETWORK, DiagnosticSource.QUOTA, DiagnosticSource.SERVICE),
)
def test_p2_s4b_provider_faults_propagate(source: DiagnosticSource) -> None:
    runner = CurationServicesRunner.__new__(CurationServicesRunner)
    runner.structured = _FastService(error=source)

    with pytest.raises(LLMRequestError, match="provider fault"):
        asyncio.run(runner._card_fast_classify(_fast_context((_card(1),))))


class _ThoroughGenerator:
    def __init__(self, outputs: list[str]):
        self.outputs = outputs
        self.instructions: list[str] = []

    def generate_text(self, instruction, _input_text, *, provider, model, **_kwargs):
        self.instructions.append(instruction)
        return GeneratedText(
            text=self.outputs.pop(0),
            provider=provider,
            model=model,
            request_id=f"thorough-{len(self.instructions)}",
            input_tokens=10,
            output_tokens=5,
            cost_microusd=1,
        )


def _classifier(generator: _ThoroughGenerator) -> CardCentricClassifier:
    return CardCentricClassifier(StructuredTextService(generator), batch_size=30, concurrency=1)


def test_p2_s4c_s6_retry_once_then_accepts_a_repaired_batch() -> None:
    invalid = CardClassificationBatchOutput(
        results=(
            CardClassification(note_id=1, verdict="YES", primary_subject="factor", reason="taught"),
        )
    ).model_dump_json()
    valid = CardClassificationBatchOutput(
        results=(
            CardClassification(
                note_id=1,
                verdict="NO",
                primary_subject="factor",
                reason="not taught",
            ),
        )
    ).model_dump_json()
    generator = _ThoroughGenerator([invalid, valid])

    result = asyncio.run(
        _classifier(generator).classify(
            (_card(1),),
            source_index=_source(),
            concept_ids=(),
            provider=ProviderName.OPENAI,
            model="configured-model",
        )
    )

    assert result.results[0].verdict == "NO"
    assert len(generator.instructions) == 2
    assert "repair" in generator.instructions[1].casefold()


def test_p2_s4c_s6_retries_once_then_blocks_invalid_output() -> None:
    invalid = CardClassificationBatchOutput(
        results=(
            CardClassification(note_id=1, verdict="YES", primary_subject="factor", reason="taught"),
        )
    ).model_dump_json()
    generator = _ThoroughGenerator([invalid, invalid])

    with pytest.raises(CardCentricValidationError, match="ungrounded YES"):
        asyncio.run(
            _classifier(generator).classify(
                (_card(1),),
                source_index=_source(),
                concept_ids=(),
                provider=ProviderName.OPENAI,
                model="configured-model",
            )
        )
    assert len(generator.instructions) == 2


@pytest.mark.parametrize("invalid_kind", ("partition", "invented_passage", "ungrounded_yes"))
def test_p2_s4c_s6_schema_valid_invalid_output_retries_once_then_blocks(
    invalid_kind: str,
) -> None:
    if invalid_kind == "partition":
        invalid = CardClassificationBatchOutput(results=()).model_dump_json()
        error = "exactly partition"
    elif invalid_kind == "invented_passage":
        invalid = CardClassificationBatchOutput(
            results=(
                CardClassification(
                    note_id=1,
                    verdict="YES",
                    primary_subject="factor",
                    reason="taught",
                    supporting_passage_ids=("SLD:invented",),
                ),
            )
        ).model_dump_json()
        error = "invented"
    else:
        invalid = CardClassificationBatchOutput(
            results=(
                CardClassification(
                    note_id=1,
                    verdict="YES",
                    primary_subject="factor",
                    reason="taught",
                ),
            )
        ).model_dump_json()
        error = "ungrounded"
    generator = _ThoroughGenerator([invalid, invalid])

    with pytest.raises(CardCentricValidationError, match=error):
        asyncio.run(
            _classifier(generator).classify(
                (_card(1),),
                source_index=_source(),
                concept_ids=(),
                provider=ProviderName.OPENAI,
                model="configured-model",
            )
        )
    assert len(generator.instructions) == 2


class _FaultingThoroughGenerator:
    def __init__(self, source: DiagnosticSource):
        self.source = source

    def generate_text(self, *_args, **_kwargs):
        raise LLMRequestError("thorough provider fault", source=self.source)


@pytest.mark.parametrize(
    "source",
    (DiagnosticSource.NETWORK, DiagnosticSource.QUOTA, DiagnosticSource.SERVICE),
)
def test_p2_s4c_s6_provider_faults_propagate(source: DiagnosticSource) -> None:
    classifier = CardCentricClassifier(StructuredTextService(_FaultingThoroughGenerator(source)))

    with pytest.raises(LLMRequestError, match="thorough provider fault"):
        asyncio.run(
            classifier.classify(
                (_card(1),),
                source_index=_source(),
                concept_ids=(),
                provider=ProviderName.OPENAI,
                model="configured-model",
            )
        )


def test_p2_thorough_summary_grounded_yes_is_admissible() -> None:
    summary = SourcePassage.create(
        revision_id=7,
        lecture_id=12,
        artifact_id="outline-7",
        source_kind=SourceKind.SUMMARY,
        locator="summary:1",
        text="Factor deficiency prolongs the assay.",
        source_id="SUM:12:CORE:01",
        summary_section="core",
    )
    source = build_source_index(
        (summary,), snapshot_id="snapshot-1", source_revision_hashes={7: "a" * 64}
    )
    accepted = CardClassificationBatchOutput(
        results=(
            CardClassification(
                note_id=1,
                verdict="YES",
                primary_subject="factor",
                reason="summary supports the tested fact",
                supporting_passage_ids=(source.passages[0].passage_id,),
            ),
        )
    ).model_dump_json()

    result = asyncio.run(
        _classifier(_ThoroughGenerator([accepted])).classify(
            (_card(1),),
            source_index=source,
            concept_ids=(),
            provider=ProviderName.OPENAI,
            model="configured-model",
        )
    )
    assert result.results[0].supporting_passage_ids == (source.passages[0].passage_id,)
