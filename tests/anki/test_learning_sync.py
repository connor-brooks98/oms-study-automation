import asyncio
from datetime import UTC, datetime
from typing import Any

import pytest

from oms_hub.anki.learning_contracts import (
    AnkiLearningSnapshot,
    AnkiNoteLearningState,
    AnkiSyncHealth,
)
from oms_hub.anki.learning_repository import AnkiLearningRepository
from oms_hub.anki.learning_sync import AnkiLearningSync

SNAPSHOT_TIME = datetime(2026, 8, 23, 12, 0, tzinfo=UTC)


def _snapshot() -> AnkiLearningSnapshot:
    return AnkiLearningSnapshot(
        notes=(
            AnkiNoteLearningState(
                note_id=42,
                card_ids=(4201,),
                deck_name="AnKing Step Deck::Heme",
                selected_tags=("lecture::heme",),
                due=True,
                overdue=False,
                lapse_count=2,
                interval=7,
                retrievability=None,
                suspended=False,
                buried=False,
                last_reviewed_at=None,
                snapshot_at=SNAPSHOT_TIME,
            ),
        ),
        health=AnkiSyncHealth(
            reachable=True,
            ankiconnect_version=6,
            active_profile="Disposable Test",
            collection_accessible=True,
            sync_available=True,
            blocking_reason=None,
        ),
        snapshot_at=SNAPSHOT_TIME,
    )


class FakeReader:
    def __init__(self, snapshot: object, calls: list[str]) -> None:
        self.snapshot_value = snapshot
        self.calls = calls

    async def read_snapshot(self, query: str = "") -> object:
        assert query == ""
        self.calls.append("read")
        return self.snapshot_value


class FakeUploader:
    def __init__(self, calls: list[str], error: Exception | None = None) -> None:
        self.calls = calls
        self.error = error
        self.payload: object | None = None

    async def upload(self, payload: object) -> None:
        self.calls.append("upload")
        if self.error is not None:
            raise self.error
        self.payload = payload


def test_build_and_upload_uses_only_injected_offline_fakes_and_records_after_upload() -> None:
    calls: list[str] = []
    reader = FakeReader(_snapshot(), calls)
    uploader = FakeUploader(calls)
    repository = AnkiLearningRepository(now=lambda: SNAPSHOT_TIME)

    run = asyncio.run(
        AnkiLearningSync(
            reader=reader,
            uploader=uploader,
            repository=repository,
        ).build_and_upload()
    )

    assert calls == ["read", "upload"]
    assert isinstance(uploader.payload, AnkiLearningSnapshot)
    assert "card HTML" not in repr(uploader.payload)
    assert "media" not in repr(uploader.payload)
    assert repository.latest_sync_run() == run


def test_reader_failure_does_not_upload_or_record() -> None:
    calls: list[str] = []
    reader = FakeReader(RuntimeError("offline reader failed"), calls)

    async def failing_read(query: str = "") -> object:
        calls.append("read")
        raise RuntimeError("offline reader failed")

    reader.read_snapshot = failing_read  # type: ignore[method-assign]
    uploader = FakeUploader(calls)
    repository = AnkiLearningRepository()

    with pytest.raises(RuntimeError, match="offline reader failed"):
        asyncio.run(
            AnkiLearningSync(
                reader=reader,
                uploader=uploader,
                repository=repository,
            ).build_and_upload()
        )

    assert calls == ["read"]
    assert repository.sync_history() == ()


def test_uploader_failure_does_not_record() -> None:
    calls: list[str] = []
    uploader = FakeUploader(calls, error=RuntimeError("offline uploader failed"))

    with pytest.raises(RuntimeError, match="offline uploader failed"):
        asyncio.run(
            AnkiLearningSync(
                reader=FakeReader(_snapshot(), calls),
                uploader=uploader,
                repository=AnkiLearningRepository(),
            ).build_and_upload()
        )

    assert calls == ["read", "upload"]


def test_build_rejects_unminimized_reader_payload_before_upload() -> None:
    calls: list[str] = []
    payload: dict[str, Any] = {
        "notes": [],
        "health": {
            "reachable": True,
            "ankiconnect_version": 6,
            "active_profile": "Disposable Test",
            "collection_accessible": True,
            "sync_available": True,
            "blocking_reason": None,
        },
        "snapshot_at": SNAPSHOT_TIME,
        "private_collection": "must be rejected",
    }
    uploader = FakeUploader(calls)

    with pytest.raises(ValueError, match="unexpected snapshot fields"):
        asyncio.run(
            AnkiLearningSync(
                reader=FakeReader(payload, calls),
                uploader=uploader,
                repository=AnkiLearningRepository(),
            ).build_and_upload()
        )

    assert calls == ["read"]
    assert uploader.payload is None


def test_build_and_upload_does_not_expose_mutation_surface() -> None:
    public = {name for name in dir(AnkiLearningSync) if not name.startswith("_")}
    assert not {
        "add_note",
        "add_tags",
        "update_note",
        "delete_notes",
        "suspend",
        "sync",
        "ensure_running",
        "create_filtered_deck",
    } & public
