"""Apply curated Anki envelopes directly through the NUC's AnkiConnect."""

from __future__ import annotations

import threading
from collections.abc import Callable, Sequence
from copy import deepcopy
from typing import Any, Literal, Protocol, cast
from uuid import UUID

from oms_hub.anki.ankiconnect import AnkiConnectError, AnkiConnectUnavailable
from oms_hub.anki.contracts import (
    ActionEnvelope,
    AddNotesOperation,
    AddTagsOperation,
    EnvelopeReceipt,
    Operation,
    OperationReceipt,
    StoreMediaOperation,
    SyncOperation,
    VerifyOperation,
    canonical_payload_sha256,
)
from oms_hub.anki.domain import StoredEnvelopeOperation
from oms_hub.anki.ledger import AnkiLedger
from oms_hub.anki.normalize import normalize_snapshot_note
from oms_hub.anki.snapshot_export import snapshot_note_from_info


class StaleEnvelopeError(RuntimeError):
    """Anki changed after curation and the envelope is unsafe to apply."""


class AnkiApplyClient(Protocol):
    def notes_info(self, note_ids: Sequence[int]) -> list[dict[str, Any]]: ...

    def find_notes(self, query: str) -> list[int]: ...

    def store_media_file(self, filename: str, data_base64: str) -> str: ...

    def add_tags(self, note_ids: Sequence[int], tags: Sequence[str]) -> None: ...

    def add_notes(self, notes: Sequence[dict[str, Any]]) -> list[int]: ...

    def sync(self) -> None: ...


class OperationStore(Protocol):
    def start_envelope_operation(
        self,
        envelope_id: UUID,
        operation_id: UUID,
    ) -> StoredEnvelopeOperation: ...

    def complete_envelope_operation(
        self,
        envelope_id: UUID,
        operation_id: UUID,
        result: dict[str, Any],
    ) -> StoredEnvelopeOperation: ...

    def fail_envelope_operation(
        self,
        envelope_id: UUID,
        operation_id: UUID,
        safe_error: str,
        *,
        retryable: bool,
    ) -> StoredEnvelopeOperation: ...

    def record_receipt(
        self,
        envelope_id: UUID,
        receipt: dict[str, Any],
    ) -> None: ...


def current_note_content_hash(note: dict[str, Any]) -> str:
    """Return the same canonical hash used when the snapshot was indexed."""

    return normalize_snapshot_note(snapshot_note_from_info(note)).content_sha256


