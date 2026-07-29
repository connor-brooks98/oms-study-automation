import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from oms_hub.anki.ledger import AnkiLedger
from oms_hub.anki.service import LocalAnkiService
from oms_hub.anki.snapshot_export import (
    FullSnapshotExporter,
    SnapshotVersions,
)
from oms_hub.app import create_app
from oms_hub.cli import (
    anki_doctor,
    anki_snapshot,
    build_parser,
)
from oms_hub.config import Settings

NOW = datetime(2026, 7, 29, 12, tzinfo=UTC)


class FakeRuntime:
    def __init__(self) -> None:
        self.ensure_calls = 0

    def ensure_available(self) -> int:
        self.ensure_calls += 1
        return 6

    def doctor(self) -> object:
        raise AssertionError("doctor is not used by this test")


class FakeAnki:
    def __init__(self, notes: list[dict[str, Any]]) -> None:
        self.notes = {int(note["noteId"]): note for note in notes}

    def version(self) -> int:
        return 6

    def find_notes(self, query: str) -> list[int]:
        assert query == 'deck:"Anking Step Deck"'
        return list(reversed(self.notes))

    def notes_info(self, note_ids: list[int]) -> list[dict[str, Any]]:
        return [self.notes[note_id] for note_id in note_ids]


def _notes() -> list[dict[str, Any]]:
    fixture = Path(__file__).parent / "fixtures" / "anking_notes.json"
    return json.loads(fixture.read_text(encoding="utf-8"))


def test_local_service_exports_to_nuc_data_directory_and_commits_ledger(
    tmp_path: Path,
) -> None:
    runtime = FakeRuntime()
    ledger = AnkiLedger(tmp_path / "ledger.sqlite3")
    service = LocalAnkiService(
        runtime=runtime,
        exporter=FullSnapshotExporter(
            anki=FakeAnki(_notes()),
            chunk_size=2,
            producer_version="test",
        ),
        ledger=ledger,
        data_dir=tmp_path,
        versions=SnapshotVersions(
            export_version="1",
            normalizer_version="1",
            embedding_model="test-embedding",
        ),
    )

    manifest = service.export_full(exported_at=NOW)

    assert manifest.note_count == 3
    assert runtime.ensure_calls == 1
    assert set(ledger.note_hashes()) == {101, 102, 103}
    assert ledger.snapshot_state() == {
        "embedding_model": "test-embedding",
        "export_version": "1",
        "exported_at": NOW.isoformat(),
        "normalizer_version": "1",
        "note_count": 3,
    }
    assert service.snapshot_path == tmp_path / "snapshots" / "current.jsonl.gz"
    assert service.snapshot_path.exists()
    assert not list((tmp_path / "snapshots").glob(".current-*.jsonl.gz"))


def test_cli_exposes_nuc_local_anki_commands() -> None:
    parser = build_parser()

    assert parser.parse_args(["anki-doctor"]).handler is anki_doctor
    snapshot = parser.parse_args(["anki-snapshot", "--full"])
    assert snapshot.handler is anki_snapshot
    assert snapshot.full is True


def test_application_composes_local_anki_without_starting_it(tmp_path: Path) -> None:
    settings = Settings(
        _env_file=None,
        data_dir=tmp_path,
        database_url=f"sqlite:///{tmp_path / 'hub.db'}",
        allow_local_access=True,
        anki_enabled=True,
        anki_executable_path=tmp_path / "Anki.exe",
    )

    app = create_app(settings)

    assert app.state.anki_service is not None
    assert app.state.anki_executor is not None
    assert TestClient(app).get("/health").status_code == 200
