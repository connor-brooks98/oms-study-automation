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
    DuplicateIdentity,
    GeneratedFactResolution,
    GeneratedResolutionKind,
    MarginalValueReason,
    SelectionTier,
)
from oms_hub.anki.domain import SourceKind
from oms_hub.anki.reconciliation import (
    AuditResolution,
    CardCentricReconciliationInput,
    GeneratedResolution,
    reconcile_card_centric,
)
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


def test_selector_preserves_distinct_existing_facts_with_equivalent_concepts() -> None:
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

    assert result.selected_existing_note_ids == (12, 10, 11)
    assert result.excluded_existing_note_ids == ()


def test_selector_preserves_duplicate_target_and_distinct_existing_fact() -> None:
    source = _source()
    primary_id = next(
        passage.passage_id for passage in source.passages if passage.authority != "summary"
    )
    summary_id = next(
        passage.passage_id for passage in source.passages if passage.authority == "summary"
    )
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

    result = select_high_yield_v2(
        (
            CardClassification(
                note_id=10,
                verdict="YES",
                primary_subject="fixture",
                reason="higher-ranked equivalent coverage",
                covered_concept_ids=("C01",),
                supporting_passage_ids=(primary_id,),
            ),
            CardClassification(
                note_id=11,
                verdict="YES",
                primary_subject="fixture",
                reason="exact S8 duplicate target",
                covered_concept_ids=("C01",),
                supporting_passage_ids=(summary_id,),
            ),
        ),
        fast_classifications=(),
        ledger=_ledger(1),
        source_index=source,
        generated_cards=(duplicate,),
    )

    assert result.selected_existing_note_ids == (11, 10)
    assert result.excluded_existing_note_ids == ()
    assert result.mandatory_note_ids == (11,)


def _duplicate_target(
    *,
    card_id: str,
    concept_id: str,
    note_id: int,
    passage_id: str,
) -> GeneratedCardResolution:
    return GeneratedCardResolution(
        card_id=card_id,
        concept_id=concept_id,
        fact_id=f"{concept_id}-M1",
        text="{{c1::Duplicate fact}}",
        source_passage_ids=(passage_id,),
        evidence_ids=(f"E-{card_id}",),
        status="duplicate_of_existing",
        duplicate_of_existing_note_id=note_id,
        reason=f"Semantic duplicate of existing note {note_id}.",
    )


def _thorough_duplicate_target(
    *, note_id: int, concept_id: str, passage_id: str
) -> CardClassification:
    return CardClassification(
        note_id=note_id,
        verdict="YES",
        primary_subject="fixture",
        reason="independently eligible thorough S8 duplicate target",
        covered_concept_ids=(concept_id,),
        covered_fact_ids=(f"{concept_id}-M1",),
        supporting_passage_ids=(passage_id,),
    )


def _generated_duplicate_target(
    *,
    card_id: str,
    concept_id: str,
    fact_id: str,
    target_card_id: str,
    passage_id: str,
) -> GeneratedCardResolution:
    return GeneratedCardResolution(
        card_id=card_id,
        concept_id=concept_id,
        fact_id=fact_id,
        text="{{c1::Duplicate generated fact}}",
        source_passage_ids=(passage_id,),
        evidence_ids=(f"E-{card_id}",),
        status="duplicate_of_existing",
        duplicate_of_generated_card_id=target_card_id,
        reason=f"Semantic duplicate of generated card {target_card_id}.",
    )


