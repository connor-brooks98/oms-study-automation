import random

from oms_hub.anki.card_centric import build_source_index, select_high_yield_v2
from oms_hub.anki.card_centric_contracts import (
    CardCentricSourceIndex,
    CardClassification,
    CardConcept,
    CardConceptLedger,
    FastCardClassification,
    GeneratedCardResolution,
)
from oms_hub.anki.correction_contracts import (
    EvidenceQuality,
    MarginalValueReason,
    SelectionTier,
)
from oms_hub.anki.domain import SourceKind
from oms_hub.anki.sources import SourcePassage


def _source() -> CardCentricSourceIndex:
    return build_source_index(
        [
            SourcePassage.create(
                revision_id=7,
                lecture_id=12,
                artifact_id="upload-7",
                source_kind=SourceKind.SLIDE,
                locator="slide:1",
                text="primary evidence",
                slide_number=1,
            ),
            SourcePassage.create(
                revision_id=7,
                lecture_id=12,
                artifact_id="outline-7",
                source_kind=SourceKind.SUMMARY,
                locator="summary:1",
                text="summary evidence",
                source_id="SUM:12:CORE:01",
                summary_section="core",
            ),
        ],
        snapshot_id="snapshot-1",
        source_revision_hashes={7: "a" * 64},
    )


def _ledger(
    count: int,
    *,
    high: set[int] = frozenset(),
    emphasized: set[int] = frozenset(),
    low: set[int] = frozenset(),
) -> CardConceptLedger:
    return CardConceptLedger(
        lecture_entity_count=count,
        concepts=tuple(
            CardConcept(
                concept_id=f"C{index:02d}",
                canonical_statement=f"Fact {index}",
                primary_entity=f"Entity {index}",
                depth="deep"
                if index in high or index in emphasized
                else "surface"
                if index in low
                else "medium",
                emphasis_flag=index in emphasized,
                importance="high" if index in high else "low" if index in low else "medium",
            )
            for index in range(1, count + 1)
        ),
    )


def _generated(index: int, passage_id: str, *, high: bool = False) -> GeneratedCardResolution:
    concept_id = f"C{index:02d}"
    return GeneratedCardResolution(
        card_id=f"G{index:02d}",
        concept_id=concept_id,
        fact_id=f"{concept_id}-M1",
        text=f"{{{{c1::Fact {index}}}}}",
        source_passage_ids=(passage_id,),
        evidence_ids=(f"E{index}",),
    )


def test_selector_uses_tier_order_and_is_stable_when_inputs_are_shuffled() -> None:
    source = _source()
    passage_id = source.passages[0].passage_id
    ledger = _ledger(6, high={1, 3}, low={4})
    generated = (_generated(1, passage_id), _generated(2, passage_id), _generated(4, passage_id))
    thorough = (
        CardClassification(
            note_id=30,
            verdict="YES",
            primary_subject="fixture",
            reason="high existing",
            covered_concept_ids=("C03",),
            supporting_passage_ids=(passage_id,),
        ),
        CardClassification(
            note_id=50,
            verdict="YES",
            primary_subject="fixture",
            reason="medium existing",
            covered_concept_ids=("C05",),
            supporting_passage_ids=(passage_id,),
        ),
    )
    fast = (
        FastCardClassification(
            note_id=60,
            verdict="LIKELY_YES",
            grounded_concept_ids=("C06",),
            supporting_passage_ids=(passage_id,),
            reason="fast grounded",
        ),
    )

    first = select_high_yield_v2(
        thorough,
        fast_classifications=fast,
        ledger=ledger,
        source_index=source,
        generated_cards=generated,
    )
    shuffled_generated = list(generated)
    shuffled_thorough = list(thorough)
    random.Random(7).shuffle(shuffled_generated)
    random.Random(8).shuffle(shuffled_thorough)
    second = select_high_yield_v2(
        shuffled_thorough,
        fast_classifications=tuple(reversed(fast)),
        ledger=ledger,
        source_index=source,
        generated_cards=shuffled_generated,
    )

    assert first == second
    assert [item.tier for item in first.selection_metadata] == [
        SelectionTier.T1,
        SelectionTier.T2,
        SelectionTier.T3,
        SelectionTier.T4,
        SelectionTier.T5,
        SelectionTier.T6,
    ]
    assert first.excluded_existing_note_ids == ()


def test_selector_applies_subset_and_equivalent_coverage_dominance() -> None:
    source = _source()
    primary_id = next(
        passage.passage_id for passage in source.passages if passage.authority != "summary"
    )
    summary_id = next(
        passage.passage_id for passage in source.passages if passage.authority == "summary"
    )
    ledger = _ledger(2)
    result = select_high_yield_v2(
        (
            CardClassification(
                note_id=10,
                verdict="YES",
                primary_subject="fixture",
                reason="subset",
                covered_concept_ids=("C01",),
                supporting_passage_ids=(primary_id,),
            ),
            CardClassification(
                note_id=11,
                verdict="YES",
                primary_subject="fixture",
                reason="equivalent but summary only",
                covered_concept_ids=("C01",),
                supporting_passage_ids=(summary_id,),
            ),
            CardClassification(
                note_id=12,
                verdict="YES",
                primary_subject="fixture",
                reason="strict superset",
                covered_concept_ids=("C01", "C02"),
                supporting_passage_ids=(primary_id,),
            ),
        ),
        fast_classifications=(),
        ledger=ledger,
        source_index=source,
        generated_cards=(),
    )

    assert result.selected_existing_note_ids == (12,)
    assert result.excluded_existing_note_ids == (10, 11)


