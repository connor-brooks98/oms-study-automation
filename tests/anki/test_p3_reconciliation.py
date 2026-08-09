import pytest

from oms_hub.anki.correction_contracts import (
    DuplicateIdentity,
    EvidenceQuality,
    GeneratedFactResolution,
    GeneratedResolutionKind,
    MarginalValueReason,
    SelectionMetadata,
    SelectionTier,
)
from oms_hub.anki.reconciliation import (
    AuditResolution,
    CardCentricReconciliationInput,
    GeneratedResolution,
    reconcile_card_centric,
)


def _metadata(
    selected_nids: tuple[int, ...],
    selected_generated: tuple[str, ...] = (),
    *,
    mandatory: bool = False,
) -> tuple[SelectionMetadata, ...]:
    identities = [*(f"existing:{nid}" for nid in selected_nids)] + [
        *(f"generated:{card_id}" for card_id in selected_generated)
    ]
    return tuple(
        SelectionMetadata(
            identity=identity,
            selected_position=position,
            tier=SelectionTier.T1,
            evidence_quality=EvidenceQuality.PRIMARY_SOURCE,
            mandatory=mandatory,
            marginal_value_reason=(
                MarginalValueReason.ONLY_VALID_REQUIRED_FACT if 66 <= position <= 70 else None
            ),
            overflow_reason="required evidence" if position > 70 else None,
            manual_acknowledgement_required=position > 70,
        )
        for position, identity in enumerate(identities, start=1)
    )


def _snapshot(
    *,
    required: tuple[str, ...] = (),
    raw: tuple[GeneratedResolution, ...] = (),
    canonical: tuple[GeneratedResolution, ...] = (),
    terminal: tuple[GeneratedFactResolution, ...] = (),
    selected_nids: tuple[int, ...] = tuple(range(1, 11)),
    selected_generated: tuple[str, ...] = (),
    mandatory_nids: tuple[int, ...] = (),
    mandatory_generated: tuple[str, ...] = (),
    acknowledgement: dict[str, object] | None = None,
    semantic_review: tuple[str, ...] = (),
    forbidden_by_fact: dict[str, tuple[str, ...]] | None = None,
) -> CardCentricReconciliationInput:
    metadata = _metadata(
        selected_nids,
        selected_generated,
        mandatory=bool(mandatory_nids or mandatory_generated),
    )
    return CardCentricReconciliationInput(
        pipeline_contract_version="card_centric_v2",
        concept_ids=("C01",),
        coverage={"C01": "covered"},
        required_fact_ids=required,
        uncovered_after_s5=(),
        residual_ran_for=(),
        generated_cards=tuple(card for card in canonical if card.card_id in selected_generated),
        raw_generated_cards=raw,
        canonical_generated_cards=canonical,
        terminal_resolutions=terminal,
        terminal_resolutions_provided=True,
        canonical_unresolved_fact_ids=tuple(
            resolution.fact_id
            for resolution in terminal
            if resolution.kind is GeneratedResolutionKind.UNRESOLVED
        ),
        unresolved_fact_ids=(),
        expected_scoped_nids=selected_nids,
        classifications=tuple(AuditResolution(nid=nid, verdict="keep") for nid in selected_nids),
        eligible_yes_nids=selected_nids,
        selected_nids=selected_nids,
        selected_generated_card_ids=selected_generated,
        generated_card_ids=tuple(card.card_id for card in canonical),
        source_passage_ids=(),
        forbidden_cloze_targets=(),
        forbidden_cloze_targets_by_fact=forbidden_by_fact or {},
        prompt_sync_stale=False,
        untagged_rate=0,
        mandatory_nids=mandatory_nids,
        mandatory_generated_card_ids=mandatory_generated,
        covered_concept_ids_by_nid={nid: ("C01",) for nid in selected_nids},
        generated_concept_id_by_card_id={card.card_id: "C01" for card in canonical},
        overflow_acknowledgement=acknowledgement,
        selection_metadata=metadata,
        selection_order=tuple(item.identity for item in metadata),
        selected_count=len(metadata),
        below_warning_floor=len(metadata) < 60,
        semantic_review_required_card_ids=semantic_review,
    )


def _generated(
    card_id: str,
    fact_id: str,
    *,
    text: str = "The supported result is {{c1::present}}.",
    split: bool = False,
    split_index: int | None = None,
) -> GeneratedResolution:
    return GeneratedResolution(
        card_id=card_id,
        fact_id=fact_id,
        text=text,
        split=split,
        split_index=split_index,
    )


