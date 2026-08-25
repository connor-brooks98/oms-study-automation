from oms_hub.anki.domain import (
    ApplyState,
    CurationStage,
    CurationState,
    EvidenceSupport,
    RetrievalPass,
)


def test_v4_pipeline_states_are_explicit() -> None:
    assert tuple(
        state.value
        for state in CurationState
        if not state.value.startswith(("card_", "v3_"))
    ) == (
        "queued",
        "preflight",
        "snapshotting_embeddings",
        "building_companion_index",
        "building_source_index",
        "building_lcl",
        "retrieving_pass_1",
        "judging_pass_1",
        "localizing_missed_concepts",
        "retrieving_pass_2",
        "judging_pass_2",
        "converging_pass_3",
        "converging_pass_4",
        "converging_pass_5",
        "auditing_candidates",
        "recomputing_coverage",
        "deduping",
        "generating_gaps",
        "reconciling",
        "ready_for_review",
        "envelope_pending",
        "applying_local",
        "syncing",
        "verifying",
        "complete",
        "failed",
        "canceled",
        "removed",
    )


def test_v4_stage_and_recovery_vocabularies_are_stable() -> None:
    assert CurationStage.SOURCE_INDEX.value == "source_index"
    assert CurationStage.RETRIEVAL_PASS_2.value == "retrieval_pass_2"
    assert CurationStage.CONVERGENCE_PASS_3.value == "convergence_pass_3"
    assert CurationStage.CONVERGENCE_PASS_5.value == "convergence_pass_5"
    assert CurationStage.CARD_AUDIT.value == "card_audit"
    assert CurationStage.COVERAGE_RECOMPUTE.value == "coverage_recompute"
    assert RetrievalPass.PASS_2_RESCUE.value == "pass_2_rescue"
    assert RetrievalPass.CONVERGENCE.value == "convergence"
    assert EvidenceSupport.PARTIAL.value == "partial"
    assert ApplyState.APPLIED_LOCAL_SYNC_BLOCKED.value == (
        "applied_local_sync_blocked"
    )