def _strict_duplicate_selection_report(
    *,
    source: CardCentricSourceIndex,
    ledger: CardConceptLedger,
    generated_rows: tuple[GeneratedCardResolution, ...],
    selection,
    classifications: tuple[CardClassification, ...] = (),
    fast_classifications: tuple[FastCardClassification, ...] = (),
) -> object:
    """Freeze a real selector result into strict S9 inputs for boundary tests."""
    canonical = tuple(row for row in generated_rows if row.status == "generated")
    selected_generated = set(selection.selected_generated_card_ids)
    terminals: list[GeneratedFactResolution] = [
        GeneratedFactResolution(
            fact_id=row.fact_id,
            kind=GeneratedResolutionKind.GENERATED,
            generated_card_ids=(row.card_id,),
        )
        for row in canonical
    ]
    for row in generated_rows:
        if row.status != "duplicate_of_existing":
            continue
        duplicate_of = (
            DuplicateIdentity(existing_note_id=row.duplicate_of_existing_note_id)
            if row.duplicate_of_existing_note_id is not None
            else DuplicateIdentity(generated_card_id=row.duplicate_of_generated_card_id)
        )
        terminals.append(
            GeneratedFactResolution(
                fact_id=row.fact_id,
                kind=GeneratedResolutionKind.DUPLICATE_OF_EXISTING,
                duplicate_of=duplicate_of,
            )
        )

    def as_s9(row: GeneratedCardResolution) -> GeneratedResolution:
        return GeneratedResolution(
            card_id=row.card_id,
            fact_id=row.fact_id,
            text=row.text,
            split=row.split,
            split_index=row.split_index,
        )

    note_coverage = {
        row.note_id: row.covered_concept_ids for row in classifications
    } | {
        row.note_id: row.grounded_concept_ids for row in fast_classifications
    }
    audit_note_ids = tuple(sorted(note_coverage))
    snapshot = CardCentricReconciliationInput(
        pipeline_contract_version="card_centric_v2",
        concept_ids=tuple(concept.concept_id for concept in ledger.concepts),
        coverage={concept.concept_id: "covered" for concept in ledger.concepts},
        required_fact_ids=tuple(row.fact_id for row in terminals),
        uncovered_after_s5=tuple(concept.concept_id for concept in ledger.concepts),
        residual_ran_for=tuple(concept.concept_id for concept in ledger.concepts),
        generated_cards=tuple(
            as_s9(row) for row in canonical if row.card_id in selected_generated
        ),
        raw_generated_cards=tuple(as_s9(row) for row in generated_rows),
        canonical_generated_cards=tuple(as_s9(row) for row in canonical),
        terminal_resolutions=tuple(terminals),
        terminal_resolutions_provided=True,
        canonical_unresolved_fact_ids=(),
        unresolved_fact_ids=(),
        expected_scoped_nids=audit_note_ids,
        classifications=tuple(
            AuditResolution(nid=note_id, verdict="keep") for note_id in audit_note_ids
        ),
        eligible_yes_nids=audit_note_ids,
        selected_nids=selection.selected_existing_note_ids,
        selected_generated_card_ids=selection.selected_generated_card_ids,
        generated_card_ids=tuple(row.card_id for row in canonical),
        source_passage_ids=tuple(passage.passage_id for passage in source.passages),
        forbidden_cloze_targets=(),
        prompt_sync_stale=False,
        untagged_rate=0,
        mandatory_nids=selection.mandatory_note_ids,
        mandatory_generated_card_ids=selection.mandatory_generated_card_ids,
        covered_concept_ids_by_nid=note_coverage,
        generated_concept_id_by_card_id={
            row.card_id: row.concept_id for row in canonical
        },
        overflow_acknowledgement=(
            {"acknowledged_by": "reviewer"}
            if selection.overflow_acknowledgement is not None
            else None
        ),
        selection_metadata=selection.selection_metadata,
        selection_order=tuple(item.identity for item in selection.selection_metadata),
        selected_count=(
            len(selection.selected_existing_note_ids)
            + len(selection.selected_generated_card_ids)
        ),
        below_warning_floor=selection.below_warning_floor,
    )
    return reconcile_card_centric(snapshot)


def test_fast_duplicate_target_is_not_mandatory_after_warning_floor() -> None:
    source = _source()
    passage_id = source.passages[0].passage_id
    target = _duplicate_target(
        card_id="G61",
        concept_id="C61",
        note_id=99,
        passage_id=passage_id,
    )
    result = select_high_yield_v2(
        (),
        fast_classifications=(
            FastCardClassification(
                note_id=99,
                verdict="LIKELY_YES",
                grounded_concept_ids=("C61",),
                supporting_passage_ids=(passage_id,),
                reason="fast-only historical S8 duplicate target",
            ),
        ),
        ledger=_ledger(61),
        source_index=source,
        generated_cards=(*(_generated(index, passage_id) for index in range(1, 61)), target),
    )

    assert result.selected_existing_note_ids == ()
    assert result.mandatory_note_ids == ()
    assert 99 in result.excluded_existing_note_ids
    assert all(item.identity != "existing:99" for item in result.selection_metadata)


