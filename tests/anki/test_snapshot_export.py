import gzip
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from oms_hub.anki.snapshot import SnapshotValidationError, stage_full_snapshot
from oms_hub.anki.snapshot_export import (
    FullSnapshotExporter,
    snapshot_note_hashes,
)


class FakeAnki:
    def __init__(self, notes: list[dict[str, Any]]) -> None:
        self.notes = {int(note["noteId"]): note for note in notes}
        self.queries: list[str] = []
        self.info_calls: list[list[int]] = []

    def version(self) -> int:
        return 6

    def find_notes(self, query: str) -> list[int]:
        self.queries.append(query)
        return list(reversed(self.notes))

    def notes_info(self, note_ids: list[int]) -> list[dict[str, Any]]:
        self.info_calls.append(note_ids)
        return [self.notes[note_id] for note_id in note_ids]


def _fixture_notes() -> list[dict[str, Any]]:
    fixture = Path(__file__).parent / "fixtures" / "anking_notes.json"
    return json.loads(fixture.read_text(encoding="utf-8"))


def test_full_export_streams_sorted_notes_in_bounded_chunks(
    tmp_path: Path,
) -> None:
    anki = FakeAnki(_fixture_notes())
    output = tmp_path / "notes.jsonl.gz"
    exporter = FullSnapshotExporter(
        anki=anki,
        chunk_size=2,
        producer_version="test",
    )

    manifest = exporter.export(
        output,
        exported_at=datetime(2026, 7, 27, 12, tzinfo=UTC),
    )

    assert anki.queries == ['deck:"Anking Step Deck"']
    assert anki.info_calls == [[101, 102], [103]]
    with gzip.open(output, "rt", encoding="utf-8") as stream:
        rows = [json.loads(line) for line in stream]
    assert [row["note_id"] for row in rows] == [101, 102, 103]
    assert rows[0]["media"] == ["warfarin.png", "warfarin.mp3", "inr.jpg"]
    assert rows[0]["tags"] == sorted(rows[0]["tags"])
    assert manifest.note_count == 3
    assert manifest.producer_version == "test"
    assert manifest.ankiconnect_version == 6
    assert manifest.payload_sha256 != "0" * 64
    assert set(snapshot_note_hashes(output)) == {101, 102, 103}


def test_full_export_preserves_safe_unicode_anki_media_filenames(
    tmp_path: Path,
) -> None:
    notes = _fixture_notes()
    filename = "University of Michigan’s Pressure–Volume Loops.png"
    notes[0]["fields"]["Extra"]["value"] += f' <img src="{filename}">'
    output = tmp_path / "notes.jsonl.gz"

    FullSnapshotExporter(
        anki=FakeAnki(notes),
        producer_version="test",
    ).export(output)

    with gzip.open(output, "rt", encoding="utf-8") as stream:
        rows = [json.loads(line) for line in stream]
    assert filename in rows[0]["media"]


def test_id_set_hash_changes_for_add_or_delete(tmp_path: Path) -> None:
    notes = _fixture_notes()
    first = FullSnapshotExporter(
        anki=FakeAnki(notes),
        chunk_size=2,
        producer_version="test",
    ).export(tmp_path / "all.jsonl")
    second = FullSnapshotExporter(
        anki=FakeAnki(notes[:-1]),
        chunk_size=2,
        producer_version="test",
    ).export(tmp_path / "fewer.jsonl")

    assert first.id_set_sha256 != second.id_set_sha256


def test_hub_stages_a_valid_local_snapshot_atomically(tmp_path: Path) -> None:
    source = tmp_path / "source.jsonl.gz"
    manifest = FullSnapshotExporter(
        anki=FakeAnki(_fixture_notes()),
        chunk_size=2,
        producer_version="test",
    ).export(source)

    staged = stage_full_snapshot(
        manifest,
        source,
        tmp_path / "job",
        max_decompressed_bytes=100_000,
        max_row_bytes=20_000,
    )

    assert staged.manifest_path.exists()
    assert staged.notes_path.exists()
    assert staged.note_count == 3
    assert not any(
        path.name.startswith(".staging-")
        for path in staged.root.iterdir()
    )


def test_local_snapshot_hash_reader_rejects_duplicate_note_ids(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.jsonl"
    FullSnapshotExporter(
        anki=FakeAnki(_fixture_notes()),
        chunk_size=2,
        producer_version="test",
    ).export(source)
    rows = source.read_text(encoding="utf-8").splitlines()
    source.write_text("\n".join([rows[0], rows[0]]) + "\n", encoding="utf-8")

    with pytest.raises(SnapshotValidationError, match="unique and sorted"):
        snapshot_note_hashes(source)
