from __future__ import annotations

from collections.abc import Mapping
from math import isfinite
from numbers import Real
from types import MappingProxyType

from oms_hub.mastery.models import AssistanceLevel, ConfidenceRating, LearnerEvent, LearnerEventType

ASSISTANCE_MULTIPLIERS: Mapping[AssistanceLevel, float] = MappingProxyType(
    {
        AssistanceLevel.NONE: 1.00,
        AssistanceLevel.CONCEPT_HINT: 0.70,
        AssistanceLevel.SOURCE_EXCERPT: 0.55,
        AssistanceLevel.FULL_EXPLANATION: 0.35,
        AssistanceLevel.ANSWER_REVEALED: 0.10,
    }
)

DIFFICULTY_MULTIPLIERS: Mapping[int, float] = MappingProxyType(
    {
        1: 0.75,
        2: 0.90,
        3: 1.00,
        4: 1.20,
        5: 1.40,
    }
)

_CORRECT_CONFIDENCE_MULTIPLIERS: Mapping[ConfidenceRating, float] = MappingProxyType(
    {
        ConfidenceRating.CONFIDENT: 1.10,
        ConfidenceRating.UNSURE: 0.85,
        ConfidenceRating.GUESSED: 0.65,
        ConfidenceRating.NOT_RECORDED: 1.00,
    }
)
_INCORRECT_CONFIDENCE_MULTIPLIERS: Mapping[ConfidenceRating, float] = MappingProxyType(
    {
        ConfidenceRating.CONFIDENT: 1.15,
        ConfidenceRating.UNSURE: 1.00,
        ConfidenceRating.GUESSED: 1.00,
        ConfidenceRating.NOT_RECORDED: 1.00,
    }
)


def recency_weight(age_days: float, half_life_days: float = 60.0) -> float:
    """Return exponential evidence decay for a non-negative age."""

    age = _finite_number(age_days, "age_days")
    half_life = _finite_number(half_life_days, "half_life_days")
    if age < 0:
        raise ValueError("age_days must be non-negative")
    if half_life <= 0:
        raise ValueError("half_life_days must be positive")
    return float(0.5 ** (age / half_life))


def event_weight(event: object) -> float:
    """Return the approved base weight for one complete question event."""

    if not isinstance(event, LearnerEvent):
        return 0.0
    if event.event_type is not LearnerEventType.QUESTION_ANSWERED:
        return 0.0
    if not isinstance(event.correct, bool):
        return 0.0
    if (
        event.difficulty is None
        or isinstance(event.difficulty, bool)
        or not isinstance(event.difficulty, int)
    ):
        return 0.0
    difficulty_multiplier = DIFFICULTY_MULTIPLIERS.get(event.difficulty)
    assistance_multiplier = ASSISTANCE_MULTIPLIERS.get(event.assistance_level)
    if difficulty_multiplier is None or assistance_multiplier is None:
        return 0.0
    confidence_multipliers = (
        _CORRECT_CONFIDENCE_MULTIPLIERS
        if event.correct
        else _INCORRECT_CONFIDENCE_MULTIPLIERS
    )
    confidence_multiplier = confidence_multipliers.get(event.confidence)
    if confidence_multiplier is None:
        return 0.0
    return assistance_multiplier * difficulty_multiplier * confidence_multiplier


def _finite_number(value: object, field_name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, Real) or not isfinite(float(value)):
        raise ValueError(f"{field_name} must be a finite number")
    return float(value)
