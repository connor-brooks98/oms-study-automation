import gzip
import json
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Protocol, TextIO

from oms_hub.anki.contracts import (
    SnapshotManifest,
    SnapshotNote,
    canonical_payload_sha256,
)
from oms_hub.anki.ledger import AnkiLedger
from oms_hub.anki.normalize import extract_media_references
from oms_hub.anki.snapshot import (
    SnapshotValidationError,
    hash_content_sequence,
    hash_id_set,
    snapshot_note_hash,
)

SOURCE_DECK = "Anking Step Deck"
SOURCE_DECK_QUERY = 'deck:"Anking Step Deck"'
EXPORT_VERSION = "1"


@dataclass(frozen=True, slots=True)
class SnapshotVersions:
    export_version: str
    normalizer_version: str
    embedding_model: str


@dataclass(frozen=True, slots=True)
class DeltaSnapshotPlan:
    full_export: bool
    reason: str
    edit_query: str | None = None
    added_note_ids: tuple[int, ...] = ()
    deleted_note_ids: tuple[int, ...] = ()


class DeltaSnapshotPlanner:
    def __init__(
        self,
        ledger: AnkiLedger,
        *,
        safety_margin: timedelta = timedelta(hours=2),
        maximum_window: timedelta = timedelta(days=7),
    ) -> None:
        self.ledger = ledger
        self.safety_margin = safety_margin
        self.maximum_window = maximum_window

    def plan(
        self,
        *,
        current_note_ids: Sequence[int],
        now: datetime,
        versions: SnapshotVersions,
    ) -> DeltaSnapshotPlan:
        stored_hashes = self.ledger.note_hashes()
        state = self.ledger.snapshot_state()
        if not stored_hashes or state is None:
            return DeltaSnapshotPlan(full_export=True, reason="ledger_absent")
        if int(state.get("note_count", -1)) != len(stored_hashes):
            return DeltaSnapshotPlan(
                full_export=True,
                reason="ledger_count_mismatch",
            )
        stored_versions = (
            state.get("export_version"),
            state.get("normalizer_version"),
            state.get("embedding_model"),
        )
        if stored_versions != (
            versions.export_version,
            versions.normalizer_version,
            versions.embedding_model,
        ):
            return DeltaSnapshotPlan(full_export=True, reason="version_changed")
        try:
            exported_at = datetime.fromisoformat(str(state["exported_at"]))
        except (KeyError, ValueError):
            return DeltaSnapshotPlan(full_export=True, reason="ledger_invalid")
        elapsed = now - exported_at
        if (
            elapsed < timedelta()
            or elapsed + self.safety_margin > self.maximum_window
        ):
            return DeltaSnapshotPlan(full_export=True, reason="unsafe_window")
        current = set(current_note_ids)
        if any(note_id <= 0 for note_id in current):
            raise ValueError("current note IDs must be positive")
        prior = set(stored_hashes)
        days = max(
            1,
            math.ceil((elapsed + self.safety_margin) / timedelta(days=1)),
        )
        return DeltaSnapshotPlan(
            full_export=False,
            reason="delta_safe",
            edit_query=f"{SOURCE_DECK_QUERY} edited:{days}",
            added_note_ids=tuple(sorted(current - prior)),
            deleted_note_ids=tuple(sorted(prior - current)),
        )


class SnapshotAnki(Protocol):
    def version(self) -> int: ...

    def find_notes(self, query: str) -> list[int]: ...

    def notes_info(
        self,
        note_ids: Sequence[int],
    ) -> list[dict[str, Any]]: ...


class FullSnapshotExporter:
    def __init__(
        self,
        *,
        anki: SnapshotAnki,
        chunk_size: int = 250,
        producer_version: str,
    ) -> None:
        if chunk_size < 1:
            raise ValueError("chunk_size must be positive")
        if not producer_version:
            raise ValueError("producer_version cannot be empty")
        self.anki = anki
        self.chunk_size = chunk_size
        self.producer_version = producer_version

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
        with _open_output(destination) as output:
            for offset in range(0, len(note_ids), self.chunk_size):
                chunk = note_ids[offset : offset + self.chunk_size]
                records = self.anki.notes_info(chunk)
                if len(records) != len(chunk):
                    raise ValueError(
                        "Anki returned an incomplete notesInfo chunk"
                    )
                parsed = sorted(
                    (_snapshot_note(record) for record in records),
                    key=lambda note: note.note_id,
                )
                if [note.note_id for note in parsed] != chunk:
                    raise ValueError(
                        "Anki returned mismatched notesInfo IDs"
                    )
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
        id_set_sha256 = hash_id_set(note_ids)
        content_sha256 = hash_content_sequence(content_hashes)
        timestamp = exported_at or datetime.now(UTC)
        values: dict[str, object] = {
            "contract_version": 1,
            "snapshot_id": (
                f"full-{id_set_sha256[:12]}-{content_sha256[:12]}"
            ),
            "source_deck": SOURCE_DECK,
            "note_count": len(note_ids),
            "id_set_sha256": id_set_sha256,
            "content_sha256": content_sha256,
            "export_version": EXPORT_VERSION,
            "producer_version": self.producer_version,
            "ankiconnect_version": self.anki.version(),
            "exported_at": timestamp,
            "payload_sha256": "0" * 64,
        }
        provisional = SnapshotManifest.model_validate(values)
        return provisional.model_copy(
            update={
                "payload_sha256": canonical_payload_sha256(provisional)
            }
        )


def snapshot_note_hashes(path: Path) -> dict[int, str]:
    result: dict[int, str] = {}
    previous_note_id = 0
    with _open_input(path) as stream:
        for line in stream:
            if not line.strip():
                continue
            try:
                raw = json.loads(line)
                note = SnapshotNote.model_validate(raw)
            except (json.JSONDecodeError, ValueError) as exc:
                raise SnapshotValidationError(
                    "snapshot contains an invalid row"
                ) from exc
            if snapshot_note_hash(raw) != note.content_sha256:
                raise SnapshotValidationError("snapshot note hash mismatch")
            if note.note_id <= previous_note_id:
                raise SnapshotValidationError(
                    "snapshot note IDs must be unique and sorted"
                )
            result[note.note_id] = note.content_sha256
            previous_note_id = note.note_id
    return result


def _snapshot_note(raw: dict[str, Any]) -> SnapshotNote:
    try:
        note_id = _positive_int(raw["noteId"])
        model_name = str(raw["modelName"]).strip()
        raw_fields = raw["fields"]
        raw_tags = raw["tags"]
        raw_cards = raw["cards"]
    except KeyError as exc:
        raise ValueError(
            "notesInfo record is missing a required field"
        ) from exc
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
    if not isinstance(raw_tags, list) or not all(
        isinstance(tag, str) for tag in raw_tags
    ):
        raise TypeError("notesInfo record has invalid tags")
    if not isinstance(raw_cards, list):
        raise TypeError("notesInfo record has invalid cards")
    cards = tuple(sorted({_positive_int(card) for card in raw_cards}))
    tags = tuple(sorted(set(raw_tags), key=str.casefold))
    media = tuple(
        item.filename for item in extract_media_references(fields)
    )
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


def _open_output(path: Path) -> TextIO:
    if path.suffix.casefold() == ".gz":
        return gzip.open(path, "wt", encoding="utf-8", newline="\n")
    return path.open("w", encoding="utf-8", newline="\n")


def _open_input(path: Path) -> TextIO:
    if path.suffix.casefold() == ".gz":
        return gzip.open(path, "rt", encoding="utf-8")
    return path.open("r", encoding="utf-8")
