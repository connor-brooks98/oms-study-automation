from uuid import UUID

import pytest

from oms_anki_agent.ledger import AgentLedger, OperationIdentityConflict


def test_ledger_replaces_and_reads_snapshot_note_hashes(tmp_path) -> None:
    ledger = AgentLedger(tmp_path / "agent.db")

    ledger.replace_note_hashes({11: "a" * 64, 22: "b" * 64})
    ledger.replace_note_hashes({22: "c" * 64, 33: "d" * 64})

    assert ledger.note_hashes() == {22: "c" * 64, 33: "d" * 64}


def test_operation_replay_returns_recorded_result(tmp_path) -> None:
    ledger = AgentLedger(tmp_path / "agent.db")
    operation_id = UUID("12dbcf36-32fc-42a2-979d-31096ab4f413")

    first = ledger.record_operation(
        operation_id,
        "a" * 64,
        {"created_note_ids": [33]},
    )
    replay = ledger.record_operation(
        operation_id,
        "a" * 64,
        {"created_note_ids": [999]},
    )

    assert first == {"created_note_ids": [33]}
    assert replay == {"created_note_ids": [33]}


def test_operation_uuid_reuse_with_different_hash_fails_closed(tmp_path) -> None:
    ledger = AgentLedger(tmp_path / "agent.db")
    operation_id = UUID("12dbcf36-32fc-42a2-979d-31096ab4f413")
    ledger.record_operation(operation_id, "a" * 64, {"status": "complete"})

    with pytest.raises(OperationIdentityConflict, match=str(operation_id)):
        ledger.record_operation(operation_id, "b" * 64, {"status": "complete"})