class LocalEnvelopeExecutor:
    """Execute an action envelope against the NUC-local Anki instance."""

    executor_id: Literal["nuc-local"] = "nuc-local"

    def __init__(
        self,
        *,
        anki: AnkiApplyClient,
        operations: OperationStore,
        ledger: AnkiLedger,
        note_hasher: Callable[[dict[str, Any]], str] = current_note_content_hash,
        lock: threading.RLock | None = None,
    ) -> None:
        self._anki = anki
        self._operations = operations
        self._ledger = ledger
        self._note_hasher = note_hasher
        self._lock = lock or threading.RLock()

    def execute(self, envelope: ActionEnvelope) -> EnvelopeReceipt:
        with self._lock:
            return self._execute(envelope)

    def _execute(self, envelope: ActionEnvelope) -> EnvelopeReceipt:
        self._verify_preconditions(envelope)
        receipts: list[OperationReceipt] = []
        created_note_ids: list[int] = []
        media_filenames: list[str] = []
        sync_status: Literal["complete", "retryable", "failed"] = "failed"
        verified = False
        safe_error: str | None = None

        for operation in envelope.operations:
            result = self._replayed_result(envelope, operation)
            if result is None:
                try:
                    result = self._apply_operation(
                        operation,
                        created_note_ids,
                        envelope.target_tag,
                    )
                except AnkiConnectUnavailable:
                    error = (
                        "AnkiConnect is unavailable during sync"
                        if isinstance(operation, SyncOperation)
                        else "AnkiConnect is unavailable during local apply"
                    )
                    self._operations.fail_envelope_operation(
                        envelope.envelope_id,
                        operation.operation_id,
                        error,
                        retryable=True,
                    )
                    receipts.append(
                        OperationReceipt(
                            operation_id=operation.operation_id,
                            status="retryable",
                            result={},
                            error=error,
                        )
                    )
                    sync_status = "retryable"
                    safe_error = error
                    break
                except (AnkiConnectError, RuntimeError, TypeError, ValueError):
                    error = "local Anki operation failed"
                    self._operations.fail_envelope_operation(
                        envelope.envelope_id,
                        operation.operation_id,
                        error,
                        retryable=False,
                    )
                    receipts.append(
                        OperationReceipt(
                            operation_id=operation.operation_id,
                            status="failed",
                            result={},
                            error=error,
                        )
                    )
                    safe_error = error
                    break

                if isinstance(operation, VerifyOperation) and not result["verified"]:
                    error = "post-sync verification failed"
                    self._operations.fail_envelope_operation(
                        envelope.envelope_id,
                        operation.operation_id,
                        error,
                        retryable=False,
                    )
                    receipts.append(
                        OperationReceipt(
                            operation_id=operation.operation_id,
                            status="failed",
                            result=result,
                            error=error,
                        )
                    )
                    verified = False
                    safe_error = error
                    break

                self._ledger.record_operation(
                    operation.operation_id,
                    operation.content_sha256,
                    result,
                )
                self._operations.complete_envelope_operation(
                    envelope.envelope_id,
                    operation.operation_id,
                    result,
                )

            receipts.append(
                OperationReceipt(
                    operation_id=operation.operation_id,
                    status="complete",
                    result=result,
                )
            )
            created_note_ids.extend(result.get("created_note_ids", []))
            if filename := result.get("filename"):
                media_filenames.append(str(filename))
            if isinstance(operation, SyncOperation):
                sync_status = cast(
                    Literal["complete", "retryable", "failed"],
                    result["sync_status"],
                )
            if isinstance(operation, VerifyOperation):
                verified = bool(result["verified"])

        receipt = EnvelopeReceipt(
            envelope_id=envelope.envelope_id,
            executor_id=self.executor_id,
            operations=tuple(receipts),
            sync_status=sync_status,
            verified=verified,
            created_note_ids=tuple(created_note_ids),
            media_filenames=tuple(media_filenames),
            safe_error=safe_error,
            payload_sha256="0" * 64,
        )
        receipt = receipt.model_copy(
            update={"payload_sha256": canonical_payload_sha256(receipt)}
        )
        self._operations.record_receipt(
            envelope.envelope_id,
            receipt.model_dump(mode="json"),
        )
        return receipt

    def _verify_preconditions(self, envelope: ActionEnvelope) -> None:
        expected = envelope.touched_note_hashes
        if not expected:
            return
        notes = self._anki.notes_info(sorted(expected))
        actual = {int(note["noteId"]): self._note_hasher(note) for note in notes}
        if set(actual) != set(expected) or any(
            actual[note_id] != content_hash
            for note_id, content_hash in expected.items()
        ):
            raise StaleEnvelopeError("Anki notes changed since indexing")

    def _replayed_result(
        self,
        envelope: ActionEnvelope,
        operation: Operation,
    ) -> dict[str, Any] | None:
        result = self._ledger.operation_result(
            operation.operation_id,
            operation.content_sha256,
        )
        stored = self._operations.start_envelope_operation(
            envelope.envelope_id,
            operation.operation_id,
        )
        if result is not None:
            if stored.state != "complete":
                self._operations.complete_envelope_operation(
                    envelope.envelope_id,
                    operation.operation_id,
                    result,
                )
            return result
        if stored.state == "complete":
            return stored.result
        return None

    def _apply_operation(
        self,
        operation: Operation,
        created_note_ids: list[int],
        target_tag: str,
    ) -> dict[str, Any]:
        if isinstance(operation, StoreMediaOperation):
            return {
                "filename": self._anki.store_media_file(
                    operation.filename,
                    operation.content_base64,
                )
            }
        if isinstance(operation, AddTagsOperation):
            self._anki.add_tags(operation.note_ids, (operation.tag,))
            return {"note_ids": list(operation.note_ids), "tag": operation.tag}
        if isinstance(operation, AddNotesOperation):
            marker = f"OMSStudyHub_Operation::{operation.operation_id}"
            note_ids = self._anki.find_notes(f"tag:{marker}")
            if note_ids and len(note_ids) != len(operation.notes):
                raise RuntimeError("operation marker returned an unexpected note count")
            if not note_ids:
                notes = deepcopy(list(operation.notes))
                for note in notes:
                    tags = note.setdefault("tags", [])
                    if marker not in tags:
                        tags.append(marker)
                note_ids = self._anki.add_notes(notes)
            return {"created_note_ids": note_ids, "marker_tag": marker}
        if isinstance(operation, SyncOperation):
            self._anki.sync()
            return {"sync_status": "complete"}
        if isinstance(operation, VerifyOperation):
            note_ids = list(dict.fromkeys([*operation.note_ids, *created_note_ids]))
            notes = self._anki.notes_info(note_ids)
            by_id = {int(note["noteId"]): note for note in notes}
            target_ok = all(
                target_tag in by_id.get(note_id, {}).get("tags", [])
                for note_id in operation.note_ids
            )
            generated_ok = all(
                any(
                    str(tag).startswith("OMSStudyHub_Operation::")
                    for tag in by_id.get(note_id, {}).get("tags", [])
                )
                for note_id in created_note_ids
            )
            return {
                "verified": (
                    len(by_id) == len(note_ids) and target_ok and generated_ok
                )
            }
        raise TypeError(f"unsupported operation type: {operation.operation_type}")
