from collections.abc import Callable, Collection, Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol, cast

from oms_hub.anki.ankiconnect import AnkiConnectError
from oms_hub.anki.runtime import AnkiPreflight


class AnkiLearningGateway(Protocol):
    async def version(self) -> int: ...

    async def get_active_profile(self) -> str: ...

    async def find_notes(self, query: str) -> list[int]: ...

    async def notes_info(
        self,
        note_ids: Sequence[int],
    ) -> list[dict[str, Any]]: ...

    async def cards_info(
        self,
        card_ids: Sequence[int],
    ) -> list[dict[str, Any]]: ...


class AnkiPreflightSource(Protocol):
    async def preflight(self) -> AnkiPreflight: ...


@dataclass(frozen=True, slots=True)
class AnkiSyncHealth:
    reachable: bool
    ankiconnect_version: int | None
    active_profile: str | None
    collection_accessible: bool
    sync_available: bool
    blocking_reason: str | None

    @classmethod
    def from_preflight(cls, preflight: AnkiPreflight) -> "AnkiSyncHealth":
        return cls(
            reachable=preflight.reachable,
            ankiconnect_version=preflight.ankiconnect_version,
            active_profile=preflight.active_profile,
            collection_accessible=preflight.collection_accessible,
            sync_available=preflight.sync_available,
            blocking_reason=preflight.blocking_reason,
        )


@dataclass(frozen=True, slots=True)
class AnkiNoteLearningState:
    note_id: int
    card_ids: tuple[int, ...]
    deck_name: str
    selected_tags: tuple[str, ...]
    due: bool | None
    overdue: bool | None
    lapse_count: int
    interval: int | None
    retrievability: float | None
    suspended: bool
    buried: bool
    last_reviewed_at: datetime | None
    snapshot_at: datetime

    @property
    def tags(self) -> tuple[str, ...]:
        return self.selected_tags

    @property
    def is_due(self) -> bool | None:
        return self.due

    @property
    def is_overdue(self) -> bool | None:
        return self.overdue

    @property
    def last_reviewed_time(self) -> datetime | None:
        return self.last_reviewed_at

    @property
    def snapshot_time(self) -> datetime:
        return self.snapshot_at


@dataclass(frozen=True, slots=True)
class AnkiLearningSnapshot:
    notes: tuple[AnkiNoteLearningState, ...]
    health: AnkiSyncHealth
    snapshot_at: datetime

    @property
    def note_states(self) -> tuple[AnkiNoteLearningState, ...]:
        return self.notes

    @property
    def snapshot_time(self) -> datetime:
        return self.snapshot_at


