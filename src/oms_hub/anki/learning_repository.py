"""Isolated, read-only Anki learning snapshot persistence contracts."""

import hashlib
import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from math import isfinite
from typing import Any
from uuid import uuid4

from oms_hub.anki.learning_contracts import (
    AnkiLearningSnapshot,
    AnkiNoteLearningState,
    AnkiSyncHealth,
)


class AnkiStaleness(StrEnum):
    FRESH = "fresh"
    STALE = "stale"
    VERY_STALE = "very_stale"
    NEVER_SYNCED = "never_synced"


@dataclass(frozen=True, slots=True)
class AnkiStalenessThresholds:
    """Locally configurable, nonblocking freshness boundaries."""

    fresh_after: timedelta = timedelta(hours=24)
    very_stale_after: timedelta = timedelta(days=7)

    def __post_init__(self) -> None:
        if self.fresh_after <= timedelta(0):
            raise ValueError("fresh_after must be positive")
        if self.very_stale_after < self.fresh_after:
            raise ValueError("very_stale_after must not precede fresh_after")


StalenessThresholds = AnkiStalenessThresholds


@dataclass(frozen=True, slots=True)
class AnkiSyncHealthView(AnkiSyncHealth):
    """Task 7.1 health plus the current nonblocking freshness label."""

    last_synced_at: datetime | None = None
    staleness: AnkiStaleness = AnkiStaleness.NEVER_SYNCED

    @property
    def freshness(self) -> AnkiStaleness:
        return self.staleness


@dataclass(frozen=True, slots=True)
class AnkiSyncRun:
    """An append-only receipt for one accepted snapshot attempt."""

    sync_id: str
    content_hash: str
    snapshot_at: datetime
    recorded_at: datetime
    note_count: int
    changed: bool
    health: AnkiSyncHealth
    status: str

    @property
    def no_change(self) -> bool:
        return not self.changed

    @property
    def is_no_change(self) -> bool:
        return self.no_change

    @property
    def content_sha256(self) -> str:
        return self.content_hash

    @property
    def snapshot_id(self) -> str:
        return self.sync_id


@dataclass(frozen=True, slots=True)
class AnkiNoteStateRecord:
    """Auditable note-state row, retained independently of the current pointer."""

    sync_id: str
    content_hash: str
    state: AnkiNoteLearningState

    @property
    def note_id(self) -> int:
        return self.state.note_id


