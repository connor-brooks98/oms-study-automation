"""Local, recoverable application of immutable V1/V2 Hub envelopes."""

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Protocol

from oms_anki_agent.ledger import AgentLedger
from oms_hub.anki.contracts import (
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
    def find_notes(self, query: str) -> list[int]: ...


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
        sync_status, verified = "complete", False
        for operation in envelope.operations:
            state, result = self.ledger.begin_operation(
                operation.operation_id, operation.content_sha256
            )
            if state != "completed":
                # An intent can be left by a process crash. Every operation is
                # checked for its postcondition before another mutation is sent.
                result = self._recover_or_apply(operation)
                self.ledger.complete_operation(
                    operation.operation_id, operation.content_sha256, result
                )
            assert result is not None
            if isinstance(operation, AddNotesOperation):
                created.extend(int(value) for value in result.get("note_ids", []))
            if "filename" in result:
                media.append(str(result["filename"]))
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

    def _recover_or_apply(self, operation: object) -> dict[str, Any]:
        if isinstance(operation, StoreMediaOperation):
            return {
                "filename": self.anki.store_media_file(operation.filename, operation.content_base64)
            }
        if isinstance(operation, RemoveTagsOperation):
            if not self._tags_match(operation.note_ids, operation.tag, False):
                self.anki.remove_tags(operation.note_ids, (operation.tag,))
            if not self._tags_match(operation.note_ids, operation.tag, False):
                raise ValueError("remove-tags postcondition failed")
            return {"note_ids": list(operation.note_ids), "tag": operation.tag}
        if isinstance(operation, AddTagsOperation):
            if not self._tags_match(operation.note_ids, operation.tag, True):
                self.anki.add_tags(operation.note_ids, (operation.tag,))
            if not self._tags_match(operation.note_ids, operation.tag, True):
                raise ValueError("add-tags postcondition failed")
            return {"note_ids": list(operation.note_ids), "tag": operation.tag}
        if isinstance(operation, AddNotesOperation):
            # Preflight every marker before adding anything. A mixed crash
            # recovery must never re-send a recovered note or proceed past an
            # ambiguous idempotency marker.
            note_ids: list[int | None] = []
            missing: list[dict[str, Any]] = []
            missing_positions: list[int] = []
            for position, note in enumerate(operation.notes):
                marker = self._marker(note)
                matches = self.anki.find_notes(f"tag:{marker}")
                if len(matches) > 1:
                    raise ValueError("generated-note marker is ambiguous; no new notes were added")
                if matches:
                    note_ids.append(matches[0])
                else:
                    note_ids.append(None)
                    missing.append(note)
                    missing_positions.append(position)
            if missing:
                added = self.anki.add_notes(tuple(missing))
                if len(added) != len(missing):
                    raise ValueError("Anki did not create every missing generated note")
                for position, note_id in zip(missing_positions, added, strict=True):
                    note_ids[position] = note_id
            if any(note_id is None for note_id in note_ids):
                raise ValueError("generated-note recovery did not resolve every note")
            resolved = [note_id for note_id in note_ids if note_id is not None]
            self._verify_generated(operation.notes, resolved)
            return {"note_ids": resolved}
        if isinstance(operation, SyncOperation):
            self.anki.sync()
            return {"status": "complete"}
        if isinstance(operation, VerifyOperation):
            if len(self.anki.notes_info(operation.note_ids)) != len(operation.note_ids):
                raise ValueError("Anki did not return every verification note")
            return {"verified": True}
        raise ValueError("unsupported envelope operation")

    def _tags_match(self, note_ids: tuple[int, ...], tag: str, present: bool) -> bool:
        notes = self.anki.notes_info(note_ids)
        return len(notes) == len(note_ids) and all(
            (tag.casefold() in {str(value).casefold() for value in note.get("tags", [])}) is present
            for note in notes
        )

    @staticmethod
    def _marker(note: dict[str, Any]) -> str:
        marker = next((str(tag) for tag in note.get("tags", []) if "Envelope_" in str(tag)), "")
        if not marker:
            raise ValueError("generated note has no deterministic envelope marker")
        return marker

    def _verify_generated(self, expected: tuple[dict[str, Any], ...], note_ids: list[int]) -> None:
        actual = self.anki.notes_info(tuple(note_ids))
        if len(actual) != len(expected):
            raise ValueError("generated-note verification did not return every note")
        for wanted, observed in zip(expected, actual, strict=True):
            tags = {str(value).casefold() for value in observed.get("tags", [])}
            if not {str(value).casefold() for value in wanted.get("tags", [])} <= tags:
                raise ValueError("generated-note tags do not match envelope")
            fields = observed.get("fields", {})
            for name, value in wanted.get("fields", {}).items():
                seen = fields.get(name, {})
                if isinstance(seen, dict):
                    seen = seen.get("value", "")
                if str(seen) != str(value):
                    raise ValueError("generated-note fields do not match envelope")
            expected_deck = wanted.get("deckName")
            observed_deck = observed.get("deckName")
            if observed_deck is not None and str(observed_deck) != str(expected_deck):
                raise ValueError("generated-note deck does not match envelope")
