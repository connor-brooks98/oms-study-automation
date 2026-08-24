from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import inspect

from oms_hub.db import Database
from oms_hub.mastery.models import (
    AssistanceLevel,
    ConfidenceRating,
    LearnerEvent,
    LearnerEventType,
)
from oms_hub.mastery.repository import MasteryRepository


@pytest.fixture
def database(tmp_path: Path) -> Iterator[Database]:
    value = Database(f"sqlite:///{tmp_path / 'mastery.db'}")
    value.create_schema()
    try:
        yield value
    finally:
        value.close()


def test_repository_constructor_does_not_create_schema(tmp_path: Path) -> None:
    database = Database(f"sqlite:///{tmp_path / 'uninitialized.db'}")
    try:
        assert not inspect(database.engine).has_table("learner_events")
        MasteryRepository(database)
        assert not inspect(database.engine).has_table("learner_events")
    finally:
        database.close()


def _event(
    client_event_id: str,
    *,
    objective_ids: tuple[str, ...] = ("objective-1",),
    event_type: LearnerEventType = LearnerEventType.QUESTION_ANSWERED,
    occurred_at: datetime = datetime(2026, 8, 23, 12, tzinfo=UTC),
    correct: bool | None = True,
) -> LearnerEvent:
    return LearnerEvent(
        client_event_id=client_event_id,
        event_type=event_type,
        objective_ids=objective_ids,
        question_version_id="question-version-1",
        correct=correct,
        selected_option="B",
        difficulty=3,
        response_duration_ms=1250,
        confidence=ConfidenceRating.CONFIDENT,
        assistance_level=AssistanceLevel.NONE,
        occurred_at=occurred_at,
        source_snapshot_hash="snapshot-hash",
    )


def test_append_event_is_durable_and_objective_queries_preserve_event_order(
    database: Database,
) -> None:
    repository = MasteryRepository(database)
    first = repository.append_event(_event("client-1"))
    second = repository.append_event(
        _event(
            "client-2",
            occurred_at=first.occurred_at + timedelta(minutes=1),
            correct=False,
        )
    )
    third = repository.append_event(
        _event(
            "client-3",
            objective_ids=("objective-2",),
            occurred_at=first.occurred_at + timedelta(minutes=2),
        )
    )

    assert repository.events_for_objective("objective-1") == [first, second]
    assert repository.events_for_objective("objective-2") == [third]
    assert repository.events_for_objective("missing") == []


def test_duplicate_client_event_id_returns_one_immutable_event(database: Database) -> None:
    repository = MasteryRepository(database)
    first = repository.append_event(_event("client-duplicate"))
    duplicate = repository.append_event(
        _event(
            "client-duplicate",
            occurred_at=first.occurred_at + timedelta(hours=1),
            correct=False,
        )
    )

    assert duplicate == first
    assert repository.events_for_objective("objective-1") == [first]


def test_repository_does_not_persist_incomplete_question_event(
    database: Database,
) -> None:
    repository = MasteryRepository(database)

    with pytest.raises(ValueError, match="correct"):
        repository.append_event(
            LearnerEvent(
                client_event_id="client-incomplete",
                event_type=LearnerEventType.QUESTION_ANSWERED,
                objective_ids=("objective-1",),
                question_version_id="question-version-1",
                source_snapshot_hash="snapshot-hash",
            )
        )

    assert repository.events_for_objective("objective-1") == []


def test_repository_exposes_append_and_query_only_for_event_writes(
    database: Database,
) -> None:
    repository = MasteryRepository(database)

    assert hasattr(repository, "append_event")
    assert hasattr(repository, "events_for_objective")
    assert not hasattr(repository, "update_event")
    assert not hasattr(repository, "delete_event")
