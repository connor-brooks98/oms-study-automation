import gzip
import json
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from oms_anki_agent.ledger import AgentLedger
from oms_hub.anki.contracts import (
    SnapshotManifest,
    SnapshotNote,
    canonical_payload_sha256,
)
from oms_hub.anki.normalize import extract_media_references
from oms_hub.anki.snapshot import (
    hash_content_sequence,
    hash_id_set,
    snapshot_note_hash,
)

SOURCE_DECK = "Anking Step Deck"
SOURCE_DECK_QUERY = 'deck:"Anking Step Deck"'
EXPORT_VERSION = "1"


class SnapshotAnki(Protocol):
    def version(self) -> int: ...

    def find_notes(self, query: str) -> list[int]: ...

    def notes_info(self, note_ids: Sequence[int]) -> list[dict[str, Any]]: ...


class FullSnapshotExporter:
    def __init__(
        self,
        *,
        anki: SnapshotAnki,
        chunk_size: int = 250,
        agent_version: str,
        ledger: AgentLedger | None = None,
    ) -> None:
        if chunk_size < 1:
            raise ValueError("chunk_size must be positive")
        self.anki = anki
        self.chunk_size = chunk_size
        self.agent_version = agent_version
        self.ledger = ledger

    def export(
        self,
        destination: Path,
        *,
        exported_at: datetime | None = None,
    ) -> SnapshotManifest:
        note_ids = sorted(set(self.anki.find_notes(SOURCE_DECK_QUERY)))
        if any(note_id <= 0 for note_id in note_ids):
            raise ValueError("Anki returned an invalid note ID")
        destination.parent.mkdir(parents=True, exist_ok=True)
        content_hashes: list[str] = []
        ledger_hashes: dict[int, str] = {}
        with _open_output(destination) as output:
            for offset in range(0, len(note_ids), self.chunk_size):
                chunk = note_ids[offset : offset + self.chunk_size]
                records = self.anki.notes_info(chunk)
                if len(records) != len(chunk):
                    raise ValueError("Anki returned an incomplete notesInfo chunk")
                parsed = sorted((_snapshot_note(record) for record in records), key=_note_id)
                if [note.note_id for note in parsed] != chunk:
                    raise ValueError("Anki returned mismatched notesInfo IDs")
                for note in parsed:
                    output.write(
                        json.dumps(
                            note.model_dump(mode="json"),
                            sort_keys=True,
                            separators=(",", ":"),
                            ensure_ascii=False,
                        )
                        + "\n"
                    )
                    content_hashes.append(note.content_sha256)
                    ledger_hashes[note.note_id] = note.content_sha256
        if self.ledger is not None:
            self.ledger.replace_note_hashes(ledger_hashes)
        id_set_sha256 = hash_id_set(note_ids)
        content_sha256 = hash_content_sequence(content_hashes)
        timestamp = exported_at or datetime.now(UTC)
        snapshot_id = f"full-{id_set_sha256[:12]}-{content_sha256[:12]}"
        values: dict[str, object] = {
            "contract_version": 1,
            "snapshot_id": snapshot_id,
            "source_deck": SOURCE_DECK,
            "note_count": len(note_ids),
            "id_set_sha256": id_set_sha256,
            "content_sha256": content_sha256,
            "export_version": EXPORT_VERSION,
            "agent_version": self.agent_version,
            "ankiconnect_version": self.anki.version(),
            "exported_at": timestamp,
            "payload_sha256": "0" * 64,
        }
        provisional = SnapshotManifest.model_validate(values)
        return provisional.model_copy(
            update={"payload_sha256": canonical_payload_sha256(provisional)}
        )


def _snapshot_note(raw: dict[str, Any]) -> SnapshotNote:
    try:
        note_id = _positive_int(raw["noteId"])
        model_name = str(raw["modelName"]).strip()
        raw_fields = raw["fields"]
        raw_tags = raw["tags"]
        raw_cards = raw["cards"]
    except KeyError as exc:
        raise ValueError("notesInfo record is missing a required field") from exc
    if not model_name or not isinstance(raw_fields, Mapping):
        raise ValueError("notesInfo record has invalid model or fields")
    ordered_fields: list[tuple[int, str, str]] = []
    for name, value in raw_fields.items():
        if not isinstance(name, str) or not isinstance(value, Mapping):
            raise TypeError("notesInfo record has an invalid field")
        field_value = value.get("value")
        field_order = value.get("order")
        if not isinstance(field_value, str) or not isinstance(field_order, int):
            raise TypeError("notesInfo record has an invalid field value")
        ordered_fields.append((field_order, name, field_value))
    ordered_fields.sort()
    fields = {name: value for _, name, value in ordered_fields}
    if not isinstance(raw_tags, list) or not all(isinstance(tag, str) for tag in raw_tags):
        raise TypeError("notesInfo record has invalid tags")
    if not isinstance(raw_cards, list):
        raise TypeError("notesInfo record has invalid cards")
    cards = tuple(sorted({_positive_int(card) for card in raw_cards}))
    tags = tuple(sorted(set(raw_tags), key=str.casefold))
    media = tuple(item.filename for item in extract_media_references(fields))
    values: dict[str, object] = {
        "contract_version": 1,
        "note_id": note_id,
        "model_name": model_name,
        "fields": fields,
        "tags": tags,
        "card_ids": cards,
        "media": media,
    }
    return SnapshotNote.model_validate(
        {**values, "content_sha256": snapshot_note_hash(values)}
    )


def _positive_int(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError("notesInfo record contains an invalid ID")
    return value


def _note_id(note: SnapshotNote) -> int:
    return note.note_id


def _open_output(path: Path) -> Any:
    if path.suffix.casefold() == ".gz":
        return gzip.open(path, "wt", encoding="utf-8", newline="\n")
    return path.open("w", encoding="utf-8", newline="\n")
