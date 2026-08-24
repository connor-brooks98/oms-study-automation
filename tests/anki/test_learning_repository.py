from dataclasses import asdict, replace
from datetime import UTC, datetime, timedelta

import pytest

from oms_hub.anki.learning_contracts import (
    AnkiLearningSnapshot,
    AnkiNoteLearningState,
    AnkiSyncHealth,
)
from oms_hub.anki.learning_repository import (
    AnkiLearningRepository,
    AnkiStaleness,
    snapshot_to_payload,
)

BASE_TIME = datetime(2026, 8, 23, 12, 0, tzinfo=UTC)


def _health() -> AnkiSyncHealth:
    return AnkiSyncHealth(
        reachable=True,
        ankiconnect_version=6,
        active_profile="Disposable Test",
        collection_accessible=True,
        sync_available=True,
        blocking_reason=None,
    )


def _state(
    note_id: int = 42,
    *,
    snapshot_at: datetime = BASE_TIME,
    lapse_count: int = 3,
    selected_tags: tuple[str, ...] = ("lecture::heme",),
) -> AnkiNoteLearningState:
    return AnkiNoteLearningState(
        note_id=note_id,
        card_ids=(4201, 4202),
        deck_name="AnKing Step Deck::Heme",
        selected_tags=selected_tags,
        due=True,
        overdue=False,
        lapse_count=lapse_count,
        interval=14,
        retrievability=0.8,
        suspended=False,
        buried=False,
        last_reviewed_at=BASE_TIME - timedelta(days=1),
        snapshot_at=snapshot_at,
    )


def _snapshot(
    *states: AnkiNoteLearningState,
    snapshot_at: datetime = BASE_TIME,
) -> AnkiLearningSnapshot:
    return AnkiLearningSnapshot(
        notes=states,
        health=_health(),
        snapshot_at=snapshot_at,
    )


def test_sync_history_preserves_receipts_and_replaces_current_state() -> None:
    now = BASE_TIME + timedelta(minutes=1)
    repository = AnkiLearningRepository(now=lambda: now)

    first = repository.record_sync(_snapshot(_state()))
    second_snapshot = _snapshot(
        _state(lapse_count=4, snapshot_at=BASE_TIME + timedelta(hours=1)),
        snapshot_at=BASE_TIME + timedelta(hours=1),
    )
    second = repository.record_sync(second_snapshot)

    assert first.changed is True
    assert second.changed is True
    assert second.content_hash != first.content_hash
    assert repository.latest_note_state(42) == second_snapshot.notes[0]
    assert repository.latest_sync_run() == second
    assert repository.sync_history() == (first, second)


def test_staleness_boundaries_are_exact_and_never_synced_is_nonblocking() -> None:
    clock = [BASE_TIME]
    repository = AnkiLearningRepository(now=lambda: clock[0])

    assert repository.latest_sync_health().staleness is AnkiStaleness.NEVER_SYNCED

    repository.record_sync(_snapshot(_state()))
    clock[0] = BASE_TIME + timedelta(hours=24) - timedelta(microseconds=1)
    assert repository.latest_sync_health().staleness is AnkiStaleness.FRESH
    clock[0] = BASE_TIME + timedelta(hours=24)
    assert repository.latest_sync_health().staleness is AnkiStaleness.STALE
    clock[0] = BASE_TIME + timedelta(days=7)
    assert repository.latest_sync_health().staleness is AnkiStaleness.STALE
    clock[0] = BASE_TIME + timedelta(days=7, microseconds=1)
    assert repository.latest_sync_health().staleness is AnkiStaleness.VERY_STALE
    assert repository.latest_sync_health().blocking_reason is None


def test_same_content_retry_has_no_duplicate_note_state_and_records_no_change_receipt() -> None:
    repository = AnkiLearningRepository(now=lambda: BASE_TIME)
    first_snapshot = _snapshot(_state(), snapshot_at=BASE_TIME)
    retry_snapshot = _snapshot(
        replace(
            _state(snapshot_at=BASE_TIME + timedelta(minutes=1)),
            snapshot_at=BASE_TIME + timedelta(minutes=1),
        ),
        snapshot_at=BASE_TIME + timedelta(minutes=1),
    )

    first = repository.record_sync(first_snapshot)
    retry = repository.record_sync(retry_snapshot)

    assert first.changed is True
    assert retry.changed is False
    assert retry.no_change is True
    assert retry.status == "no_change"
    assert retry.content_hash == first.content_hash
    assert len(repository.note_state_history()) == 1
    assert len(repository.sync_history()) == 2
    assert repository.latest_note_state(42) == first_snapshot.notes[0]


def test_content_hash_and_order_are_deterministic() -> None:
    repository = AnkiLearningRepository(now=lambda: BASE_TIME)
    state_one = _state(1, selected_tags=("b", "a"))
    state_two = _state(2, selected_tags=("a", "b"))
    first = repository.record_sync(_snapshot(state_two, state_one))
    second = repository.record_sync(_snapshot(
        replace(state_one, card_ids=(4202, 4201)),
        replace(state_two, card_ids=(4202, 4201)),
        snapshot_at=BASE_TIME + timedelta(seconds=1),
    ))

    assert first.content_hash == second.content_hash
    assert second.no_change is True
    assert [state.note_id for state in repository.latest_note_states()] == [1, 2]


def test_mixed_case_tag_order_is_canonical_and_idempotent() -> None:
    repository = AnkiLearningRepository(now=lambda: BASE_TIME)
    first_snapshot = _snapshot(_state(selected_tags=("a", "A")))
    retry_snapshot = _snapshot(_state(selected_tags=("A", "a")))

    assert snapshot_to_payload(first_snapshot) == snapshot_to_payload(retry_snapshot)
    first = repository.record_sync(first_snapshot)
    retry = repository.record_sync(retry_snapshot)

    assert first.content_hash == retry.content_hash
    assert retry.no_change is True
    assert len(repository.note_state_history()) == 1


def test_changed_content_creates_a_new_note_state_history_row() -> None:
    repository = AnkiLearningRepository(now=lambda: BASE_TIME)
    repository.record_sync(_snapshot(_state()))
    repository.record_sync(
        _snapshot(
            _state(
                selected_tags=("lecture::heme", "lecture::new"),
                snapshot_at=BASE_TIME + timedelta(hours=1),
            ),
            snapshot_at=BASE_TIME + timedelta(hours=1),
        )
    )

    assert len(repository.note_state_history(42)) == 2
    latest = repository.latest_note_state(42)
    assert latest is not None
    assert latest.selected_tags == (
        "lecture::heme",
        "lecture::new",
    )


def test_source_minimization_rejects_extra_private_card_fields() -> None:
    payload = asdict(_snapshot(_state()))
    payload["notes"][0]["fields"] = {"Text": "private card HTML"}
    payload["notes"][0]["media"] = ["image.png"]

    with pytest.raises(ValueError, match="unexpected note fields"):
        AnkiLearningRepository().record_sync(payload)


def test_approved_serialized_payload_round_trips_without_extra_fields() -> None:
    snapshot = _snapshot(_state())

    saved = AnkiLearningRepository().record_sync(snapshot_to_payload(snapshot))

    assert saved.note_count == 1


def test_repository_rejects_naive_snapshot_timestamps() -> None:
    naive = datetime(2026, 8, 23, 12, 0)
    snapshot = _snapshot(_state(snapshot_at=naive), snapshot_at=naive)

    with pytest.raises(ValueError, match="timezone-aware"):
        AnkiLearningRepository().record_sync(snapshot)