class AnkiLearningRepository:
    """Process-local repository pending Sol-0 database composition.

    The content hash excludes snapshot timestamps so a retry of the same
    minimized snapshot is idempotent while each receipt remains auditable.
    """

    def __init__(
        self,
        *,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
        thresholds: AnkiStalenessThresholds | None = None,
        fresh_after: timedelta | None = None,
        very_stale_after: timedelta | None = None,
    ) -> None:
        if thresholds is not None and (
            fresh_after is not None or very_stale_after is not None
        ):
            raise ValueError("provide thresholds or threshold values, not both")
        self._now = now
        self._thresholds = thresholds or AnkiStalenessThresholds(
            fresh_after=fresh_after or timedelta(hours=24),
            very_stale_after=very_stale_after or timedelta(days=7),
        )
        self._sync_runs: list[AnkiSyncRun] = []
        self._current_content_hash: str | None = None
        self._current_notes: dict[int, AnkiNoteLearningState] = {}
        self._note_state_rows: list[AnkiNoteStateRecord] = []
        self._states_by_hash: dict[str, tuple[AnkiNoteLearningState, ...]] = {}

    @property
    def thresholds(self) -> AnkiStalenessThresholds:
        return self._thresholds

    def record_sync(self, snapshot: AnkiLearningSnapshot | Mapping[str, Any]) -> AnkiSyncRun:
        normalized = normalize_snapshot(snapshot)
        recorded_at = _aware_datetime(self._now(), "recorded_at")
        content_hash = snapshot_content_hash(normalized)
        previous_hash = self._current_content_hash
        changed = previous_hash != content_hash
        sync_id = f"anki_sync_{uuid4()}"
        run = AnkiSyncRun(
            sync_id=sync_id,
            content_hash=content_hash,
            snapshot_at=normalized.snapshot_at,
            recorded_at=recorded_at,
            note_count=len(normalized.notes),
            changed=changed,
            health=normalized.health,
            status="recorded" if changed else "no_change",
        )
        self._sync_runs.append(run)

        if changed:
            states = self._states_by_hash.get(content_hash)
            if states is None:
                states = normalized.notes
                self._states_by_hash[content_hash] = states
                self._note_state_rows.extend(
                    AnkiNoteStateRecord(sync_id, content_hash, state)
                    for state in states
                )
            self._current_notes = {state.note_id: state for state in states}
            self._current_content_hash = content_hash
        return run

    def latest_note_state(self, note_id: int) -> AnkiNoteLearningState | None:
        if isinstance(note_id, bool) or not isinstance(note_id, int) or note_id <= 0:
            raise ValueError("note_id must be a positive integer")
        return self._current_notes.get(note_id)

    def latest_note_states(self) -> tuple[AnkiNoteLearningState, ...]:
        return tuple(self._current_notes[note_id] for note_id in sorted(self._current_notes))

    def latest_sync_health(self) -> AnkiSyncHealthView:
        latest = self.latest_sync_run()
        if latest is None:
            return AnkiSyncHealthView(
                reachable=False,
                ankiconnect_version=None,
                active_profile=None,
                collection_accessible=False,
                sync_available=False,
                blocking_reason=None,
            )
        return AnkiSyncHealthView(
            reachable=latest.health.reachable,
            ankiconnect_version=latest.health.ankiconnect_version,
            active_profile=latest.health.active_profile,
            collection_accessible=latest.health.collection_accessible,
            sync_available=latest.health.sync_available,
            blocking_reason=latest.health.blocking_reason,
            last_synced_at=latest.snapshot_at,
            staleness=self._staleness(latest.snapshot_at),
        )

    def latest_staleness(self) -> AnkiStaleness:
        return self.latest_sync_health().staleness

    def sync_history(self) -> tuple[AnkiSyncRun, ...]:
        return tuple(self._sync_runs)

    def list_sync_runs(self) -> tuple[AnkiSyncRun, ...]:
        return self.sync_history()

    def latest_sync_run(self) -> AnkiSyncRun | None:
        return self._sync_runs[-1] if self._sync_runs else None

    def note_state_history(
        self,
        note_id: int | None = None,
    ) -> tuple[AnkiNoteStateRecord, ...]:
        if note_id is not None and (
            isinstance(note_id, bool) or not isinstance(note_id, int) or note_id <= 0
        ):
            raise ValueError("note_id must be a positive integer")
        rows = self._note_state_rows
        if note_id is not None:
            rows = [row for row in rows if row.note_id == note_id]
        return tuple(rows)

    def list_note_state_rows(self) -> tuple[AnkiNoteStateRecord, ...]:
        return self.note_state_history()

    def _staleness(self, snapshot_at: datetime) -> AnkiStaleness:
        now = _aware_datetime(self._now(), "now")
        age = now - snapshot_at
        if age < self._thresholds.fresh_after:
            return AnkiStaleness.FRESH
        if age <= self._thresholds.very_stale_after:
            return AnkiStaleness.STALE
        return AnkiStaleness.VERY_STALE


_SNAPSHOT_FIELDS = frozenset({"notes", "health", "snapshot_at"})
_HEALTH_FIELDS = frozenset(
    {
        "reachable",
        "ankiconnect_version",
        "active_profile",
        "collection_accessible",
        "sync_available",
        "blocking_reason",
    }
)
_NOTE_FIELDS = frozenset(
    {
        "note_id",
        "card_ids",
        "deck_name",
        "selected_tags",
        "due",
        "overdue",
        "lapse_count",
        "interval",
        "retrievability",
        "suspended",
        "buried",
        "last_reviewed_at",
        "snapshot_at",
    }
)


def normalize_snapshot(
    snapshot: AnkiLearningSnapshot | Mapping[str, Any],
) -> AnkiLearningSnapshot:
    """Validate the trust boundary and return a canonical minimized snapshot."""

    if type(snapshot) is AnkiLearningSnapshot:
        raw_notes: object = snapshot.notes
        raw_health: object = snapshot.health
        raw_snapshot_at: object = snapshot.snapshot_at
    elif isinstance(snapshot, Mapping):
        _require_exact_fields(snapshot, _SNAPSHOT_FIELDS, "snapshot")
        raw_notes = snapshot["notes"]
        raw_health = snapshot["health"]
        raw_snapshot_at = snapshot["snapshot_at"]
    else:
        raise TypeError("snapshot must be AnkiLearningSnapshot or approved mapping")

    if not isinstance(raw_notes, (list, tuple)):
        raise TypeError("snapshot notes must be a sequence")
    notes = tuple(_normalize_note(note) for note in raw_notes)
    note_ids = tuple(note.note_id for note in notes)
    if len(note_ids) != len(set(note_ids)):
        raise ValueError("snapshot notes must have unique note IDs")
    notes = tuple(sorted(notes, key=lambda note: note.note_id))
    health = _normalize_health(raw_health)
    return AnkiLearningSnapshot(
        notes=notes,
        health=health,
        snapshot_at=_coerce_required_datetime(raw_snapshot_at, "snapshot_at"),
    )


