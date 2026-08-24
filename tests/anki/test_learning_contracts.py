import asyncio
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

from oms_hub.anki.learning_contracts import AnkiLearningReader, AnkiSyncHealth
from oms_hub.anki.runtime import AnkiPreflight

SNAPSHOT_TIME = datetime(2026, 8, 23, 12, 0, tzinfo=UTC)


def test_learning_reader_has_no_write_surface() -> None:
    public = {name for name in dir(AnkiLearningReader) if not name.startswith("_")}
    assert not {
        "add_note",
        "add_tags",
        "update_note",
        "delete_notes",
        "suspend",
        "create_filtered_deck",
    } & public


class FakeGateway:
    async def version(self) -> int:
        return 6

    async def get_active_profile(self) -> str:
        return "Disposable Test"

    async def find_notes(self, query: str) -> list[int]:
        assert query == 'deck:"AnKing Step Deck"'
        return [42]

    async def notes_info(self, note_ids: Sequence[int]) -> list[dict[str, Any]]:
        assert tuple(note_ids) == (42,)
        return [
            {
                "noteId": 42,
                "tags": ["lecture::heme", "private::unrelated"],
                "cards": [4201, 4202],
                "fields": {
                    "Text": {"value": "full card HTML must not escape"},
                    "Extra": {"value": "also private"},
                },
            }
        ]

    async def cards_info(self, card_ids: Sequence[int]) -> list[dict[str, Any]]:
        assert tuple(card_ids) == (4201, 4202)
        return [
            {
                "cardId": 4201,
                "note": 42,
                "deckName": "AnKing Step Deck::Heme",
                "queue": 2,
                "due": 100,
                "lapses": 3,
                "interval": 7,
                "mod": 1_756_560_000,
            },
            {
                "cardId": 4202,
                "note": 42,
                "deckName": "AnKing Step Deck::Heme",
                "queue": -2,
                "due": 101,
                "lapses": 1,
                "interval": 14,
                "mod": 1_756_500_000,
            },
        ]


class FakeRuntime:
    async def preflight(self) -> AnkiPreflight:
        return AnkiPreflight(
            reachable=True,
            ankiconnect_version=6,
            active_profile="Disposable Test",
            collection_accessible=True,
            sync_available=True,
            blocking_reason=None,
        )


def test_reader_maps_minimized_note_learning_state() -> None:
    async def scenario() -> None:
        reader = AnkiLearningReader(
            FakeGateway(),
            runtime=FakeRuntime(),
            selected_tags=("lecture::heme",),
            now=lambda: SNAPSHOT_TIME,
        )

        snapshot = await reader.snapshot('deck:"AnKing Step Deck"')

        assert snapshot.snapshot_at == SNAPSHOT_TIME
        assert snapshot.health == AnkiSyncHealth(
            reachable=True,
            ankiconnect_version=6,
            active_profile="Disposable Test",
            collection_accessible=True,
            sync_available=True,
            blocking_reason=None,
        )
        assert len(snapshot.notes) == 1
        note = snapshot.notes[0]
        assert note.note_id == 42
        assert note.card_ids == (4201, 4202)
        assert note.deck_name == "AnKing Step Deck::Heme"
        assert note.selected_tags == ("lecture::heme",)
        assert note.due is None
        assert note.overdue is None
        assert note.lapse_count == 3
        assert note.interval == 14
        assert note.suspended is False
        assert note.buried is True
        assert note.last_reviewed_at is None
        assert note.snapshot_at == SNAPSHOT_TIME
        assert "full card HTML" not in repr(snapshot)
        assert "private::unrelated" not in repr(snapshot)

    asyncio.run(scenario())


def test_explicit_card_status_is_preserved_without_inference() -> None:
    class ExplicitStatusGateway(FakeGateway):
        async def cards_info(self, card_ids: Sequence[int]) -> list[dict[str, Any]]:
            records = await super().cards_info(card_ids)
            records[0]["isDue"] = True
            records[0]["isOverdue"] = True
            records[0]["lastReviewed"] = 1_756_560_000
            return records

    async def scenario() -> None:
        snapshot = await AnkiLearningReader(
            ExplicitStatusGateway(),
            runtime=FakeRuntime(),
            selected_tags=("lecture::heme",),
            now=lambda: SNAPSHOT_TIME,
        ).snapshot('deck:"AnKing Step Deck"')

        note = snapshot.notes[0]
        assert note.due is True
        assert note.overdue is True
        assert note.last_reviewed_at == datetime.fromtimestamp(
            1_756_560_000,
            tz=UTC,
        )

    asyncio.run(scenario())


def test_card_modification_time_is_not_review_time() -> None:
    async def scenario() -> None:
        snapshot = await AnkiLearningReader(
            FakeGateway(),
            runtime=FakeRuntime(),
            selected_tags=("lecture::heme",),
            now=lambda: SNAPSHOT_TIME,
        ).snapshot('deck:"AnKing Step Deck"')

        assert snapshot.notes[0].last_reviewed_at is None

    asyncio.run(scenario())


def test_reader_maps_unavailable_preflight_without_reading_collection() -> None:
    class OfflineRuntime:
        async def preflight(self) -> AnkiPreflight:
            return AnkiPreflight(
                reachable=False,
                ankiconnect_version=None,
                active_profile=None,
                collection_accessible=False,
                sync_available=False,
                blocking_reason="AnkiConnect is unavailable",
            )

    async def scenario() -> None:
        snapshot = await AnkiLearningReader(
            FakeGateway(),
            runtime=OfflineRuntime(),
            now=lambda: SNAPSHOT_TIME,
        ).snapshot()

        assert snapshot.notes == ()
        assert snapshot.health.reachable is False
        assert snapshot.health.blocking_reason == "AnkiConnect is unavailable"

    asyncio.run(scenario())
