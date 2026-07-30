from oms_hub.anki.domain import (
    ApplyState,
    CurationStage,
    CurationState,
    EvidenceSupport,
    RetrievalPass,
)


def test_v4_pipeline_states_are_explicit() -> None:
    assert tuple(state.value for state in CurationState) == (
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
        "deduping",
        "generating_gaps",
        "ready_for_review",
        "envelope_pending",
        "applying_local",
        "syncing",
        "verifying",
        "complete",
        "failed",
    )


def test_v4_stage_and_recovery_vocabularies_are_stable() -> None:
    assert CurationStage.SOURCE_INDEX.value == "source_index"
    assert CurationStage.RETRIEVAL_PASS_2.value == "retrieval_pass_2"
    assert RetrievalPass.PASS_2_RESCUE.value == "pass_2_rescue"
    assert EvidenceSupport.PARTIAL.value == "partial"
    assert ApplyState.APPLIED_LOCAL_SYNC_BLOCKED.value == (
        "applied_local_sync_blocked"
    )
