import asyncio
from types import SimpleNamespace

from oms_hub.anki.card_centric import (
    build_source_index,
    evidence_quality_v2,
    selection_eligible_v2,
)
from oms_hub.anki.card_centric_contracts import (
    CardClassification,
    CardConcept,
    CardConceptLedger,
    CardEvidenceAudit,
    ClassifierResult,
    ClassifierTelemetry,
    FastClassificationResult,
)
from oms_hub.anki.correction_contracts import EvidenceQuality
from oms_hub.anki.domain import CurationStage, PipelineContractVersion, SourceKind
from oms_hub.anki.sources import SourcePassage
from oms_hub.anki.stages import CurationServicesRunner, _merged_card_coverage


def _passage(kind: SourceKind, locator: str, text: str) -> SourcePassage:
    return SourcePassage.create(
        revision_id=7,
        lecture_id=12,
        artifact_id=f"{kind.value}-7",
        source_kind=kind,
        locator=locator,
        text=text,
        slide_number=1 if kind is SourceKind.SLIDE else None,
    )


def _source_index(*passages: SourcePassage):
    return build_source_index(
        passages,
        snapshot_id="snapshot-1",
        source_revision_hashes={7: "a" * 64},
    )


def _concept(
    concept_id: str,
    primary_entity: str,
    aliases: tuple[str, ...] = (),
) -> CardConcept:
    return CardConcept(
        concept_id=concept_id,
        canonical_statement=f"{primary_entity} is testable.",
        primary_entity=primary_entity,
        aliases=aliases,
        depth="deep",
        emphasis_flag=False,
        importance="high",
    )


def _classifier(*results: CardClassification) -> ClassifierResult:
    return ClassifierResult(
        results=results,
        telemetry=ClassifierTelemetry(
            batch_count=0,
            cache_prefix_sha256="b" * 64,
            cache_mode="ephemeral",
            provider="fixture",
            model="fixture",
            request_ids=(),
            batches=(),
        ),
    )


def test_s2b_audit_normalizes_unicode_boundaries_and_preserves_diagnostics() -> None:
    beta_slide = _passage(
        SourceKind.SLIDE,
        "slide:1",
        "The β‑blocker therapy reduces sympathetic tone.",
    )
    nonmatch_slide = _passage(
        SourceKind.SLIDE,
        "slide:2",
        "Partial agonism is unrelated.",
    )
    phrase_slide = _passage(
        SourceKind.SLIDE,
        "slide:3",
        "Renin—angiotensin system regulates pressure.",
    )
    source = _source_index(beta_slide, nonmatch_slide, phrase_slide)
    ledger = CardConceptLedger(
        concepts=(
            _concept("C01", "β blocker", aliases=("β-blocker",)),
            _concept("C02", "art"),
            _concept("C03", "RAAS", aliases=("renin angiotensin system",)),
        ),
        lecture_entity_count=3,
    )
    context = SimpleNamespace(
        prior_payloads={
            CurationStage.SOURCE_INDEX: {"source_index": source.model_dump(mode="json")},
            CurationStage.CARD_LEDGER: {"ledger": ledger.model_dump(mode="json")},
        }
    )
    runner = CurationServicesRunner.__new__(CurationServicesRunner)

    product = asyncio.run(runner._card_evidence_audit(context))
    audit = CardEvidenceAudit.model_validate(product.payload)
    passage_id_by_text = {passage.text: passage.passage_id for passage in source.passages}

    assert audit.matched_slide_passage_ids == {
        "C01": (passage_id_by_text[beta_slide.text],),
        "C02": (),
        "C03": (passage_id_by_text[phrase_slide.text],),
    }
    assert audit.matched_slide_char_counts == {"C01": 47, "C02": 0, "C03": 44}
    assert audit.total_concepts == 3
    assert audit.evidence_poor_concept_ids == ("C01", "C02", "C03")