def snapshot_to_payload(snapshot: AnkiLearningSnapshot) -> dict[str, Any]:
    """Serialize only the approved Task 7.1 schema for an upload boundary."""

    normalized = normalize_snapshot(snapshot)
    return {
        "notes": [_note_payload(note, include_snapshot_at=True) for note in normalized.notes],
        "health": _health_payload(normalized.health),
        "snapshot_at": normalized.snapshot_at.isoformat(),
    }


def canonical_snapshot_json(snapshot: AnkiLearningSnapshot) -> str:
    return json.dumps(
        snapshot_to_payload(snapshot),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )


def snapshot_content_hash(snapshot: AnkiLearningSnapshot) -> str:
    normalized = normalize_snapshot(snapshot)
    content = {
        "notes": [_note_payload(note, include_snapshot_at=False) for note in normalized.notes],
        "health": _health_payload(normalized.health),
    }
    encoded = json.dumps(
        content,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _normalize_health(value: object) -> AnkiSyncHealth:
    if type(value) is AnkiSyncHealth:
        health = value
    elif isinstance(value, Mapping):
        _require_exact_fields(value, _HEALTH_FIELDS, "health")
        health = AnkiSyncHealth(
            reachable=value["reachable"],
            ankiconnect_version=value["ankiconnect_version"],
            active_profile=value["active_profile"],
            collection_accessible=value["collection_accessible"],
            sync_available=value["sync_available"],
            blocking_reason=value["blocking_reason"],
        )
    else:
        raise TypeError("snapshot health must be AnkiSyncHealth or approved mapping")
    for field_name in ("reachable", "collection_accessible", "sync_available"):
        if type(getattr(health, field_name)) is not bool:
            raise ValueError(f"health {field_name} must be a boolean")
    version = health.ankiconnect_version
    if version is not None and (
        isinstance(version, bool) or not isinstance(version, int) or version < 1
    ):
        raise ValueError("health ankiconnect_version must be positive or None")
    for field_name in ("active_profile", "blocking_reason"):
        value = getattr(health, field_name)
        if value is not None and (not isinstance(value, str) or not value.strip()):
            raise ValueError(f"health {field_name} must be text or None")
    return health


def _normalize_note(value: object) -> AnkiNoteLearningState:
    if type(value) is AnkiNoteLearningState:
        note = value
    elif isinstance(value, Mapping):
        _require_exact_fields(value, _NOTE_FIELDS, "note")
        note = AnkiNoteLearningState(
            note_id=value["note_id"],
            card_ids=tuple(value["card_ids"]),
            deck_name=value["deck_name"],
            selected_tags=tuple(value["selected_tags"]),
            due=value["due"],
            overdue=value["overdue"],
            lapse_count=value["lapse_count"],
            interval=value["interval"],
            retrievability=value["retrievability"],
            suspended=value["suspended"],
            buried=value["buried"],
            last_reviewed_at=_coerce_datetime(
                value["last_reviewed_at"], "last_reviewed_at"
            ),
            snapshot_at=_coerce_required_datetime(value["snapshot_at"], "snapshot_at"),
        )
    else:
        raise TypeError("snapshot notes must use AnkiNoteLearningState values")

    note_id = _positive_int(note.note_id, "note_id")
    card_ids = _positive_ints(note.card_ids, "card_ids")
    if len(card_ids) != len(set(card_ids)):
        raise ValueError("note card_ids must be unique")
    if not isinstance(note.deck_name, str):
        raise ValueError("note deck_name must be text")
    selected_tags = _text_sequence(note.selected_tags, "selected_tags")
    due = _optional_bool(note.due, "due")
    overdue = _optional_bool(note.overdue, "overdue")
    lapse_count = _nonnegative_int(note.lapse_count, "lapse_count")
    interval = (
        None if note.interval is None else _nonnegative_int(note.interval, "interval")
    )
    retrievability = _ratio(note.retrievability, "retrievability")
    if type(note.suspended) is not bool or type(note.buried) is not bool:
        raise ValueError("note suspended and buried must be booleans")
    last_reviewed_at = _optional_datetime(note.last_reviewed_at, "last_reviewed_at")
    snapshot_at = _aware_datetime(note.snapshot_at, "snapshot_at")
    return AnkiNoteLearningState(
        note_id=note_id,
        card_ids=card_ids,
        deck_name=note.deck_name,
        selected_tags=selected_tags,
        due=due,
        overdue=overdue,
        lapse_count=lapse_count,
        interval=interval,
        retrievability=retrievability,
        suspended=note.suspended,
        buried=note.buried,
        last_reviewed_at=last_reviewed_at,
        snapshot_at=snapshot_at,
    )


def _note_payload(note: AnkiNoteLearningState, *, include_snapshot_at: bool) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "note_id": note.note_id,
        "card_ids": list(sorted(note.card_ids)),
        "deck_name": note.deck_name,
        "selected_tags": list(
            sorted(note.selected_tags, key=lambda tag: (tag.casefold(), tag))
        ),
        "due": note.due,
        "overdue": note.overdue,
        "lapse_count": note.lapse_count,
        "interval": note.interval,
        "retrievability": note.retrievability,
        "suspended": note.suspended,
        "buried": note.buried,
        "last_reviewed_at": (
            note.last_reviewed_at.isoformat() if note.last_reviewed_at is not None else None
        ),
    }
    if include_snapshot_at:
        payload["snapshot_at"] = note.snapshot_at.isoformat()
    return payload