def test_s9_conserves_unselected_valid_generation_without_a1_a2_failure() -> None:
    card = _generated("G1", "C01-M1")
    report = reconcile_card_centric(
        _snapshot(
            required=("C01-M1",),
            raw=(card,),
            canonical=(card,),
            terminal=(
                GeneratedFactResolution(
                    fact_id="C01-M1",
                    kind=GeneratedResolutionKind.GENERATED,
                    generated_card_ids=("G1",),
                ),
            ),
        )
    )

    assert {"A1", "A2"} <= set(report.passed)


def test_s9_adapts_only_prep3d_review_snapshots() -> None:
    card = _generated("G1", "C01-M1", split=True)
    legacy = _snapshot(
        required=("C01-M1",),
        canonical=(card,),
        selected_generated=("G1",),
    ).model_copy(
        update={
            "generated_cards": (card,),
            "raw_generated_cards": (),
            "terminal_resolutions": (),
            "terminal_resolutions_provided": False,
            "selection_metadata": (),
            "selection_order": (),
            "selected_count": None,
            "below_warning_floor": None,
        }
    )

    report = reconcile_card_centric(legacy)

    assert not report.failed
    assert report.can_render_envelope is True


def test_s9_strict_snapshots_cannot_omit_the_raw_s7_rows() -> None:
    card = _generated("G1", "C01-M1")
    report = reconcile_card_centric(
        _snapshot(
            required=("C01-M1",),
            canonical=(card,),
            terminal=(
                GeneratedFactResolution(
                    fact_id="C01-M1",
                    kind=GeneratedResolutionKind.GENERATED,
                    generated_card_ids=("G1",),
                ),
            ),
        )
    )

    assert "S7" in {finding.assertion_id for finding in report.failed}


def test_s9_validates_unselected_raw_outputs_before_selection() -> None:
    canonical = (_generated("G1", "C01-M1"), _generated("G2", "C01-M2"))
    raw = (
        _generated("G1", "C01-M1", text="{{c1::forbidden}} remains invalid."),
        _generated("G2", "C01-M2", text="This has no cloze deletion."),
    )
    report = reconcile_card_centric(
        _snapshot(
            required=("C01-M1", "C01-M2"),
            raw=raw,
            canonical=canonical,
            terminal=(
                GeneratedFactResolution(
                    fact_id="C01-M1",
                    kind=GeneratedResolutionKind.GENERATED,
                    generated_card_ids=("G1",),
                ),
                GeneratedFactResolution(
                    fact_id="C01-M2",
                    kind=GeneratedResolutionKind.GENERATED,
                    generated_card_ids=("G2",),
                ),
            ),
            forbidden_by_fact={"C01-M1": ("forbidden",)},
        )
    )

    assert {"A5", "A5b"} <= {finding.assertion_id for finding in report.failed}


def test_s9_preserves_actual_duplicate_identity_and_unresolved_terminal() -> None:
    card = _generated("G1", "C01-M2")
    report = reconcile_card_centric(
        _snapshot(
            required=("C01-M1", "C01-M2", "C01-M3"),
            raw=(card,),
            canonical=(card,),
            terminal=(
                GeneratedFactResolution(
                    fact_id="C01-M1",
                    kind=GeneratedResolutionKind.DUPLICATE_OF_EXISTING,
                    duplicate_of=DuplicateIdentity(existing_note_id=9),
                ),
                GeneratedFactResolution(
                    fact_id="C01-M2",
                    kind=GeneratedResolutionKind.GENERATED,
                    generated_card_ids=("G1",),
                ),
                GeneratedFactResolution(
                    fact_id="C01-M3",
                    kind=GeneratedResolutionKind.UNRESOLVED,
                    unresolved_reason="No grounded atomic card.",
                ),
            ),
        )
    )

    assert {"A1", "A2"} <= set(report.passed)