def test_thorough_duplicate_target_has_governed_marginal_reason_after_65() -> None:
    source = _source()
    passage_id = source.passages[0].passage_id
    target = _duplicate_target(
        card_id="G66",
        concept_id="C66",
        note_id=66,
        passage_id=passage_id,
    )
    result = select_high_yield_v2(
        (
            CardClassification(
                note_id=66,
                verdict="YES",
                primary_subject="fixture",
                reason="exact S8 duplicate target with low ordinary value",
                covered_concept_ids=("C66",),
                supporting_passage_ids=(passage_id,),
            ),
        ),
        fast_classifications=(),
        ledger=_ledger(66, low={66}),
        source_index=source,
        generated_cards=(*(_generated(index, passage_id) for index in range(1, 66)), target),
    )

    selected = result.selection_metadata[-1]
    assert result.selected_existing_note_ids == (66,)
    assert selected.selected_position == 66
    assert selected.marginal_value_reason is MarginalValueReason.ONLY_VALID_REQUIRED_FACT


def test_duplicate_targets_reserve_soft_cap_then_use_mandatory_overflow() -> None:
    source = _source()
    passage_id = source.passages[0].passage_id
    target = _duplicate_target(
        card_id="D70",
        concept_id="C70",
        note_id=99,
        passage_id=passage_id,
    )
    thorough = _thorough_duplicate_target(
        note_id=99, concept_id="C70", passage_id=passage_id
    )
    reserved = select_high_yield_v2(
        (thorough,),
        fast_classifications=(),
        ledger=_ledger(70),
        source_index=source,
        generated_cards=(*(_generated(index, passage_id) for index in range(1, 71)), target),
    )

    assert len(reserved.selection_metadata) == 70
    assert reserved.selection_metadata[-1].identity == "existing:99"
    assert reserved.selection_metadata[-1].selected_position == 70
    assert "G70" in reserved.excluded_generated_card_ids

    overflow = select_high_yield_v2(
        (thorough,),
        fast_classifications=(),
        ledger=_ledger(70, high=set(range(1, 71))),
        source_index=source,
        generated_cards=(*(_generated(index, passage_id) for index in range(1, 71)), target),
        overflow_acknowledgement={
            "acknowledged_at": "2026-08-09T00:00:00Z",
            "acknowledged_by": "reviewer",
            "reason": "S8 duplicate target conservation",
        },
    )

    assert len(overflow.selection_metadata) == 70
    assert overflow.selection_metadata[-1].identity == "existing:99"
    assert overflow.selection_metadata[-1].mandatory is True
    assert overflow.selection_metadata[-1].manual_acknowledgement_required is False
    assert "G70" in overflow.excluded_generated_card_ids
    assert overflow.overflow_acknowledgement is not None


def test_multiple_equivalent_duplicate_targets_are_all_conserved() -> None:
    source = _source()
    passage_id = source.passages[0].passage_id
    targets = (
        _duplicate_target(
            card_id="G1a",
            concept_id="C01",
            note_id=10,
            passage_id=passage_id,
        ),
        _duplicate_target(
            card_id="G1b",
            concept_id="C01",
            note_id=11,
            passage_id=passage_id,
        ),
    )
    result = select_high_yield_v2(
        (
            _thorough_duplicate_target(
                note_id=10, concept_id="C01", passage_id=passage_id
            ),
            _thorough_duplicate_target(
                note_id=11, concept_id="C01", passage_id=passage_id
            ),
        ),
        fast_classifications=(),
        ledger=_ledger(1),
        source_index=source,
        generated_cards=targets,
    )

    assert result.selected_existing_note_ids == (10, 11)
    assert result.mandatory_note_ids == (10, 11)
    assert result.excluded_existing_note_ids == ()