def test_summary_evidence_with_unknown_id_does_not_upgrade_to_primary() -> None:
    source = _source()
    summary_id = next(
        passage.passage_id for passage in source.passages if passage.authority == "summary"
    )
    result = select_high_yield_v2(
        (
            CardClassification(
                note_id=10,
                verdict="YES",
                primary_subject="fixture",
                reason="summary grounded",
                covered_concept_ids=("C01",),
                supporting_passage_ids=(summary_id, "unknown:passage"),
            ),
        ),
        fast_classifications=(),
        ledger=_ledger(1),
        source_index=source,
        generated_cards=(),
    )

    assert result.selection_metadata[0].evidence_quality is EvidenceQuality.SUMMARY_GROUNDED


def test_selector_never_pads_and_excludes_fallbacks_and_semantic_review_cards() -> None:
    source = _source()
    passage_id = source.passages[0].passage_id
    ledger = _ledger(2)
    reviewed = _generated(1, passage_id)
    result = select_high_yield_v2(
        (
            CardClassification(
                note_id=1,
                verdict="NO",
                primary_subject="fixture",
                reason="not taught",
            ),
        ),
        fast_classifications=(),
        fast_fallback_note_ids=(2,),
        semantic_review_required_card_ids=(reviewed.card_id,),
        ledger=ledger,
        source_index=source,
        generated_cards=(reviewed,),
    )

    assert result.selected_existing_note_ids == ()
    assert result.selected_generated_card_ids == ()
    assert result.excluded_existing_note_ids == (1, 2)
    assert result.excluded_generated_card_ids == (reviewed.card_id,)
    assert result.below_warning_floor is True


def test_t6_is_considered_only_below_warning_floor() -> None:
    source = _source()
    passage_id = source.passages[0].passage_id
    ledger = _ledger(61)
    generated = tuple(_generated(index, passage_id) for index in range(1, 61))
    fast = FastCardClassification(
        note_id=99,
        verdict="LIKELY_YES",
        grounded_concept_ids=("C61",),
        supporting_passage_ids=(passage_id,),
        reason="grounded fast",
    )
    result = select_high_yield_v2(
        (),
        fast_classifications=(fast,),
        ledger=ledger,
        source_index=source,
        generated_cards=generated,
    )

    assert len(result.selected_generated_card_ids) == 60
    assert result.selected_existing_note_ids == ()
    assert result.excluded_existing_note_ids == (99,)


def test_marginal_cards_need_approved_reasons_and_overflow_is_mandatory() -> None:
    source = _source()
    passage_id = source.passages[0].passage_id
    ledger = _ledger(71, high=set(range(1, 72)))
    result = select_high_yield_v2(
        (),
        fast_classifications=(),
        ledger=ledger,
        source_index=source,
        generated_cards=tuple(_generated(index, passage_id) for index in range(1, 72)),
        overflow_acknowledgement={
            "acknowledged_at": "2026-08-08T00:00:00Z",
            "acknowledged_by": "reviewer",
            "reason": "mandatory overflow",
        },
    )

    assert len(result.selected_generated_card_ids) == 71
    assert result.overflow_acknowledgement is not None
    marginal = [item for item in result.selection_metadata if 66 <= item.selected_position <= 70]
    assert [item.marginal_value_reason for item in marginal] == [
        MarginalValueReason.ONLY_VALID_REQUIRED_FACT
    ] * 5
    overflow = result.selection_metadata[-1]
    assert overflow.selected_position == 71
    assert overflow.mandatory is True
    assert overflow.manual_acknowledgement_required is True
    assert overflow.overflow_reason == "validated mandatory high-value nonredundant coverage"


def test_pending_mandatory_overflow_needs_review_but_excludes_nonmandatory_overflow() -> None:
    source = _source()
    passage_id = source.passages[0].passage_id
    result = select_high_yield_v2(
        (),
        fast_classifications=(),
        ledger=_ledger(72, high=set(range(1, 72))),
        source_index=source,
        generated_cards=tuple(_generated(index, passage_id) for index in range(1, 73)),
    )

    assert len(result.selected_generated_card_ids) == 71
    assert result.excluded_generated_card_ids == ("G72",)
    assert result.overflow_acknowledgement is None
    assert result.selection_metadata[-1].manual_acknowledgement_required is True


def test_invalid_low_value_marginal_card_is_excluded_after_65() -> None:
    source = _source()
    passage_id = source.passages[0].passage_id
    ledger = _ledger(66, low={66})
    generated = tuple(_generated(index, passage_id) for index in range(1, 67))
    result = select_high_yield_v2(
        (),
        fast_classifications=(),
        ledger=ledger,
        source_index=source,
        generated_cards=generated,
    )

    assert len(result.selected_generated_card_ids) == 65
    assert result.excluded_generated_card_ids == ("G66",)
