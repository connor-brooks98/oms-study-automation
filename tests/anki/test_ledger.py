from uuid import UUID

import pytest

from oms_hub.anki.ledger import AnkiLedger, OperationIdentityConflict


def test_ledger_replaces_snapshot_hashes_without_retaining_deleted_notes(
    tmp_path,
) -> None:
    ledger = AnkiLedger(tmp_path / "local.db")

    ledger.replace_note_hashes({11: "a" * 64, 22: "b" * 64})
    ledger.replace_note_hashes({22: "c" * 64, 33: "d" * 64})

    assert ledger.note_hashes() == {22: "c" * 64, 33: "d" * 64}


def test_operation_replay_returns_first_durable_result(tmp_path) -> None:
    ledger = AnkiLedger(tmp_path / "local.db")
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


def test_operation_uuid_reuse_with_different_content_fails_closed(
    tmp_path,
) -> None:
    ledger = AnkiLedger(tmp_path / "local.db")
    operation_id = UUID("12dbcf36-32fc-42a2-979d-31096ab4f413")
    ledger.record_operation(operation_id, "a" * 64, {"status": "complete"})

    with pytest.raises(OperationIdentityConflict, match=str(operation_id)):
        ledger.record_operation(operation_id, "b" * 64, {"status": "complete"})
