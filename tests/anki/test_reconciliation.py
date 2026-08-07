from pathlib import Path

from oms_hub.anki.reconciliation import (
    AuditResolution,
    CardCentricReconciliationInput,
    ConceptResolution,
    GeneratedResolution,
    ReconciliationInput,
    reconcile,
    reconcile_card_centric,
)

FIXTURE = Path(__file__).parent / "fixtures" / "lecture_07_v1_reconciliation.json"


def test_v1_zero_gap_output_fails_fact_reconciliation() -> None:
    snapshot = ReconciliationInput.model_validate_json(FIXTURE.read_text(encoding="utf-8"))

    report = reconcile(snapshot)

    assert {item.assertion_id for item in report.failed} >= {"A1", "A2", "A3", "A4"}
    assert report.can_render_envelope is False


def test_split_cards_satisfy_one_missing_fact() -> None:
    snapshot = ReconciliationInput(
        concepts=(
            ConceptResolution(
                concept_id="C01",
                missing_fact_ids=("C01-M1",),
                status="covered",
                converged=True,
                cited_passage_ids=("SLD:07:0001", "TRX:07:0001"),
            ),
        ),
        generated_cards=(
            GeneratedResolution(
                card_id="G1",
                fact_id="C01-M1",
                text="Mechanism is {{c1::first step}}.",
            ),
            GeneratedResolution(
                card_id="G2",
                fact_id="C01-M1",
                text="Consequence is {{c1::second step}}.",
            ),
        ),
        unresolved_fact_ids=(),
        expected_audit_nids=(1001,),
        audit_verdicts=(AuditResolution(nid=1001, verdict="keep"),),
        source_passage_ids=("SLD:07:0001", "TRX:07:0001"),
        forbidden_cloze_targets=(),
        prompt_sync_stale=False,
    )

    report = reconcile(snapshot)

    assert "A1" in report.passed
    assert "A2" in report.passed


def test_duplicate_audit_nid_fails_exact_partition() -> None:
    snapshot = ReconciliationInput(
        concepts=(),
        generated_cards=(),
        unresolved_fact_ids=(),
        expected_audit_nids=(1001,),
        audit_verdicts=(
            AuditResolution(nid=1001, verdict="keep"),
            AuditResolution(nid=1001, verdict="drop"),
        ),
        source_passage_ids=(),
        forbidden_cloze_targets=(),
        prompt_sync_stale=False,
    )

    report = reconcile(snapshot)

    assert "A3" in {item.assertion_id for item in report.failed}


def test_generated_card_cannot_blank_forbidden_target() -> None:
    snapshot = ReconciliationInput(
        concepts=(
            ConceptResolution(
                concept_id="C01",
                missing_fact_ids=("C01-M1",),
                status="covered",
                converged=True,
                cited_passage_ids=(),
            ),
        ),
        generated_cards=(
            GeneratedResolution(
                card_id="G1",
                fact_id="C01-M1",
                text="{{c1::<b>G6PD deficiency</b>}} is X-linked.",
            ),
        ),
        unresolved_fact_ids=(),
        expected_audit_nids=(),
        audit_verdicts=(),
        source_passage_ids=(),
        forbidden_cloze_targets=("G6PD deficiency",),
        prompt_sync_stale=False,
    )

    report = reconcile(snapshot)

    assert "A5" in {item.assertion_id for item in report.failed}


def test_stale_prompt_and_nonconvergence_are_warnings() -> None:
    snapshot = ReconciliationInput(
        concepts=(
            ConceptResolution(
                concept_id="C01",
                missing_fact_ids=(),
                status="covered",
                converged=False,
                cited_passage_ids=(),
            ),
        ),
        generated_cards=(),
        unresolved_fact_ids=(),
        expected_audit_nids=(),
        audit_verdicts=(),
        source_passage_ids=("SLD:07:0001",),
        forbidden_cloze_targets=(),
        prompt_sync_stale=True,
    )

    report = reconcile(snapshot)

    assert {item.assertion_id for item in report.warned} == {"A9", "A10", "A11"}
    assert report.can_render_envelope is True


