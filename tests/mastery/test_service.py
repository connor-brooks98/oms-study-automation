from datetime import UTC, datetime, timedelta

import pytest

from oms_hub.mastery.models import LearnerEvent, LearnerEventType
from oms_hub.mastery.service import MasteryService


def test_rebuild_equivalence_requires_explicit_shared_as_of() -> None:
    event_time = datetime(2026, 8, 23, 12, tzinfo=UTC)
    service = MasteryService(now=lambda: event_time)
    first = LearnerEvent(
        client_event_id="implicit-first",
        event_type=LearnerEventType.QUESTION_ANSWERED,
        objective_ids=("objective-1",),
        question_version_id="question-version-implicit-first",
        correct=True,
        selected_option="A",
        difficulty=3,
        occurred_at=event_time - timedelta(days=30),
        source_snapshot_hash="snapshot-hash",
    )
    second = LearnerEvent(
        client_event_id="implicit-second",
        event_type=LearnerEventType.QUESTION_ANSWERED,
        objective_ids=("objective-1",),
        question_version_id="question-version-implicit-second",
        correct=False,
        selected_option="B",
        difficulty=4,
        occurred_at=event_time - timedelta(days=1),
        source_snapshot_hash="snapshot-hash",
    )

    incremental = service.recompute_on_event("objective-1", [first], second, as_of=event_time)
    rebuilt = service.rebuild("objective-1", [first, second], as_of=event_time)

    assert incremental.serialize() == rebuilt.serialize()


def test_service_rebuild_requires_as_of() -> None:
    service = MasteryService()

    with pytest.raises(TypeError):
        service.rebuild("objective-1", [])  # type: ignore[call-arg]


def test_incremental_recompute_and_full_rebuild_serialize_identically() -> None:
    now = datetime(2026, 8, 23, 12, tzinfo=UTC)
    service = MasteryService(now=lambda: now)
    first = LearnerEvent(
        client_event_id="first",
        event_type=LearnerEventType.QUESTION_ANSWERED,
        objective_ids=("objective-1",),
        question_version_id="question-version-first",
        correct=True,
        selected_option="A",
        difficulty=3,
        occurred_at=now,
        source_snapshot_hash="snapshot-hash",
    )
    second = LearnerEvent(
        client_event_id="second",
        event_type=LearnerEventType.QUESTION_ANSWERED,
        objective_ids=("objective-1",),
        question_version_id="question-version-second",
        correct=False,
        selected_option="B",
        difficulty=4,
        occurred_at=now,
        source_snapshot_hash="snapshot-hash",
    )

    incremental = service.recompute_on_event("objective-1", [first], second, as_of=now)
    rebuilt = service.rebuild("objective-1", [first, second], as_of=now)

    assert incremental.serialize() == rebuilt.serialize()
