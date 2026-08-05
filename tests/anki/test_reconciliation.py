from pathlib import Path

from oms_hub.anki.reconciliation import (
    AuditResolution,
    ConceptResolution,
    GeneratedResolution,
    ReconciliationInput,
    reconcile,
)

FIXTURE = Path(__file__).parent / "fixtures" / "lecture_07_v1_reconciliation.json"


def test_v1_zero_gap_output_fails_fact_reconciliation() -> None:
    snapshot = ReconciliationInput.model_validate_json(
        FIXTURE.read_text(encoding="utf-8")
    )

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


def test_high_audit_drop_rate_warns_without_blocking_review() -> None:
    expected_ids = tuple(range(1, 21))
    snapshot = ReconciliationInput(
        concepts=(),
        generated_cards=(),
        unresolved_fact_ids=(),
        expected_audit_nids=expected_ids,
        audit_verdicts=tuple(
            AuditResolution(
                nid=note_id,
                verdict="keep" if note_id <= 10 else "drop",
            )
            for note_id in expected_ids
        ),
        source_passage_ids=(),
        forbidden_cloze_targets=(),
        prompt_sync_stale=False,
    )

    report = reconcile(snapshot)

    assert "A6" in {item.assertion_id for item in report.warned}
    assert "A6" not in {item.assertion_id for item in report.failed}
    assert report.can_render_envelope is True
