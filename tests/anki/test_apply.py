from copy import deepcopy
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest

from oms_hub.anki.ankiconnect import AnkiConnectUnavailable
from oms_hub.anki.apply import LocalEnvelopeExecutor, StaleEnvelopeError
from oms_hub.anki.contracts import (
    ActionEnvelope,
    AddNotesOperation,
    AddTagsOperation,
    StoreMediaOperation,
    SyncOperation,
    VerifyOperation,
    canonical_payload_sha256,
)
from oms_hub.anki.domain import StoredEnvelopeOperation
from oms_hub.anki.ledger import AnkiLedger

ENVELOPE_ID = UUID("0a0de74a-a60b-41e3-808e-e89974b0f615")
MEDIA_ID = UUID("46b479b2-e574-4bb8-a8d0-0b58170df646")
TAG_ID = UUID("12dbcf36-32fc-42a2-979d-31096ab4f413")
ADD_ID = UUID("2867f393-0ae3-4935-bc85-55bfdbfbec82")
SYNC_ID = UUID("10d845ad-4837-4992-b6d5-ed2fc3855672")
VERIFY_ID = UUID("8f58e8c9-3e80-4d78-a879-c07243e56a88")
TARGET_TAG = (
    "AnkiHub_Optional::LMU_OMS_II::HemeLymph::Block1::Lec4_Anemia_I"
)
MARKER_TAG = f"OMSStudyHub_Operation::{ADD_ID}"


def _envelope() -> ActionEnvelope:
    operations = (
        StoreMediaOperation(
            operation_id=MEDIA_ID,
            filename="oms_anki_0123456789abcdef.png",
            content_base64="aGVsbG8=",
            sha256="1" * 64,
            content_sha256="2" * 64,
        ),
        AddTagsOperation(
            operation_id=TAG_ID,
            note_ids=(101,),
            tag=TARGET_TAG,
            content_sha256="3" * 64,
        ),
        AddNotesOperation(
            operation_id=ADD_ID,
            notes=(
                {
                    "deckName": (
                        "OMS-II_Custom_Cards::Heme_Lymph::"
                        "Exam_1::Lec4_Anemia_I"
                    ),
                    "modelName": "AnKingOverhaul (OMS_II_Extra/JCBrooks)",
                    "fields": {
                        "Text": "{{c1::Anemia}}",
                        "Extra": "Explanation",
                    },
                    "tags": [TARGET_TAG],
                },
            ),
            content_sha256="4" * 64,
        ),
        SyncOperation(
            operation_id=SYNC_ID,
            content_sha256="5" * 64,
        ),
        VerifyOperation(
            operation_id=VERIFY_ID,
            note_ids=(101,),
            content_sha256="6" * 64,
        ),
    )
    payload = {
        "envelope_id": str(ENVELOPE_ID),
        "snapshot_id": "snapshot-1",
        "target_deck": (
            "OMS-II_Custom_Cards::Heme_Lymph::Exam_1::Lec4_Anemia_I"
        ),
        "target_tag": TARGET_TAG,
        "touched_note_hashes": {"101": "a" * 64},
        "operations": [
            operation.model_dump(mode="json") for operation in operations
        ],
    }
    return ActionEnvelope(
        envelope_id=ENVELOPE_ID,
        snapshot_id="snapshot-1",
        target_deck=payload["target_deck"],
        target_tag=TARGET_TAG,
        touched_note_hashes={101: "a" * 64},
        operations=operations,
        payload_sha256=canonical_payload_sha256(payload),
    )


