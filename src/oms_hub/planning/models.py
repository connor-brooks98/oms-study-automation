"""Immutable contracts for Board Runway planning data."""

from dataclasses import dataclass, field
from datetime import UTC, datetime
from datetime import date as Date
from math import isfinite
from numbers import Real
from typing import ClassVar
from uuid import uuid4

_DEFAULT_EARLIEST_DATE = Date(2027, 5, 1)
_DEFAULT_LATEST_DATE = Date(2027, 7, 31)


def _identifier(prefix: str) -> str:
    return f"{prefix}_{uuid4()}"


def _now() -> datetime:
    return datetime.now(UTC)


def _text(value: str, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must not be blank")
    return value.strip()


def _date(value: Date, field_name: str) -> None:
    if isinstance(value, datetime) or not isinstance(value, Date):
        raise TypeError(f"{field_name} must be a date")


def _timestamp(value: datetime | None, field_name: str) -> datetime | None:
    if value is None:
        return None
    if not isinstance(value, datetime):
        raise TypeError(f"{field_name} must be a datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")
    return value.astimezone(UTC)


def _required_timestamp(value: datetime, field_name: str) -> datetime:
    normalized = _timestamp(value, field_name)
    if normalized is None:
        raise TypeError(f"{field_name} must be a datetime")
    return normalized


def _ratio(value: float | None, field_name: str) -> None:
    if value is not None and (not isinstance(value, Real) or isinstance(value, bool)):
        raise TypeError(f"{field_name} must be a number or None")
    if value is not None and (not isfinite(float(value)) or not 0 <= value <= 1):
        raise ValueError(f"{field_name} must be between 0 and 1")


def _count(value: int, field_name: str) -> None:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ValueError(f"{field_name} must be a non-negative integer")


def _strings(values: tuple[str, ...], field_name: str) -> tuple[str, ...]:
    normalized = tuple(_text(value, field_name) for value in values)
    if len(normalized) != len(set(normalized)):
        raise ValueError(f"{field_name} must be unique")
    return normalized


@dataclass(frozen=True, slots=True)
class BoardTarget:
    """User-editable exam family and target window."""

    exam_family: str = "COMLEX Level 1"
    earliest_date: Date = _DEFAULT_EARLIEST_DATE
    latest_date: Date = _DEFAULT_LATEST_DATE
    target_id: str = field(default_factory=lambda: _identifier("target"))

    _DEFAULT_ID: ClassVar[str] = "default-comlex-level-1"

    @classmethod
    def default(cls) -> "BoardTarget":
        return cls(target_id=cls._DEFAULT_ID)

    def __post_init__(self) -> None:
        _text(self.exam_family, "exam_family")
        _date(self.earliest_date, "earliest_date")
        _date(self.latest_date, "latest_date")
        if self.earliest_date > self.latest_date:
            raise ValueError("earliest_date must not be after latest_date")
        _text(self.target_id, "target_id")


@dataclass(frozen=True, slots=True)
class StudyAllocation:
    """One transparent, non-predictive unit of planned study work."""

    category: str
    planned_count: int = 0
    objective_ids: tuple[str, ...] = ()
    rationale: str = ""
    allocation_id: str = field(default_factory=lambda: _identifier("allocation"))

    def __post_init__(self) -> None:
        _text(self.category, "category")
        _count(self.planned_count, "planned_count")
        object.__setattr__(self, "objective_ids", _strings(self.objective_ids, "objective_ids"))
        if not isinstance(self.rationale, str):
            raise TypeError("rationale must be a string")
        _text(self.allocation_id, "allocation_id")

    @property
    def kind(self) -> str:
        return self.category

    @property
    def count(self) -> int:
        return self.planned_count


@dataclass(frozen=True, slots=True)
class StudyPlanDay:
    """A dated collection of transparent study allocations."""

    date: Date
    allocations: tuple[StudyAllocation, ...] = ()
    plan_id: str = field(default_factory=lambda: _identifier("plan"))
    created_at: datetime = field(default_factory=_now)
    input_snapshot_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _date(self.date, "date")
        allocations = tuple(self.allocations)
        if any(not isinstance(item, StudyAllocation) for item in allocations):
            raise TypeError("allocations must contain StudyAllocation values")
        allocation_ids = tuple(item.allocation_id for item in allocations)
        if len(allocation_ids) != len(set(allocation_ids)):
            raise ValueError("allocations must have unique IDs")
        object.__setattr__(self, "allocations", allocations)
        _text(self.plan_id, "plan_id")
        object.__setattr__(
            self,
            "created_at",
            _required_timestamp(self.created_at, "created_at"),
        )
        object.__setattr__(
            self,
            "input_snapshot_ids",
            _strings(self.input_snapshot_ids, "input_snapshot_ids"),
        )

    @property
    def plan_date(self) -> Date:
        return self.date


@dataclass(frozen=True, slots=True)
class ExternalAssessment:
    """An immutable user-entered assessment; corrections are replacements."""

    assessment_name: str
    date: Date
    score_result: str | int | float
    scale: str
    notes: str = ""
    source: str = "user"
    assessment_id: str = field(default_factory=lambda: _identifier("assessment"))
    recorded_at: datetime = field(default_factory=_now)
    retired_at: datetime | None = None
    replacement_id: str | None = None
    replaces_assessment_id: str | None = None

    def __post_init__(self) -> None:
        _text(self.assessment_name, "assessment_name")
        _date(self.date, "date")
        if isinstance(self.score_result, bool) or not isinstance(
            self.score_result, (str, int, float)
        ):
            raise TypeError("score_result must be text or a number")
        if isinstance(self.score_result, str):
            _text(self.score_result, "score_result")
        elif isinstance(self.score_result, float) and not isfinite(self.score_result):
            raise ValueError("score_result must be finite")
        _text(self.scale, "scale")
        if not isinstance(self.notes, str):
            raise TypeError("notes must be a string")
        _text(self.source, "source")
        _text(self.assessment_id, "assessment_id")
        object.__setattr__(
            self,
            "recorded_at",
            _required_timestamp(self.recorded_at, "recorded_at"),
        )
        object.__setattr__(self, "retired_at", _timestamp(self.retired_at, "retired_at"))
        for value, field_name in (
            (self.replacement_id, "replacement_id"),
            (self.replaces_assessment_id, "replaces_assessment_id"),
        ):
            if value is not None:
                _text(value, field_name)
        if self.replacement_id == self.assessment_id:
            raise ValueError("replacement_id cannot reference the same assessment")
        if self.replaces_assessment_id == self.assessment_id:
            raise ValueError("replaces_assessment_id cannot reference the same assessment")

    @property
    def retired(self) -> bool:
        return self.retired_at is not None

    @property
    def is_retired(self) -> bool:
        return self.retired

    @property
    def replaced_assessment_id(self) -> str | None:
        return self.replaces_assessment_id

    @property
    def result(self) -> str | int | float:
        return self.score_result

    @property
    def user_notes(self) -> str:
        return self.notes


@dataclass(frozen=True, slots=True)
class BoardRunwaySnapshot:
    """Point-in-time preparation dimensions and their source freshness."""

    captured_at: datetime = field(default_factory=_now)
    recall_retention: float | None = None
    application_mastery: float | None = None
    timed_application: float | None = None
    blueprint_exposure: float | None = None
    question_volume: int = 0
    anki_due_overdue_load: int = 0
    external_assessment_history: tuple[ExternalAssessment, ...] = ()
    data_freshness: datetime = field(default_factory=_now)
    snapshot_id: str = field(default_factory=lambda: _identifier("snapshot"))

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "captured_at",
            _required_timestamp(self.captured_at, "captured_at"),
        )
        for value, field_name in (
            (self.recall_retention, "recall_retention"),
            (self.application_mastery, "application_mastery"),
            (self.timed_application, "timed_application"),
            (self.blueprint_exposure, "blueprint_exposure"),
        ):
            _ratio(value, field_name)
        _count(self.question_volume, "question_volume")
        _count(self.anki_due_overdue_load, "anki_due_overdue_load")
        history = tuple(self.external_assessment_history)
        if any(not isinstance(item, ExternalAssessment) for item in history):
            raise TypeError(
                "external_assessment_history must contain ExternalAssessment values"
            )
        assessment_ids = tuple(item.assessment_id for item in history)
        if len(assessment_ids) != len(set(assessment_ids)):
            raise ValueError("external_assessment_history must have unique IDs")
        object.__setattr__(self, "external_assessment_history", history)
        object.__setattr__(
            self,
            "data_freshness",
            _required_timestamp(self.data_freshness, "data_freshness"),
        )
        _text(self.snapshot_id, "snapshot_id")

    @property
    def timed_mixed_block_accuracy(self) -> float | None:
        return self.timed_application

    @property
    def anki_due_overdue(self) -> int:
        return self.anki_due_overdue_load

    @property
    def freshness_at(self) -> datetime:
        return self.data_freshness


__all__ = [
    "BoardRunwaySnapshot",
    "BoardTarget",
    "ExternalAssessment",
    "StudyAllocation",
    "StudyPlanDay",
]
