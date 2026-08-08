import gzip
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from oms_anki_agent.ledger import AgentLedger
from oms_anki_agent.service import LedgerSnapshotFactory
from oms_anki_agent.snapshot import FullSnapshotExporter
from oms_hub.anki.contracts import (
    AgentCommand,
    SnapshotDelta,
    SnapshotManifest,
    canonical_payload_sha256,
)
from oms_hub.anki.domain import AgentCommandType
from oms_hub.anki.snapshot import SnapshotValidationError, stage_full_snapshot


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
    fixture = Path(__file__).parents[1] / "anki" / "fixtures" / "anking_notes.json"
    return json.loads(fixture.read_text(encoding="utf-8"))


def test_full_export_streams_sorted_notes_in_bounded_chunks(tmp_path: Path) -> None:
    anki = FakeAnki(_fixture_notes())
    output = tmp_path / "notes.jsonl.gz"
    exporter = FullSnapshotExporter(anki=anki, chunk_size=2, agent_version="test")

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
    assert manifest.ankiconnect_version == 6
    assert manifest.payload_sha256 != "0" * 64


def test_id_set_hash_changes_for_add_or_delete(tmp_path: Path) -> None:
    notes = _fixture_notes()
    first = FullSnapshotExporter(
        anki=FakeAnki(notes),
        chunk_size=2,
        agent_version="test",
    ).export(tmp_path / "all.jsonl")
    second = FullSnapshotExporter(
        anki=FakeAnki(notes[:-1]),
        chunk_size=2,
        agent_version="test",
    ).export(tmp_path / "fewer.jsonl")

    assert first.id_set_sha256 != second.id_set_sha256


def test_hub_stages_a_valid_snapshot_atomically(tmp_path: Path) -> None:
    source = tmp_path / "source.jsonl.gz"
    manifest = FullSnapshotExporter(
        anki=FakeAnki(_fixture_notes()),
        chunk_size=2,
        agent_version="test",
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
    assert not any(path.name.startswith(".staging-") for path in staged.root.iterdir())


@pytest.mark.parametrize("failure", ["duplicate", "count", "manifest", "oversized"])
def test_hub_rejects_invalid_snapshot_before_replacing_stage(
    tmp_path: Path,
    failure: str,
) -> None:
    source = tmp_path / "source.jsonl"
    manifest = FullSnapshotExporter(
        anki=FakeAnki(_fixture_notes()),
        chunk_size=2,
        agent_version="test",
    ).export(source)
    rows = source.read_text(encoding="utf-8").splitlines()
    if failure == "duplicate":
        rows[-1] = rows[0]
    elif failure == "count":
        rows.pop()
    elif failure == "manifest":
        manifest = SnapshotManifest.model_copy(
            manifest,
            update={"payload_sha256": "f" * 64},
        )
    else:
        rows[0] += " " * 1_000
    source.write_text("\n".join(rows) + "\n", encoding="utf-8")

    with pytest.raises(SnapshotValidationError):
        stage_full_snapshot(
            manifest,
            source,
            tmp_path / "job",
            max_decompressed_bytes=100_000,
            max_row_bytes=500 if failure == "oversized" else 20_000,
        )
    assert not (tmp_path / "job" / "current").exists()


def test_service_prepares_delta_as_streamed_file_and_defers_ledger_commit(
    tmp_path: Path,
) -> None:
    ledger = AgentLedger(tmp_path / "ledger.sqlite3")
    ledger.replace_note_hashes({101: "0" * 64, 999: "f" * 64})
    factory = LedgerSnapshotFactory(
        exporter=FullSnapshotExporter(
            anki=FakeAnki(_fixture_notes()),
            chunk_size=2,
            agent_version="test",
        ),
        ledger=ledger,
        work_root=tmp_path / "work",
    )
    command = AgentCommand(
        command_id="b2edb9da-4421-4d27-bc6b-7797ed310355",
        command_type=AgentCommandType.DELTA_SNAPSHOT,
        payload={},
        payload_sha256="a" * 64,
        created_at="2026-07-27T12:00:00Z",
    )

    prepared = factory.create(command)
    payload = json.loads(prepared.payload_path.read_text(encoding="utf-8"))
    contract = SnapshotDelta.model_validate(payload)

    assert [note["note_id"] for note in payload["upserts"]] == [101, 102, 103]
    assert payload["deleted_note_ids"] == [999]
    assert canonical_payload_sha256(contract) == prepared.payload_sha256
    assert ledger.note_hashes() == {101: "0" * 64, 999: "f" * 64}
    factory.commit(prepared)
    assert set(ledger.note_hashes()) == {101, 102, 103}
