from datetime import UTC, datetime

import pytest

from oms_hub.mastery.models import (
    AssistanceLevel,
    ConfidenceRating,
    LearnerEvent,
    LearnerEventType,
)
from oms_hub.mastery.weights import (
    ASSISTANCE_MULTIPLIERS,
    DIFFICULTY_MULTIPLIERS,
    event_weight,
    recency_weight,
)


def _question(
    *,
    client_event_id: str = "question-1",
    correct: bool | None = True,
    assistance_level: AssistanceLevel = AssistanceLevel.NONE,
    difficulty: int | None = 3,
    confidence: ConfidenceRating = ConfidenceRating.NOT_RECORDED,
) -> LearnerEvent:
    return LearnerEvent(
        client_event_id=client_event_id,
        event_type=LearnerEventType.QUESTION_ANSWERED,
        objective_ids=("objective-1",),
        question_version_id="question-version-1",
        correct=correct,
        selected_option="B",
        difficulty=difficulty,
        confidence=confidence,
        assistance_level=assistance_level,
        occurred_at=datetime(2026, 8, 23, tzinfo=UTC),
        source_snapshot_hash="snapshot-hash",
    )


def test_approved_multipliers_are_immutable_data() -> None:
    assert dict(ASSISTANCE_MULTIPLIERS) == {
        AssistanceLevel.NONE: 1.0,
        AssistanceLevel.CONCEPT_HINT: 0.7,
        AssistanceLevel.SOURCE_EXCERPT: 0.55,
        AssistanceLevel.FULL_EXPLANATION: 0.35,
        AssistanceLevel.ANSWER_REVEALED: 0.1,
    }
    assert dict(DIFFICULTY_MULTIPLIERS) == {
        1: 0.75,
        2: 0.9,
        3: 1.0,
        4: 1.2,
        5: 1.4,
    }
    with pytest.raises(TypeError):
        ASSISTANCE_MULTIPLIERS[AssistanceLevel.NONE] = 2.0  # type: ignore[index]
    with pytest.raises(TypeError):
        DIFFICULTY_MULTIPLIERS[3] = 2.0  # type: ignore[index]


def test_recency_half_life() -> None:
    assert recency_weight(0) == pytest.approx(1.0)
    assert recency_weight(60) == pytest.approx(0.5)
    assert recency_weight(120) == pytest.approx(0.25)


@pytest.mark.parametrize(
    ("age_days", "half_life_days"),
    [(-1.0, 60.0), (0.0, 0.0), (0.0, -1.0), (float("nan"), 60.0), (0.0, float("inf"))],
)
def test_recency_rejects_invalid_math_inputs(age_days: float, half_life_days: float) -> None:
    with pytest.raises(ValueError):
        recency_weight(age_days, half_life_days)


def test_event_weight_applies_assistance_difficulty_and_confidence() -> None:
    assert event_weight(
        _question(
            assistance_level=AssistanceLevel.CONCEPT_HINT,
            difficulty=4,
            confidence=ConfidenceRating.GUESSED,
        )
    ) == pytest.approx(0.7 * 1.2 * 0.65)
    assert event_weight(
        _question(
            correct=False,
            assistance_level=AssistanceLevel.SOURCE_EXCERPT,
            difficulty=5,
            confidence=ConfidenceRating.CONFIDENT,
        )
    ) == pytest.approx(0.55 * 1.4 * 1.15)


def test_event_weight_ignores_non_question_and_incomplete_events() -> None:
    assert event_weight(
        LearnerEvent(
            client_event_id="hint-1",
            event_type=LearnerEventType.HINT_REQUESTED,
        )
    ) == 0.0
    assert event_weight(_question(difficulty=None)) == 0.0
