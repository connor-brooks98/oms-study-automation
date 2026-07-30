import json
from pathlib import Path

import pytest

from oms_hub.anki.evaluation import (
    ABLATION_NAMES,
    EvaluationDataset,
    evaluate_dataset,
)

FIXTURE = (
    Path(__file__).parents[1]
    / "fixtures"
    / "anki"
    / "retrieval_gold.json"
)


def _fixture_payload() -> dict[str, object]:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def test_versioned_gold_set_emits_all_required_metrics() -> None:
    dataset = EvaluationDataset.model_validate_json(
        FIXTURE.read_text(encoding="utf-8")
    )

    report = evaluate_dataset(dataset)

    assert report.schema_version == 1
    assert report.dataset_version == "anki-retrieval-gold-v1"
    assert set(report.retrieval) == set(ABLATION_NAMES)
    assert report.retrieval["fused"].recall_at_5 == 1.0
    assert report.retrieval["fused"].recall_at_10 == 1.0
    assert report.retrieval["fused"].mrr == 1.0
    assert report.retrieval["fused"].ndcg_at_10 == 1.0
    assert report.pass_1.coverage_precision == 1.0
    assert report.pass_1.coverage_recall == pytest.approx(0.6)
    assert report.pass_2.recovery_rate == 1.0
    assert report.pass_2.false_recovery_rate == 0.0
    assert report.gap_proposals.precision == 1.0
    assert report.semantic.coverage == 1.0
    assert report.timing.query_p50_ms > 0
    assert report.timing.query_p95_ms >= report.timing.query_p50_ms
    assert report.timing.extrapolated_68k.snapshot_size_bytes > 0
    assert report.gates.automated.status == "pass"
    assert report.gates.copied_profile.status == "pending"
    assert report.gates.release_ready is False

    markdown = report.to_markdown()
    assert "Recall@5" in markdown
    assert "Pass 1 coverage" in markdown
    assert "Copied-profile acceptance" in markdown


def test_automated_gate_detects_filter_evidence_and_apply_violations() -> None:
    payload = _fixture_payload()
    queries = payload["queries"]
    assert isinstance(queries, list)
    first = queries[0]
    gap = queries[5]
    assert isinstance(first, dict)
    assert isinstance(gap, dict)
    rankings = first["rankings"]
    proposal = gap["gap_proposal"]
    acceptance = payload["acceptance"]
    assert isinstance(rankings, dict)
    assert isinstance(proposal, dict)
    assert isinstance(acceptance, dict)
    fused = rankings["fused"]
    assert isinstance(fused, list)
    fused.append(999_999)
    proposal["evidence_ids"] = ["missing-evidence"]
    acceptance["protected_tag_mutation_count"] = 1
    acceptance["duplicate_notes_after_retry"] = 1

    report = evaluate_dataset(EvaluationDataset.model_validate(payload))

    assert report.guardrails.eligible_filter_leaks == 1
    assert report.guardrails.unsupported_gap_proposals == 1
    assert report.guardrails.protected_tag_mutations == 1
    assert report.guardrails.duplicate_notes_after_retry == 1
    assert report.gates.automated.status == "fail"
    assert report.gates.release_ready is False


def test_copied_profile_requires_every_manual_acceptance_scenario() -> None:
    payload = _fixture_payload()
    profile = payload["profile"]
    acceptance = payload["acceptance"]
    assert isinstance(profile, dict)
    assert isinstance(acceptance, dict)
    profile.update(
        {
            "kind": "copied_profile",
            "label_provenance": "manual_copied_profile",
            "copied_via_supported_backup": True,
            "production_profile_untouched": True,
        }
    )
    acceptance.update(
        {
            "research_addons_unavailable": True,
            "leading_sync_failure_no_writes": True,
            "trailing_sync_failure_recorded": True,
            "retry_completed_without_reapply": True,
            "read_back_verified": True,
        }
    )

    passed = evaluate_dataset(EvaluationDataset.model_validate(payload))

    assert passed.gates.copied_profile.status == "pass"
    assert passed.gates.release_ready is True

    acceptance["trailing_sync_failure_recorded"] = False
    failed = evaluate_dataset(EvaluationDataset.model_validate(payload))

    assert failed.gates.copied_profile.status == "fail"
    assert failed.gates.release_ready is False


def test_gold_set_rejects_missing_ablation() -> None:
    payload = _fixture_payload()
    queries = payload["queries"]
    assert isinstance(queries, list)
    first = queries[0]
    assert isinstance(first, dict)
    rankings = first["rankings"]
    assert isinstance(rankings, dict)
    del rankings["statement_only"]

    with pytest.raises(ValueError, match="all retrieval ablations"):
        EvaluationDataset.model_validate(payload)