def test_generated_duplicate_target_is_conserved_at_65_and_70() -> None:
    source = _source()
    passage_id = source.passages[0].passage_id
    target_at_65 = _generated(65, passage_id)
    at_65 = select_high_yield_v2(
        (),
        fast_classifications=(),
        ledger=_ledger(65, low={65}),
        source_index=source,
        generated_cards=(
            *(_generated(index, passage_id) for index in range(1, 65)),
            target_at_65,
            _generated_duplicate_target(
                card_id="D65",
                concept_id="C65",
                fact_id="C65-M2",
                target_card_id=target_at_65.card_id,
                passage_id=passage_id,
            ),
        ),
    )

    assert at_65.selection_metadata[-1].identity == "generated:G65"
    assert at_65.selection_metadata[-1].selected_position == 65
    assert at_65.mandatory_generated_card_ids == ("G65",)

    target_at_70 = GeneratedCardResolution(
        card_id="G71",
        concept_id="C70",
        fact_id="C70-M2",
        text="{{c1::Duplicate target for C70}}",
        source_passage_ids=(passage_id,),
        evidence_ids=("E-G71",),
    )
    at_70 = select_high_yield_v2(
        (),
        fast_classifications=(),
        ledger=_ledger(70),
        source_index=source,
        generated_cards=(
            *(_generated(index, passage_id) for index in range(1, 71)),
            target_at_70,
            _generated_duplicate_target(
                card_id="D71",
                concept_id="C70",
                fact_id="C70-M3",
                target_card_id=target_at_70.card_id,
                passage_id=passage_id,
            ),
        ),
    )

    assert "G71" in at_70.selected_generated_card_ids
    assert len(at_70.selection_metadata) == 70
    assert "G70" in at_70.excluded_generated_card_ids


def test_generated_duplicate_target_uses_mandatory_overflow_when_required() -> None:
    source = _source()
    passage_id = source.passages[0].passage_id
    target = _generated(71, passage_id)
    result = select_high_yield_v2(
        (),
        fast_classifications=(),
        ledger=_ledger(71, high=set(range(1, 71))),
        source_index=source,
        generated_cards=(
            *(_generated(index, passage_id) for index in range(1, 71)),
            target,
            _generated_duplicate_target(
                card_id="D71",
                concept_id="C71",
                fact_id="C71-M2",
                target_card_id=target.card_id,
                passage_id=passage_id,
            ),
        ),
        overflow_acknowledgement={
            "acknowledged_at": "2026-08-09T00:00:00Z",
            "acknowledged_by": "reviewer",
            "reason": "S8 generated target conservation",
        },
    )

    overflow = result.selection_metadata[-1]
    assert overflow.identity == "generated:G71"
    assert overflow.selected_position == 71
    assert overflow.mandatory is True
    assert overflow.manual_acknowledgement_required is True
    assert result.overflow_acknowledgement is not None


def test_multiple_equivalent_generated_duplicate_targets_are_all_conserved() -> None:
    source = _source()
    passage_id = source.passages[0].passage_id
    targets = (
        GeneratedCardResolution(
            card_id="G-target-a",
            concept_id="C01",
            fact_id="C01-M1",
            text="{{c1::First generated target}}",
            source_passage_ids=(passage_id,),
            evidence_ids=("E-target-a",),
        ),
        GeneratedCardResolution(
            card_id="G-target-b",
            concept_id="C01",
            fact_id="C01-M1",
            text="{{c1::Second generated target}}",
            source_passage_ids=(passage_id,),
            evidence_ids=("E-target-b",),
        ),
    )
    result = select_high_yield_v2(
        (),
        fast_classifications=(),
        ledger=_ledger(1),
        source_index=source,
        generated_cards=(
            *targets,
            _generated_duplicate_target(
                card_id="D-target-a",
                concept_id="C01",
                fact_id="C01-M2",
                target_card_id="G-target-a",
                passage_id=passage_id,
            ),
            _generated_duplicate_target(
                card_id="D-target-b",
                concept_id="C01",
                fact_id="C01-M3",
                target_card_id="G-target-b",
                passage_id=passage_id,
            ),
        ),
    )

    assert result.selected_generated_card_ids == ("G-target-a", "G-target-b")
    assert result.mandatory_generated_card_ids == ("G-target-a", "G-target-b")


