from dataclasses import FrozenInstanceError
from datetime import UTC, datetime

import pytest

from oms_hub.mastery.models import (
    AssistanceLevel,
    ConfidenceRating,
    LearnerEvent,
    LearnerEventType,
)


def test_event_enums_match_the_reserved_mastery_contract() -> None:
    assert [item.value for item in LearnerEventType] == [
        "question_answered",
        "hint_requested",
        "source_opened",
        "ask_question_submitted",
        "ask_answer_completed",
        "answer_revealed",
        "question_retried",
        "anki_snapshot_observed",
        "manual_mastery_reset",
    ]
    assert [item.value for item in AssistanceLevel] == [
        "none",
        "concept_hint",
        "source_excerpt",
        "full_explanation",
        "answer_revealed",
    ]
    assert [item.value for item in ConfidenceRating] == [
        "confident",
        "unsure",
        "guessed",
        "not_recorded",
    ]


def test_learner_event_is_immutable_and_normalizes_collection_fields() -> None:
    event = LearnerEvent(
        client_event_id="client-1",
        event_type=LearnerEventType.QUESTION_ANSWERED,
        objective_ids=("objective-1", "objective-1", "objective-2"),
        question_version_id="question-version-1",
        correct=True,
        selected_option="B",
        difficulty=4,
        response_duration_ms=1250,
        confidence=ConfidenceRating.CONFIDENT,
        assistance_level=AssistanceLevel.NONE,
        occurred_at=datetime(2026, 8, 23, 12, 30, tzinfo=UTC),
        source_snapshot_hash="snapshot-hash",
    )

    assert event.event_type is LearnerEventType.QUESTION_ANSWERED
    assert event.objective_ids == ("objective-1", "objective-2")
    assert event.confidence is ConfidenceRating.CONFIDENT
    assert event.assistance_level is AssistanceLevel.NONE
    assert event.occurred_at.isoformat() == "2026-08-23T12:30:00+00:00"
    with pytest.raises(FrozenInstanceError):
        event.correct = False  # type: ignore[misc]


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("client_event_id", "", "client_event_id"),
        ("difficulty", 6, "difficulty"),
        ("response_duration_ms", -1, "response_duration_ms"),
    ],
)
def test_event_rejects_invalid_boundary_values(
    field: str,
    value: object,
    message: str,
) -> None:
    values: dict[str, object] = {
        "client_event_id": "client-1",
        "event_type": LearnerEventType.QUESTION_ANSWERED,
        "difficulty": 3,
        "response_duration_ms": 100,
    }
    values[field] = value

    with pytest.raises(ValueError, match=message):
        LearnerEvent(**values)  # type: ignore[arg-type]