class FakeAnki:
    def __init__(
        self,
        *,
        current_hash: str = "a" * 64,
        sync_error: Exception | None = None,
        drop_target_tag_after_sync: bool = False,
    ) -> None:
        self.notes: dict[int, dict[str, Any]] = {
            101: {
                "noteId": 101,
                "tags": [],
                "current_hash": current_hash,
            }
        }
        self.next_note_id = 501
        self.sync_error = sync_error
        self.drop_target_tag_after_sync = drop_target_tag_after_sync
        self.actions: list[str] = []
        self.add_notes_calls = 0
        self.sync_calls = 0

    def notes_info(self, note_ids: list[int] | tuple[int, ...]) -> list[dict[str, Any]]:
        self.actions.append("notes_info")
        return [
            deepcopy(self.notes[note_id])
            for note_id in note_ids
            if note_id in self.notes
        ]

    def find_notes(self, query: str) -> list[int]:
        self.actions.append("find_notes")
        if not query.startswith("tag:"):
            return []
        marker = query.removeprefix("tag:")
        return [
            note_id
            for note_id, note in self.notes.items()
            if marker in note["tags"]
        ]

    def store_media_file(self, filename: str, data_base64: str) -> str:
        self.actions.append("store_media")
        assert data_base64 == "aGVsbG8="
        return filename

    def add_tags(self, note_ids: tuple[int, ...], tags: tuple[str, ...]) -> None:
        self.actions.append("add_tags")
        for note_id in note_ids:
            self.notes[note_id]["tags"].extend(tags)

    def add_notes(self, notes: list[dict[str, Any]]) -> list[int]:
        self.actions.append("add_notes")
        self.add_notes_calls += 1
        result: list[int] = []
        for note in notes:
            note_id = self.next_note_id
            self.next_note_id += 1
            self.notes[note_id] = {
                "noteId": note_id,
                "tags": list(note["tags"]),
                "current_hash": "generated",
            }
            result.append(note_id)
        return result

    def sync(self) -> None:
        self.actions.append("sync")
        self.sync_calls += 1
        if self.sync_error is not None:
            raise self.sync_error
        if self.drop_target_tag_after_sync:
            self.notes[101]["tags"] = []


class MemoryOperationStore:
    def __init__(self, envelope: ActionEnvelope) -> None:
        self.operations = {
            operation.operation_id: StoredEnvelopeOperation(
                id=operation.operation_id,
                envelope_id=envelope.envelope_id,
                operation_type=operation.operation_type,
                content_hash=operation.content_sha256,
                payload=operation.model_dump(mode="json"),
                state="pending",
                attempts=0,
                result=None,
                error=None,
            )
            for operation in envelope.operations
        }
        self.receipt: dict[str, Any] | None = None

    def start_envelope_operation(
        self,
        envelope_id: UUID,
        operation_id: UUID,
    ) -> StoredEnvelopeOperation:
        current = self.operations[operation_id]
        if current.state == "complete":
            return current
        updated = StoredEnvelopeOperation(
            id=current.id,
            envelope_id=envelope_id,
            operation_type=current.operation_type,
            content_hash=current.content_hash,
            payload=current.payload,
            state="applying",
            attempts=current.attempts + 1,
            result=None,
            error=None,
        )
        self.operations[operation_id] = updated
        return updated

    def complete_envelope_operation(
        self,
        envelope_id: UUID,
        operation_id: UUID,
        result: dict[str, Any],
    ) -> StoredEnvelopeOperation:
        current = self.operations[operation_id]
        updated = StoredEnvelopeOperation(
            id=current.id,
            envelope_id=envelope_id,
            operation_type=current.operation_type,
            content_hash=current.content_hash,
            payload=current.payload,
            state="complete",
            attempts=current.attempts,
            result=result,
            error=None,
        )
        self.operations[operation_id] = updated
        return updated

    def fail_envelope_operation(
        self,
        envelope_id: UUID,
        operation_id: UUID,
        safe_error: str,
        *,
        retryable: bool,
    ) -> StoredEnvelopeOperation:
        current = self.operations[operation_id]
        updated = StoredEnvelopeOperation(
            id=current.id,
            envelope_id=envelope_id,
            operation_type=current.operation_type,
            content_hash=current.content_hash,
            payload=current.payload,
            state="retryable" if retryable else "failed",
            attempts=current.attempts,
            result=None,
            error=safe_error,
        )
        self.operations[operation_id] = updated
        return updated

    def operation_results(self, envelope_id: UUID) -> dict[str, dict[str, Any]]:
        return {
            str(operation_id): operation.result
            for operation_id, operation in self.operations.items()
            if operation.state == "complete" and operation.result is not None
        }

    def record_receipt(
        self,
        envelope_id: UUID,
        receipt: dict[str, Any],
    ) -> None:
        self.receipt = receipt