def test_v2_evidence_quality_requires_only_valid_citations_and_preserves_summary_status() -> None:
    summary = _passage(SourceKind.SUMMARY, "summary:1", "Summary evidence.")
    slide = _passage(SourceKind.SLIDE, "slide:1", "Primary slide evidence.")
    source = _source_index(summary, slide)
    summary_id, slide_id = (passage.passage_id for passage in source.passages)
    summary_only = CardClassification(
        note_id=1,
        verdict="YES",
        primary_subject="fixture",
        reason="summary-grounded",
        supporting_passage_ids=(summary_id,),
    )
    primary = CardClassification(
        note_id=2,
        verdict="YES",
        primary_subject="fixture",
        reason="primary-source",
        supporting_passage_ids=(summary_id, slide_id),
    )
    invalid = CardClassification(
        note_id=3,
        verdict="YES",
        primary_subject="fixture",
        reason="invalid citation",
        supporting_passage_ids=(summary_id, "SLD:missing:P:000"),
    )

    assert evidence_quality_v2(summary_only, source) is EvidenceQuality.SUMMARY_GROUNDED
    assert selection_eligible_v2(summary_only, source)
    assert evidence_quality_v2(primary, source) is EvidenceQuality.PRIMARY_SOURCE
    assert evidence_quality_v2(invalid, source) is None
    assert not selection_eligible_v2(invalid, source)


def test_v2_coverage_labels_thorough_rows_and_preserves_residual_label() -> None:
    summary = _passage(SourceKind.SUMMARY, "summary:1", "Summary evidence.")
    slide = _passage(SourceKind.SLIDE, "slide:1", "Primary slide evidence.")
    source = _source_index(summary, slide)
    summary_id, slide_id = (passage.passage_id for passage in source.passages)
    ledger = CardConceptLedger(
        concepts=(_concept("C01", "summary"), _concept("C02", "primary")),
        lecture_entity_count=2,
    )
    thorough = _classifier(
        CardClassification(
            note_id=1,
            verdict="YES",
            primary_subject="fixture",
            reason="summary-grounded",
            covered_concept_ids=("C01",),
            supporting_passage_ids=(summary_id,),
        ),
        CardClassification(
            note_id=2,
            verdict="YES",
            primary_subject="fixture",
            reason="primary-source",
            covered_concept_ids=("C02",),
            supporting_passage_ids=(slide_id,),
        ),
    )
    context = SimpleNamespace(
        job=SimpleNamespace(pipeline_contract_version=PipelineContractVersion.CARD_CENTRIC_V2),
        prior_payloads={
            CurationStage.SOURCE_INDEX: {"source_index": source.model_dump(mode="json")},
            CurationStage.CARD_LEDGER: {"ledger": ledger.model_dump(mode="json")},
            CurationStage.CARD_CLASSIFY: {"classifier": thorough.model_dump(mode="json")},
            CurationStage.CARD_FAST_CLASSIFY: {
                "fast_classifier": FastClassificationResult(results=()).model_dump(mode="json")
            },
        },
    )
    runner = CurationServicesRunner.__new__(CurationServicesRunner)

    product = asyncio.run(runner._card_coverage(context))

    assert product.payload["coverage"]["C01"]["evidence"][0]["evidence_quality"] == (
        "summary_grounded"
    )
    assert product.payload["coverage"]["C02"]["evidence"][0]["evidence_quality"] == (
        "primary_source"
    )

    residual = _classifier(
        CardClassification(
            note_id=3,
            verdict="YES",
            primary_subject="fixture",
            reason="summary residual",
            covered_concept_ids=("C01",),
            supporting_passage_ids=(summary_id,),
        )
    )
    merged_context = SimpleNamespace(
        job=context.job,
        prior_payloads={
            CurationStage.SOURCE_INDEX: {"source_index": source.model_dump(mode="json")},
            CurationStage.CARD_COVERAGE: {
                "coverage": {
                    "C01": {"status": "uncovered", "evidence": []},
                    "C02": {"status": "uncovered", "evidence": []},
                }
            },
            CurationStage.CARD_CLASSIFY: {"classifier": _classifier().model_dump(mode="json")},
            CurationStage.CARD_RESIDUAL: {"classifier": residual.model_dump(mode="json")},
            CurationStage.CARD_FAST_CLASSIFY: {
                "fast_classifier": FastClassificationResult(results=()).model_dump(mode="json")
            },
        },
    )

    merged = _merged_card_coverage(merged_context)

    assert merged["C01"]["evidence"] == [
        {
            "note_id": 3,
            "supporting_passage_ids": [summary_id],
            "evidence_quality": "summary_grounded",
        }
    ]