def test_card_centric_s9_uses_only_the_selected_eligible_cards() -> None:
    eligible = tuple(range(1, 11))
    snapshot = CardCentricReconciliationInput(
        concept_ids=("C01",),
        coverage={"C01": "covered"},
        required_fact_ids=(),
        uncovered_after_s5=(),
        residual_ran_for=(),
        generated_cards=(),
        unresolved_fact_ids=(),
        expected_scoped_nids=eligible,
        classifications=tuple(AuditResolution(nid=nid, verdict="keep") for nid in eligible),
        eligible_yes_nids=eligible,
        selected_nids=(1,),
        selected_generated_card_ids=(),
        generated_card_ids=(),
        source_passage_ids=(),
        forbidden_cloze_targets=(),
        prompt_sync_stale=False,
        untagged_rate=0,
        covered_concept_ids_by_nid={1: ()},
    )

    report = reconcile_card_centric(snapshot)

    assert {item.assertion_id for item in report.failed} >= {"A6"}


def test_card_centric_s9_accepts_documented_t6_but_not_undocumented_selection() -> None:
    base = dict(
        concept_ids=("C01",),
        coverage={"C01": "covered"},
        required_fact_ids=(),
        uncovered_after_s5=(),
        residual_ran_for=(),
        generated_cards=(),
        unresolved_fact_ids=(),
        expected_scoped_nids=tuple(range(1, 11)),
        classifications=tuple(
            AuditResolution(nid=nid, verdict="uncertain") for nid in range(1, 11)
        ),
        eligible_yes_nids=(),
        selected_nids=tuple(range(1, 11)),
        selected_generated_card_ids=(),
        generated_card_ids=(),
        source_passage_ids=(),
        forbidden_cloze_targets=(),
        prompt_sync_stale=False,
        untagged_rate=0,
        covered_concept_ids_by_nid={},
    )

    documented = reconcile_card_centric(
        CardCentricReconciliationInput(**base, t6_selected_nids=tuple(range(1, 11)))
    )
    undocumented = reconcile_card_centric(CardCentricReconciliationInput(**base))

    assert "selection_conservation" in documented.passed
    assert "selection_conservation" in {item.assertion_id for item in undocumented.failed}


def test_card_centric_s9_a11_uses_history_before_bootstrap_bounds() -> None:
    snapshot = CardCentricReconciliationInput(
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
        covered_concept_ids_by_nid={},
        historical_yes_rates=(0.5,),
    )

    report = reconcile_card_centric(snapshot)

    assert "A11" in {item.assertion_id for item in report.warned}


def test_card_centric_s9_rejects_coverage_from_an_unselected_yes_card() -> None:
    snapshot = CardCentricReconciliationInput(
        concept_ids=("C01",),
        coverage={"C01": "uncovered"},
        required_fact_ids=(),
        uncovered_after_s5=(),
        residual_ran_for=(),
        generated_cards=(),
        unresolved_fact_ids=(),
        expected_scoped_nids=(1,),
        classifications=(AuditResolution(nid=1, verdict="keep"),),
        eligible_yes_nids=(1,),
        selected_nids=(),
        selected_generated_card_ids=(),
        generated_card_ids=(),
        source_passage_ids=(),
        forbidden_cloze_targets=(),
        prompt_sync_stale=False,
        untagged_rate=0,
        covered_concept_ids_by_nid={1: ("C01",)},
    )

    report = reconcile_card_centric(snapshot)

    assert "A4" in {item.assertion_id for item in report.failed}