def _health_payload(health: AnkiSyncHealth) -> dict[str, Any]:
    return {
        "reachable": health.reachable,
        "ankiconnect_version": health.ankiconnect_version,
        "active_profile": health.active_profile,
        "collection_accessible": health.collection_accessible,
        "sync_available": health.sync_available,
        "blocking_reason": health.blocking_reason,
    }


def _require_exact_fields(value: Mapping[str, Any], expected: frozenset[str], name: str) -> None:
    actual = set(value)
    if actual != expected:
        unexpected = sorted(actual - expected)
        missing = sorted(expected - actual)
        if unexpected:
            raise ValueError(f"unexpected {name} fields: {unexpected}")
        raise ValueError(f"missing {name} fields: {missing}")


def _positive_int(value: object, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{field_name} must be a positive integer")
    return value


def _positive_ints(values: Sequence[object], field_name: str) -> tuple[int, ...]:
    if not isinstance(values, (list, tuple)):
        raise TypeError(f"{field_name} must be a sequence")
    return tuple(_positive_int(value, field_name) for value in values)


def _text_sequence(values: Sequence[object], field_name: str) -> tuple[str, ...]:
    if not isinstance(values, (list, tuple)):
        raise TypeError(f"{field_name} must be a sequence")
    result = tuple(value.strip() if isinstance(value, str) else "" for value in values)
    if any(not value for value in result):
        raise ValueError(f"{field_name} must contain text")
    if len(result) != len(set(result)):
        raise ValueError(f"{field_name} must be unique")
    return tuple(sorted(result, key=lambda tag: (tag.casefold(), tag)))


def _optional_bool(value: object, field_name: str) -> bool | None:
    if value is not None and type(value) is not bool:
        raise ValueError(f"{field_name} must be a boolean or None")
    return value


def _nonnegative_int(value: object, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{field_name} must be a non-negative integer")
    return value


def _ratio(value: object, field_name: str) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{field_name} must be a number or None")
    result = float(value)
    if not isfinite(result) or not 0 <= result <= 1:
        raise ValueError(f"{field_name} must be between 0 and 1")
    return result


def _optional_datetime(value: object, field_name: str) -> datetime | None:
    if value is None:
        return None
    return _aware_datetime(value, field_name)


def _coerce_datetime(value: object, field_name: str) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value)
        except ValueError as exc:
            raise ValueError(f"{field_name} must be a valid timestamp") from exc
    return _aware_datetime(value, field_name)


def _coerce_required_datetime(value: object, field_name: str) -> datetime:
    parsed = _coerce_datetime(value, field_name)
    if parsed is None:
        raise TypeError(f"{field_name} must be a datetime")
    return parsed


def _aware_datetime(value: object, field_name: str) -> datetime:
    if not isinstance(value, datetime):
        raise TypeError(f"{field_name} must be a datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")
    return value.astimezone(UTC)


__all__ = [
    "AnkiLearningRepository",
    "AnkiNoteStateRecord",
    "AnkiStaleness",
    "AnkiStalenessThresholds",
    "AnkiSyncHealthView",
    "AnkiSyncRun",
    "StalenessThresholds",
    "canonical_snapshot_json",
    "normalize_snapshot",
    "snapshot_content_hash",
    "snapshot_to_payload",
]