def test_s9_duplicate_terminals_require_selected_current_coverage() -> None:
    duplicate = GeneratedFactResolution(
        fact_id="C01-M1",
        kind=GeneratedResolutionKind.DUPLICATE_OF_EXISTING,
        duplicate_of=DuplicateIdentity(existing_note_id=999),
    )
    unselected = reconcile_card_centric(
        _snapshot(required=("C01-M1",), terminal=(duplicate,))
    )
    wrong_concept = reconcile_card_centric(
        _snapshot(required=("C01-M1",), terminal=(duplicate,)).model_copy(
            update={
                "terminal_resolutions": (
                    duplicate.model_copy(
                        update={"duplicate_of": DuplicateIdentity(existing_note_id=1)}
                    ),
                ),
                "covered_concept_ids_by_nid": {note_id: ("C02",) for note_id in range(1, 11)},
            }
        )
    )
    correct_existing = reconcile_card_centric(
        _snapshot(required=("C01-M1",), terminal=(
            duplicate.model_copy(
                update={"duplicate_of": DuplicateIdentity(existing_note_id=1)}
            ),
        ))
    )
    generated = _generated("G1", "C02-M1")
    correct_generated = reconcile_card_centric(
        _snapshot(
            required=("C01-M1", "C02-M1"),
            raw=(generated,),
            canonical=(generated,),
            terminal=(
                GeneratedFactResolution(
                    fact_id="C01-M1",
                    kind=GeneratedResolutionKind.DUPLICATE_OF_EXISTING,
                    duplicate_of=DuplicateIdentity(generated_card_id="G1"),
                ),
                GeneratedFactResolution(
                    fact_id="C02-M1",
                    kind=GeneratedResolutionKind.GENERATED,
                    generated_card_ids=("G1",),
                ),
            ),
            selected_generated=("G1",),
        )
    )

    assert "duplicate_coverage" in {item.assertion_id for item in unselected.failed}
    assert "duplicate_coverage" in {item.assertion_id for item in wrong_concept.failed}
    assert "duplicate_coverage" in correct_existing.passed
    assert "duplicate_coverage" in correct_generated.passed


def test_s9_aggregates_sequential_split_ids_for_one_terminal_fact() -> None:
    cards = (
        _generated("G1", "C01-M1", split=True, split_index=1),
        _generated("G2", "C01-M1", split=True, split_index=2),
    )
    report = reconcile_card_centric(
        _snapshot(
            required=("C01-M1",),
            raw=cards,
            canonical=cards,
            terminal=(
                GeneratedFactResolution(
                    fact_id="C01-M1",
                    kind=GeneratedResolutionKind.GENERATED,
                    generated_card_ids=("G1", "G2"),
                ),
            ),
        )
    )

    assert {"A1", "A2"} <= set(report.passed)


def test_s9_below_60_is_a_warning() -> None:
    report = reconcile_card_centric(_snapshot())

    assert "selection_warning_floor" in {warning.assertion_id for warning in report.warned}


def test_s9_requires_66_to_70_marginal_reasons() -> None:
    with pytest.raises(ValueError, match="66-70"):
        SelectionMetadata(
            identity="existing:66",
            selected_position=66,
            tier=SelectionTier.T1,
            evidence_quality=EvidenceQuality.PRIMARY_SOURCE,
        )

    report = reconcile_card_centric(_snapshot(selected_nids=tuple(range(1, 67))))

    assert "selection_metadata" in report.passed


def test_s9_unsigned_and_signed_mandatory_overflow_have_distinct_issuance_results() -> None:
    selected = tuple(range(1, 72))
    unsigned = reconcile_card_centric(
        _snapshot(selected_nids=selected, mandatory_nids=selected)
    )
    signed = reconcile_card_centric(
        _snapshot(
            selected_nids=selected,
            mandatory_nids=selected,
            acknowledgement={"token": "server-issued"},
        )
    )

    assert "selection_cap" in {finding.assertion_id for finding in unsigned.failed}
    assert unsigned.can_render_envelope is False
    assert "selection_cap" in signed.passed
    assert signed.can_render_envelope is True


def test_s9_v1_signed_mixed_overflow_binds_selected_generated_gap_card() -> None:
    selected = tuple(range(1, 72))
    generated = _generated("G1", "C01-M1")
    base = _snapshot(
        required=("C01-M1",),
        raw=(generated,),
        canonical=(generated,),
        terminal=(
            GeneratedFactResolution(
                fact_id="C01-M1",
                kind=GeneratedResolutionKind.GENERATED,
                generated_card_ids=("G1",),
            ),
        ),
        selected_nids=selected,
        selected_generated=("G1",),
        mandatory_nids=selected,
    ).model_copy(update={"pipeline_contract_version": "card_centric_v1"})

    unsigned = reconcile_card_centric(base)
    signed = reconcile_card_centric(
        base.model_copy(update={"overflow_acknowledgement": {"token": "server-issued"}})
    )

    assert "selection_cap" in {finding.assertion_id for finding in unsigned.failed}
    assert unsigned.can_render_envelope is False
    assert "selection_cap" in signed.passed
    assert signed.can_render_envelope is True


def test_s9_semantic_review_is_nonterminal_and_blocks_issuance() -> None:
    card = _generated("G1", "C01-M1")
    report = reconcile_card_centric(
        _snapshot(
            required=("C01-M1",),
            raw=(card,),
            canonical=(card,),
            terminal=(),
            semantic_review=("G1",),
        )
    )

    assert "S8" in {finding.assertion_id for finding in report.failed}
    assert report.can_render_envelope is False