class AnkiLearningReader:
    """Read and minimize Anki scheduling metadata for hosted learning use."""

    def __init__(
        self,
        gateway: AnkiLearningGateway,
        *,
        runtime: AnkiPreflightSource | None = None,
        selected_tags: Collection[str] = (),
        metadata_batch_size: int = 500,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        if metadata_batch_size < 1:
            raise ValueError("Anki metadata batch size must be positive")
        self._gateway = gateway
        self._runtime = runtime
        self._selected_tags = tuple(
            sorted(
                {tag.strip().casefold() for tag in selected_tags if tag.strip()},
            )
        )
        self._metadata_batch_size = metadata_batch_size
        self._now = now

    async def health(self) -> AnkiSyncHealth:
        if self._runtime is not None:
            return AnkiSyncHealth.from_preflight(
                await self._runtime.preflight()
            )
        try:
            version = await self._gateway.version()
        except AnkiConnectError as exc:
            return AnkiSyncHealth(
                reachable=False,
                ankiconnect_version=None,
                active_profile=None,
                collection_accessible=False,
                sync_available=False,
                blocking_reason=str(exc),
            )
        try:
            profile = await self._gateway.get_active_profile()
            await self._gateway.find_notes("")
        except AnkiConnectError as exc:
            return AnkiSyncHealth(
                reachable=True,
                ankiconnect_version=version,
                active_profile=None,
                collection_accessible=False,
                sync_available=version >= 6,
                blocking_reason=str(exc),
            )
        return AnkiSyncHealth(
            reachable=True,
            ankiconnect_version=version,
            active_profile=profile,
            collection_accessible=True,
            sync_available=version >= 6,
            blocking_reason=None,
        )

    async def sync_health(self) -> AnkiSyncHealth:
        return await self.health()

    async def snapshot(self, query: str = "") -> AnkiLearningSnapshot:
        snapshot_at = self._now()
        health = await self.health()
        if not health.reachable or not health.collection_accessible:
            return AnkiLearningSnapshot((), health, snapshot_at)

        note_ids = await self._gateway.find_notes(query)
        _require_unique_positive_ids(note_ids, "note")
        raw_notes: list[dict[str, Any]] = []
        for batch in _batches(note_ids, self._metadata_batch_size):
            raw_notes.extend(await self._gateway.notes_info(batch))
        if len(raw_notes) != len(note_ids):
            raise ValueError("Anki note metadata count does not reconcile")

        note_cards: dict[int, tuple[int, ...]] = {}
        note_tags: dict[int, tuple[str, ...]] = {}
        all_card_ids: list[int] = []
        for raw_note in raw_notes:
            note_id = _positive_int(raw_note.get("noteId"), "note ID")
            if note_id in note_cards:
                raise ValueError("Anki returned duplicate note metadata")
            raw_cards = raw_note.get("cards", raw_note.get("cardIds", ()))
            card_ids = tuple(
                sorted(
                    _positive_int(value, "card ID")
                    for value in _sequence(raw_cards, "cards")
                )
            )
            if len(card_ids) != len(set(card_ids)):
                raise ValueError("Anki returned duplicate card IDs for a note")
            raw_tags = _string_values(raw_note.get("tags", ()), "tags")
            note_cards[note_id] = card_ids
            note_tags[note_id] = self._select_tags(raw_tags)
            all_card_ids.extend(card_ids)
        if set(note_cards) != set(note_ids):
            raise ValueError("Anki note metadata IDs do not reconcile")

        card_records: dict[int, dict[str, Any]] = {}
        for batch in _batches(tuple(dict.fromkeys(all_card_ids)), self._metadata_batch_size):
            records = await self._gateway.cards_info(batch)
            if len(records) != len(batch):
                raise ValueError("Anki card metadata count does not reconcile")
            for record in records:
                card_id = _positive_int(record.get("cardId"), "card ID")
                if card_id in card_records:
                    raise ValueError("Anki returned duplicate card metadata")
                note_id = _positive_int(
                    record.get("note", record.get("noteId")),
                    "card note ID",
                )
                if card_id not in note_cards.get(note_id, ()):
                    raise ValueError("Anki card metadata is inconsistent")
                card_records[card_id] = record
        if set(card_records) != set(all_card_ids):
            raise ValueError("Anki card metadata IDs do not reconcile")

        states = tuple(
            _note_state(
                note_id,
                note_cards[note_id],
                note_tags[note_id],
                card_records,
                snapshot_at,
            )
            for note_id in sorted(note_cards)
        )
        return AnkiLearningSnapshot(states, health, snapshot_at)

    async def read_snapshot(self, query: str = "") -> AnkiLearningSnapshot:
        return await self.snapshot(query)

    def _select_tags(self, tags: Sequence[str]) -> tuple[str, ...]:
        selected = {
            tag
            for tag in tags
            if any(
                tag.casefold() == allowed
                or tag.casefold().startswith(f"{allowed}::")
                for allowed in self._selected_tags
            )
        }
        return tuple(sorted(selected, key=str.casefold))


def _note_state(
    note_id: int,
    card_ids: tuple[int, ...],
    selected_tags: tuple[str, ...],
    cards: dict[int, dict[str, Any]],
    snapshot_at: datetime,
) -> AnkiNoteLearningState:
    note_cards = [cards[card_id] for card_id in card_ids if card_id in cards]
    due = _aggregate_status(_explicit_status(card, "due") for card in note_cards)
    overdue = _aggregate_status(
        _explicit_status(card, "overdue") for card in note_cards
    )
    lapses = [_nonnegative_int(card.get("lapses", 0), "lapses") for card in note_cards]
    intervals = [
        _nonnegative_int(card["interval"], "interval")
        for card in note_cards
        if card.get("interval") is not None
    ]
    retrievabilities = [
        retrievability
        for card in note_cards
        if (retrievability := _retrievability(card.get("retrievability")))
        is not None
    ]
    decks = [
        deck_value.strip()
        for card in note_cards
        if isinstance(deck_value := card.get("deckName"), str)
        and deck_value.strip()
    ]
    reviewed = [
        reviewed_at
        for card in note_cards
        if (reviewed_at := _reviewed_at(card)) is not None
    ]
    return AnkiNoteLearningState(
        note_id=note_id,
        card_ids=card_ids,
        deck_name=decks[0] if decks else "",
        selected_tags=selected_tags,
        due=due,
        overdue=overdue,
        lapse_count=max(lapses, default=0),
        interval=max(intervals, default=None),
        retrievability=(min(retrievabilities) if retrievabilities else None),
        suspended=any(_queue(card) == -1 for card in note_cards),
        buried=any(_queue(card) in {-2, -3} for card in note_cards),
        last_reviewed_at=max(reviewed, default=None),
        snapshot_at=snapshot_at,
    )


def _explicit_status(card: dict[str, Any], kind: str) -> bool | None:
    keys = {
        "due": ("isDue", "dueStatus", "due_status", "due"),
        "overdue": (
            "isOverdue",
            "overdueStatus",
            "overdue_status",
            "overdue",
        ),
    }[kind]
    for key in keys:
        value = card.get(key)
        if value is None:
            continue
        if isinstance(value, bool):
            return value
        # cardsInfo.due is normally a numeric scheduler position, not a status.
        if key == "due":
            continue
        raise ValueError(f"Anki returned invalid {kind} status")
    return None


def _aggregate_status(statuses: Iterable[bool | None]) -> bool | None:
    values = tuple(statuses)
    if any(value is True for value in values):
        return True
    if values and all(value is False for value in values):
        return False
    return None


def _queue(card: dict[str, Any]) -> int:
    value = card.get("queue", 0)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("Anki returned invalid queue value")
    return cast(int, value)


def _reviewed_at(card: dict[str, Any]) -> datetime | None:
    for key in (
        "lastReviewed",
        "last_reviewed_at",
        "last_reviewed",
        "reviewedAt",
        "reviewed_at",
    ):
        if key in card:
            return _timestamp(card[key])
    return None


def _timestamp(value: object) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo is not None else value.replace(tzinfo=UTC)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        raise ValueError("Anki returned invalid review time")
    seconds = float(value) / 1_000 if value > 10_000_000_000 else float(value)
    return datetime.fromtimestamp(seconds, tz=UTC)


def _retrievability(value: object) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("Anki returned invalid retrievability")
    result = float(value)
    if not 0 <= result <= 1:
        raise ValueError("Anki returned invalid retrievability")
    return result


def _batches(values: Sequence[int], size: int) -> list[Sequence[int]]:
    return [values[offset : offset + size] for offset in range(0, len(values), size)]


def _sequence(value: object, description: str) -> Sequence[object]:
    if not isinstance(value, (list, tuple)):
        raise TypeError(f"Anki returned invalid {description}")
    return value


def _string_values(value: object, description: str) -> tuple[str, ...]:
    values = _sequence(value, description)
    if not all(isinstance(item, str) for item in values):
        raise TypeError(f"Anki returned invalid {description}")
    return tuple(
        item.strip()
        for item in values
        if isinstance(item, str) and item.strip()
    )


def _require_unique_positive_ids(values: Sequence[int], description: str) -> None:
    if any(
        isinstance(value, bool) or not isinstance(value, int) or value <= 0
        for value in values
    ):
        raise ValueError(f"Anki returned invalid {description} ID")
    if len(values) != len(set(values)):
        raise ValueError(f"Anki returned duplicate {description} IDs")


def _positive_int(value: object, description: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"Anki returned invalid {description}")
    return value


def _nonnegative_int(value: object, description: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"Anki returned invalid {description}")
    return value