def test_card_centric_s9_accepts_fast_only_coverage_when_the_fast_card_is_selected() -> None:
    snapshot = CardCentricReconciliationInput(
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

    assert "A4" in report.passed


def test_card_centric_s9_allows_review_for_unsigned_exact_mandatory_overflow() -> None:
    mandatory = tuple(range(1, 72))
    snapshot = CardCentricReconciliationInput(
        concept_ids=("C01",),
        coverage={"C01": "covered"},
        required_fact_ids=(),
        uncovered_after_s5=(),
        residual_ran_for=(),
        generated_cards=(),
        unresolved_fact_ids=(),
        expected_scoped_nids=mandatory,
        classifications=tuple(AuditResolution(nid=nid, verdict="keep") for nid in mandatory),
        eligible_yes_nids=mandatory,
        selected_nids=mandatory,
        selected_generated_card_ids=(),
        generated_card_ids=(),
        source_passage_ids=(),
        forbidden_cloze_targets=(),
        prompt_sync_stale=False,
        untagged_rate=0,
        mandatory_nids=mandatory,
        covered_concept_ids_by_nid={note_id: ("C01",) for note_id in mandatory},
    )

    report = reconcile_card_centric(snapshot)

    assert {item.assertion_id for item in report.failed} == {"selection_cap"}
    assert report.can_render_envelope is True


def test_card_centric_s9_blocks_nonmandatory_cards_in_an_overflow_selection() -> None:
    mandatory = tuple(range(1, 72))
    selected = (*mandatory, 72)
    snapshot = CardCentricReconciliationInput(
        concept_ids=("C01",),
        coverage={"C01": "covered"},
        required_fact_ids=(),
        uncovered_after_s5=(),
        residual_ran_for=(),
        generated_cards=(),
        unresolved_fact_ids=(),
        expected_scoped_nids=selected,
        classifications=tuple(AuditResolution(nid=nid, verdict="keep") for nid in selected),
        eligible_yes_nids=selected,
        selected_nids=selected,
        selected_generated_card_ids=(),
        generated_card_ids=(),
        source_passage_ids=(),
        forbidden_cloze_targets=(),
        prompt_sync_stale=False,
        untagged_rate=0,
        mandatory_nids=mandatory,
        covered_concept_ids_by_nid={note_id: ("C01",) for note_id in selected},
    )

    report = reconcile_card_centric(snapshot)

    assert "selection_cap" in {item.assertion_id for item in report.failed}
    assert report.can_render_envelope is False


def test_v1_overflow_can_bind_generated_cards_alongside_mandatory_existing_cards() -> None:
    mandatory = tuple(range(1, 72))
    base = dict(
        concept_ids=("C01",),
        coverage={"C01": "covered"},
        required_fact_ids=("C01-M1",),
        uncovered_after_s5=("C01",),
        residual_ran_for=("C01",),
        generated_cards=(
            GeneratedResolution(
                card_id="G1",
                fact_id="C01-M1",
                text="The result is {{c1::present}}.",
            ),
        ),
        unresolved_fact_ids=(),
        expected_scoped_nids=mandatory,
        classifications=tuple(AuditResolution(nid=nid, verdict="keep") for nid in mandatory),
        eligible_yes_nids=mandatory,
        selected_nids=mandatory,
        selected_generated_card_ids=("G1",),
        generated_card_ids=("G1",),
        source_passage_ids=(),
        forbidden_cloze_targets=(),
        prompt_sync_stale=False,
        untagged_rate=0,
        mandatory_nids=mandatory,
        mandatory_generated_card_ids=(),
        covered_concept_ids_by_nid={note_id: ("C01",) for note_id in mandatory},
        generated_concept_id_by_card_id={"G1": "C01"},
    )

    v1 = reconcile_card_centric(CardCentricReconciliationInput(**base))
    v2 = reconcile_card_centric(
        CardCentricReconciliationInput(**base, pipeline_contract_version="card_centric_v2")
    )

    assert {item.assertion_id for item in v1.failed} == {"selection_cap"}
    assert v1.can_render_envelope is True
    assert "selection_cap" in {item.assertion_id for item in v2.failed}
    assert v2.can_render_envelope is False
