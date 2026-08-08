import asyncio
import hashlib
import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

import oms_hub.anki.stages as stages_module
from oms_hub.anki.audit import AuditBatchV2, AuditCacheRecord
from oms_hub.anki.card_centric import build_snapshot_census, build_source_index
from oms_hub.anki.card_centric_contracts import (
    CardClassification,
    CardConcept,
    CardConceptLedger,
    CardRecord,
    ClassifierResult,
    ClassifierTelemetry,
    FastCardClassification,
    FastClassificationResult,
    SemanticPreFilterResult,
    TagScopeResult,
)
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
from oms_hub.anki.prompt_catalog import AnkiPromptCatalogService
from oms_hub.anki.prompts import AnkiPromptLibrary, StaticPromptSynchronizer
from oms_hub.anki.reconciliation import AssertionFinding, ReconciliationReport
from oms_hub.anki.semantic.domain import SemanticHit
from oms_hub.anki.sources import SourcePassage
from oms_hub.anki.stages import (
    CurationServicesRunner,
    _card_residual_targets,
    _effective_v2_fallback_note_ids,
    _pinned_card_v2_prompt,
    _priority_candidate_groups,
    _v2_card_candidates,
    _v2_reconciliation_classifications,
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
        return "Iron Deficiency Anemia"

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
        image_needed=None,
    )
    structured = V2GapStageStructuredService(
        GapBatchV2(
            resolutions=(
                generated,
                generated.model_copy(),
            )
        )
    )
    runner = CurationServicesRunner.__new__(CurationServicesRunner)
    runner.structured = structured
    runner.repository = GapStageRepository()
    runner.companion = MultipleCompanionNotes(())
    runner.embedder = SimpleNamespace()
    context = SimpleNamespace(
        job=SimpleNamespace(
            id="job-1",
            lecture_id=12,
            gap_prompt_version="gap-card-generation",
            provider="openai",
            model="gpt-5.6-terra",
        ),
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
    assert sent["forbidden_cloze_targets"] == [
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
