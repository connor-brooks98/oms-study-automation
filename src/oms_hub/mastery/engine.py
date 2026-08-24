from __future__ import annotations

import json
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from math import isfinite
from numbers import Real
from typing import Protocol

from oms_hub.mastery.models import LearnerEvent, LearnerEventType
from oms_hub.mastery.weights import ASSISTANCE_MULTIPLIERS, event_weight, recency_weight

ALGORITHM_VERSION = "mastery-beta-v1"
_SECONDS_PER_DAY = 24 * 60 * 60


class ObjectiveRecallRetention(Protocol):
    """Local adapter for an approved objective-scoped recall value."""

    def recall_retention(self, objective_id: str) -> float | None: ...


@dataclass(frozen=True, slots=True)
class MasterySnapshot:
    objective_id: str
    application_score: float
    timed_application_score: float | None
    recall_retention: float | None
    assistance_dependence: float | None
    evidence_weight: float
    last_tested_at: datetime | None
    status: str
    algorithm_version: str = ALGORITHM_VERSION

    def __post_init__(self) -> None:
        objective_id = _objective_id(self.objective_id)
        object.__setattr__(self, "objective_id", objective_id)
        _bounded_score(self.application_score, "application_score", 0.0, 100.0)
        if self.timed_application_score is not None:
            _bounded_score(
                self.timed_application_score,
                "timed_application_score",
                0.0,
                100.0,
            )
        if self.recall_retention is not None:
            _bounded_score(self.recall_retention, "recall_retention", 0.0, 1.0)
        if self.assistance_dependence is not None:
            _bounded_score(
                self.assistance_dependence,
                "assistance_dependence",
                0.0,
                100.0,
            )
        _finite_number(self.evidence_weight, "evidence_weight")
        if self.evidence_weight < 0:
            raise ValueError("evidence_weight must be non-negative")
        if self.last_tested_at is not None:
            object.__setattr__(
                self,
                "last_tested_at",
                _utc_datetime(self.last_tested_at, "last_tested_at"),
            )
        if self.status not in {"untested", "tested"}:
            raise ValueError("status must be untested or tested")
        if self.algorithm_version != ALGORITHM_VERSION:
            raise ValueError(f"algorithm_version must be {ALGORITHM_VERSION}")

    def to_dict(self) -> dict[str, object]:
        return {
            "algorithm_version": self.algorithm_version,
            "application_score": self.application_score,
            "assistance_dependence": self.assistance_dependence,
            "evidence_weight": self.evidence_weight,
            "last_tested_at": (
                self.last_tested_at.isoformat() if self.last_tested_at is not None else None
            ),
            "objective_id": self.objective_id,
            "recall_retention": self.recall_retention,
            "status": self.status,
            "timed_application_score": self.timed_application_score,
        }

    def to_json(self) -> str:
        return json.dumps(
            self.to_dict(),
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )

    def serialize(self) -> bytes:
        return self.to_json().encode("utf-8")


class MasteryEngine:
    def __init__(
        self,
        *,
        now: Callable[[], datetime] | datetime | None = None,
    ) -> None:
        self._now = now if now is not None else (lambda: datetime.now(UTC))

    def compute(
        self,
        objective_id: str,
        events: Iterable[LearnerEvent],
        anki_snapshot: ObjectiveRecallRetention | None = None,
        *,
        as_of: datetime | None = None,
    ) -> MasterySnapshot:
        objective_id = _objective_id(objective_id)
        now = self._as_of(as_of)
        scored_events: list[tuple[LearnerEvent, float, float]] = []
        for event in events:
            if not isinstance(event, LearnerEvent):
                continue
            _utc_datetime(event.occurred_at, "event occurred_at")
            if not _is_scored_for_objective(event, objective_id):
                continue
            age_days = max(
                (now - event.occurred_at).total_seconds() / _SECONDS_PER_DAY,
                0.0,
            )
            decay = recency_weight(age_days)
            scored_events.append((event, decay, event_weight(event)))

        scored_events.sort(key=lambda item: (item[0].occurred_at, item[0].id))
        weighted_correct = 0.0
        weighted_incorrect = 0.0
        assistance_total = 0.0
        assistance_dependence = 0.0
        last_tested_at: datetime | None = None
        for event, decay, base_weight in scored_events:
            weighted_evidence = base_weight * decay
            if event.correct:
                weighted_correct += weighted_evidence
            else:
                weighted_incorrect += weighted_evidence
            assistance_multiplier = ASSISTANCE_MULTIPLIERS[event.assistance_level]
            assistance_total += decay
            assistance_dependence += (1.0 - assistance_multiplier) * decay
            last_tested_at = event.occurred_at

        alpha = 2.0 + weighted_correct
        beta = 2.0 + weighted_incorrect
        application_score = 100.0 * alpha / (alpha + beta)
        evidence_weight = weighted_correct + weighted_incorrect
        dependence = (
            100.0 * assistance_dependence / assistance_total if scored_events else None
        )
        return MasterySnapshot(
            objective_id=objective_id,
            application_score=application_score,
            timed_application_score=None,
            recall_retention=_recall_retention(anki_snapshot, objective_id),
            assistance_dependence=dependence,
            evidence_weight=evidence_weight,
            last_tested_at=last_tested_at,
            status="tested" if scored_events else "untested",
        )

    def _as_of(self, as_of: datetime | None) -> datetime:
        if as_of is not None:
            return _utc_datetime(as_of, "as_of")
        raw_now = self._now() if callable(self._now) else self._now
        return _utc_datetime(raw_now, "now")


def _is_scored_for_objective(event: LearnerEvent, objective_id: str) -> bool:
    return (
        event.event_type is LearnerEventType.QUESTION_ANSWERED
        and objective_id in event.objective_ids
        and event_weight(event) > 0.0
    )


def _recall_retention(
    snapshot: ObjectiveRecallRetention | None,
    objective_id: str,
) -> float | None:
    if snapshot is None:
        return None
    value = snapshot.recall_retention(objective_id)
    if value is None:
        return None
    _bounded_score(value, "recall_retention", 0.0, 1.0)
    return float(value)


def _objective_id(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("objective_id must be a non-empty string")
    return value.strip()


def _utc_datetime(value: object, field_name: str) -> datetime:
    if not isinstance(value, datetime):
        raise ValueError(f"{field_name} must be a datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware UTC")
    return value.astimezone(UTC)


def _bounded_score(value: object, field_name: str, minimum: float, maximum: float) -> None:
    numeric = _finite_number(value, field_name)
    if not minimum <= numeric <= maximum:
        raise ValueError(f"{field_name} must be between {minimum} and {maximum}")


def _finite_number(value: object, field_name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, Real) or not isfinite(float(value)):
        raise ValueError(f"{field_name} must be a finite number")
    return float(value)
