"""Local, idempotent application of immutable Hub action envelopes."""

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Protocol

from oms_anki_agent.ledger import AgentLedger
from oms_hub.anki.contracts import (
    ActionEnvelopeDocument,
    AddNotesOperation,
    AddTagsOperation,
    EnvelopeReceipt,
    OperationReceipt,
    RemoveTagsOperation,
    StoreMediaOperation,
    SyncOperation,
    VerifyOperation,
    canonical_payload_sha256,
    parse_action_envelope,
)


class LocalAnki(Protocol):
    def add_tags(self, note_ids: tuple[int, ...], tags: tuple[str, ...]) -> None: ...
    def remove_tags(self, note_ids: tuple[int, ...], tags: tuple[str, ...]) -> None: ...
    def add_notes(self, notes: tuple[dict[str, Any], ...]) -> list[int]: ...
    def store_media_file(self, filename: str, data_base64: str) -> str: ...
    def sync(self) -> None: ...
    def notes_info(self, note_ids: tuple[int, ...]) -> list[dict[str, Any]]: ...


@dataclass(slots=True)
class AgentEnvelopeApplier:
    anki: LocalAnki
    ledger: AgentLedger
    agent_id: str

    def apply(self, raw: dict[str, Any]) -> EnvelopeReceipt:
        envelope = parse_action_envelope(raw)
        if canonical_payload_sha256(envelope) != envelope.payload_sha256:
            raise ValueError("action envelope payload hash does not match")
        receipts: list[OperationReceipt] = []
        created: list[int] = []
        media: list[str] = []
        sync_status = "complete"
        verified = False
        for operation in envelope.operations:
            result = self.ledger.record_operation(
                operation.operation_id,
                operation.content_sha256,
                self._apply_once(envelope, operation, created, media),
            )
            if isinstance(operation, SyncOperation):
                sync_status = str(result.get("status", "complete"))
            if isinstance(operation, VerifyOperation):
                verified = bool(result.get("verified", False))
            receipts.append(
                OperationReceipt(
                    operation_id=operation.operation_id, status="complete", result=result
                )
            )
        document = {
            "envelope_id": str(envelope.envelope_id),
            "agent_id": self.agent_id,
            "operations": [item.model_dump(mode="json") for item in receipts],
            "sync_status": sync_status,
            "verified": verified,
            "created_note_ids": sorted(set(created)),
            "media_filenames": sorted(set(media)),
            "safe_error": None,
        }
        digest = hashlib.sha256(
            json.dumps(document, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        return EnvelopeReceipt.model_validate({**document, "payload_sha256": digest})

    def _apply_once(
        self,
        envelope: ActionEnvelopeDocument,
        operation: object,
        created: list[int],
        media: list[str],
    ) -> dict[str, Any]:
        if isinstance(operation, StoreMediaOperation):
            media.append(self.anki.store_media_file(operation.filename, operation.content_base64))
            return {"filename": operation.filename}
        if isinstance(operation, RemoveTagsOperation):
            self.anki.remove_tags(operation.note_ids, (operation.tag,))
            return {"note_ids": list(operation.note_ids), "tag": operation.tag}
        if isinstance(operation, AddTagsOperation):
            self.anki.add_tags(operation.note_ids, (operation.tag,))
            return {"note_ids": list(operation.note_ids), "tag": operation.tag}
        if isinstance(operation, AddNotesOperation):
            ids = self.anki.add_notes(operation.notes)
            created.extend(ids)
            return {"note_ids": ids}
        if isinstance(operation, SyncOperation):
            self.anki.sync()
            return {"status": "complete"}
        if isinstance(operation, VerifyOperation):
            notes = self.anki.notes_info(operation.note_ids)
            if len(notes) != len(operation.note_ids):
                raise ValueError("Anki did not return every verification note")
            return {"verified": True}
        raise ValueError("unsupported envelope operation")