def _executor(
    tmp_path: Path,
    anki: FakeAnki,
    envelope: ActionEnvelope,
) -> LocalEnvelopeExecutor:
    return LocalEnvelopeExecutor(
        anki=anki,
        ledger=AnkiLedger(tmp_path / "ledger.sqlite3"),
        operations=MemoryOperationStore(envelope),
        note_hasher=lambda note: str(note["current_hash"]),
    )


def test_executor_applies_media_tags_notes_sync_and_verify_in_order(
    tmp_path: Path,
) -> None:
    envelope = _envelope()
    anki = FakeAnki()

    receipt = _executor(tmp_path, anki, envelope).execute(envelope)

    assert anki.actions == [
        "notes_info",
        "store_media",
        "add_tags",
        "find_notes",
        "add_notes",
        "sync",
        "notes_info",
    ]
    assert receipt.executor_id == "nuc-local"
    assert receipt.sync_status == "complete"
    assert receipt.verified is True
    assert receipt.created_note_ids == (501,)
    assert receipt.media_filenames == ("oms_anki_0123456789abcdef.png",)


def test_executor_replay_does_not_duplicate_generated_notes_or_sync(
    tmp_path: Path,
) -> None:
    envelope = _envelope()
    anki = FakeAnki()
    executor = _executor(tmp_path, anki, envelope)

    first = executor.execute(envelope)
    second = executor.execute(envelope)

    assert second == first
    assert anki.add_notes_calls == 1
    assert anki.sync_calls == 1


def test_executor_replay_accepts_its_own_previously_applied_tag(
    tmp_path: Path,
) -> None:
    envelope = _envelope()
    anki = FakeAnki()
    executor = LocalEnvelopeExecutor(
        anki=anki,
        ledger=AnkiLedger(tmp_path / "ledger.sqlite3"),
        operations=MemoryOperationStore(envelope),
        note_hasher=lambda note: (
            "b" * 64 if TARGET_TAG in note["tags"] else "a" * 64
        ),
    )

    first = executor.execute(envelope)
    second = executor.execute(envelope)

    assert second == first
    assert anki.add_notes_calls == 1
    assert anki.sync_calls == 1


def test_executor_refuses_stale_existing_note_before_any_write(
    tmp_path: Path,
) -> None:
    envelope = _envelope()
    anki = FakeAnki(current_hash="b" * 64)

    with pytest.raises(StaleEnvelopeError, match="changed since indexing"):
        _executor(tmp_path, anki, envelope).execute(envelope)

    assert anki.actions == ["notes_info"]


def test_executor_does_not_complete_when_post_sync_verification_fails(
    tmp_path: Path,
) -> None:
    envelope = _envelope()
    anki = FakeAnki(drop_target_tag_after_sync=True)

    receipt = _executor(tmp_path, anki, envelope).execute(envelope)

    assert receipt.sync_status == "complete"
    assert receipt.verified is False
    assert receipt.safe_error == "post-sync verification failed"


def test_executor_marks_sync_unavailability_retryable(tmp_path: Path) -> None:
    envelope = _envelope()
    anki = FakeAnki(sync_error=AnkiConnectUnavailable("offline"))

    receipt = _executor(tmp_path, anki, envelope).execute(envelope)

    assert receipt.sync_status == "retryable"
    assert receipt.verified is False
    assert receipt.safe_error == "AnkiConnect is unavailable during sync"


def test_executor_recovers_created_notes_from_marker_before_replay(
    tmp_path: Path,
) -> None:
    envelope = _envelope()
    anki = FakeAnki()
    anki.notes[501] = {
        "noteId": 501,
        "tags": [TARGET_TAG, MARKER_TAG],
        "current_hash": "generated",
    }

    receipt = _executor(tmp_path, anki, envelope).execute(envelope)

    assert receipt.created_note_ids == (501,)
    assert anki.add_notes_calls == 0