def test_existing_duplicate_target_replaces_only_redundant_coverage_before_cap() -> None:
    source = _source()
    passage_id = source.passages[0].passage_id
    duplicate = _duplicate_target(
        card_id="D70",
        concept_id="C70",
        note_id=99,
        passage_id=passage_id,
    ).model_copy(update={"fact_id": "C70-M2"})
    thorough = _thorough_duplicate_target(
        note_id=99, concept_id="C70", passage_id=passage_id
    )
    generated = (*(_generated(index, passage_id) for index in range(1, 71)), duplicate)
    selection = select_high_yield_v2(
        (thorough,),
        fast_classifications=(),
        ledger=_ledger(70),
        source_index=source,
        generated_cards=generated,
    )
    report = _strict_duplicate_selection_report(
        source=source,
        ledger=_ledger(70),
        generated_rows=generated,
        selection=selection,
        classifications=(thorough,),
    )

    assert selection.selected_existing_note_ids == (99,)
    assert "G70" in selection.excluded_generated_card_ids
    assert len(selection.selection_metadata) == 70
    assert report.failed == ()


def test_late_existing_duplicate_target_replaces_early_redundancy_deterministically() -> None:
    source = _source()
    passage_id = source.passages[0].passage_id
    duplicate = _duplicate_target(
        card_id="D71",
        concept_id="C01",
        note_id=99,
        passage_id=passage_id,
    ).model_copy(update={"fact_id": "C01-M2"})
    thorough = _thorough_duplicate_target(
        note_id=99, concept_id="C01", passage_id=passage_id
    )
    early_rows = (*(_generated(index, passage_id) for index in range(1, 71)), duplicate)
    late_rows = (
        _generated(1, passage_id).model_copy(update={"card_id": "G99"}),
        *(_generated(index, passage_id) for index in range(2, 71)),
        duplicate,
    )
    early = select_high_yield_v2(
        (thorough,),
        fast_classifications=(),
        ledger=_ledger(70),
        source_index=source,
        generated_cards=early_rows,
    )
    late = select_high_yield_v2(
        (thorough,),
        fast_classifications=(),
        ledger=_ledger(70),
        source_index=source,
        generated_cards=late_rows,
    )
    early_report = _strict_duplicate_selection_report(
        source=source,
        ledger=_ledger(70),
        generated_rows=early_rows,
        selection=early,
        classifications=(thorough,),
    )
    late_report = _strict_duplicate_selection_report(
        source=source,
        ledger=_ledger(70),
        generated_rows=late_rows,
        selection=late,
        classifications=(thorough,),
    )

    assert len(early.selection_metadata) == len(late.selection_metadata) == 70
    assert "G01" in early.excluded_generated_card_ids
    assert "G99" in late.excluded_generated_card_ids
    assert early.selection_metadata[-1].identity == "existing:99"
    assert late.selection_metadata[-1].identity == "existing:99"
    assert early_report.failed == ()
    assert late_report.failed == ()


def test_existing_duplicate_target_keeps_unique_coverage_and_overflows() -> None:
    source = _source()
    passage_id = source.passages[0].passage_id
    duplicate = _duplicate_target(
        card_id="D71",
        concept_id="C71",
        note_id=99,
        passage_id=passage_id,
    )
    thorough = _thorough_duplicate_target(
        note_id=99, concept_id="C71", passage_id=passage_id
    )
    generated = (*(_generated(index, passage_id) for index in range(1, 71)), duplicate)
    selection = select_high_yield_v2(
        (thorough,),
        fast_classifications=(),
        ledger=_ledger(71),
        source_index=source,
        generated_cards=generated,
        overflow_acknowledgement={
            "acknowledged_at": "2026-08-09T00:00:00Z",
            "acknowledged_by": "reviewer",
            "reason": "unique coverage must not be evicted",
        },
    )
    report = _strict_duplicate_selection_report(
        source=source,
        ledger=_ledger(71),
        generated_rows=generated,
        selection=selection,
        classifications=(thorough,),
    )

    assert "G70" in selection.selected_generated_card_ids
    assert selection.selection_metadata[-1].identity == "existing:99"
    assert selection.selection_metadata[-1].manual_acknowledgement_required is True
    assert report.failed == ()


def test_generated_duplicate_target_boundaries_preserve_strict_s9_coverage() -> None:
    source = _source()
    passage_id = source.passages[0].passage_id
    redundant_target = GeneratedCardResolution(
        card_id="G71",
        concept_id="C70",
        fact_id="C70-M2",
        text="{{c1::Generated redundant target}}",
        source_passage_ids=(passage_id,),
        evidence_ids=("E-G71",),
    )
    redundant = (
        *(_generated(index, passage_id) for index in range(1, 71)),
        redundant_target,
        _generated_duplicate_target(
            card_id="D71",
            concept_id="C70",
            fact_id="C70-M3",
            target_card_id="G71",
            passage_id=passage_id,
        ),
    )
    safe_selection = select_high_yield_v2(
        (),
        fast_classifications=(),
        ledger=_ledger(70),
        source_index=source,
        generated_cards=redundant,
    )
    safe_report = _strict_duplicate_selection_report(
        source=source,
        ledger=_ledger(70),
        generated_rows=redundant,
        selection=safe_selection,
    )

    unique_target = _generated(71, passage_id)
    unique = (
        *(_generated(index, passage_id) for index in range(1, 71)),
        unique_target,
        _generated_duplicate_target(
            card_id="D72",
            concept_id="C71",
            fact_id="C71-M2",
            target_card_id=unique_target.card_id,
            passage_id=passage_id,
        ),
    )
    overflow_selection = select_high_yield_v2(
        (),
        fast_classifications=(),
        ledger=_ledger(71, low={71}),
        source_index=source,
        generated_cards=unique,
        overflow_acknowledgement={
            "acknowledged_at": "2026-08-09T00:00:00Z",
            "acknowledged_by": "reviewer",
            "reason": "unique generated coverage must not be evicted",
        },
    )
    overflow_report = _strict_duplicate_selection_report(
        source=source,
        ledger=_ledger(71, low={71}),
        generated_rows=unique,
        selection=overflow_selection,
    )

    assert "G70" in safe_selection.excluded_generated_card_ids
    assert "G71" in safe_selection.selected_generated_card_ids
    # Generated exact targets are mandatory in their own tier, so identity
    # ordering cannot delay them behind an equivalent ordinary generated card.
    assert safe_selection.selection_metadata[0].identity == "generated:G71"
    assert safe_report.failed == ()
    assert "G70" in overflow_selection.selected_generated_card_ids
    assert overflow_selection.selection_metadata[-1].identity == "generated:G71"
    assert overflow_selection.selection_metadata[-1].manual_acknowledgement_required is True
    assert overflow_report.failed == ()


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

    # P2's shared v2 eligibility predicate rejects a classifier row containing
    # an unknown source identity.  The selector must preserve that integrity
    # boundary rather than silently treating the unknown citation as grounded.
    assert result.selected_existing_note_ids == ()
    assert result.excluded_existing_note_ids == (10,)
    assert result.selection_metadata == ()


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


def test_unique_required_existing_card_is_retained_after_65() -> None:
    source = _source()
    passage_id = next(
        passage.passage_id for passage in source.passages if passage.authority != "summary"
    )
    summary_id = next(
        passage.passage_id for passage in source.passages if passage.authority == "summary"
    )
    ledger = _ledger(67, low={67})
    classifications = (
        CardClassification(
            note_id=66,
            verdict="YES",
            primary_subject="fixture",
            reason="unique required medium coverage",
            covered_concept_ids=("C66",),
            supporting_passage_ids=(passage_id,),
        ),
        CardClassification(
            note_id=67,
            verdict="YES",
            primary_subject="fixture",
            reason="only the low-value coverage is new",
            covered_concept_ids=("C66", "C67"),
            supporting_passage_ids=(summary_id,),
        ),
    )

    result = select_high_yield_v2(
        classifications,
        fast_classifications=(),
        ledger=ledger,
        source_index=source,
        generated_cards=tuple(_generated(index, passage_id) for index in range(1, 66)),
    )

    assert result.selected_generated_card_ids == tuple(f"G{index:02d}" for index in range(1, 66))
    assert result.selected_existing_note_ids == (66,)
    assert result.excluded_existing_note_ids == (67,)
    marginal = result.selection_metadata[-1]
    assert marginal.identity == "existing:66"
    assert marginal.selected_position == 66
    assert marginal.tier is SelectionTier.T5
    assert marginal.marginal_value_reason is MarginalValueReason.ONLY_VALID_REQUIRED_FACT
