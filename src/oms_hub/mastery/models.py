import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import ClassVar
from uuid import uuid4

from sqlalchemy import Boolean, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from oms_hub.models import Base, utc_now


class LearnerEventType(StrEnum):
    QUESTION_ANSWERED = "question_answered"
    HINT_REQUESTED = "hint_requested"
    SOURCE_OPENED = "source_opened"
    ASK_QUESTION_SUBMITTED = "ask_question_submitted"
    ASK_ANSWER_COMPLETED = "ask_answer_completed"
    ANSWER_REVEALED = "answer_revealed"
    QUESTION_RETRIED = "question_retried"
    ANKI_SNAPSHOT_OBSERVED = "anki_snapshot_observed"
    MANUAL_MASTERY_RESET = "manual_mastery_reset"


class AssistanceLevel(StrEnum):
    NONE = "none"
    CONCEPT_HINT = "concept_hint"
    SOURCE_EXCERPT = "source_excerpt"
    FULL_EXPLANATION = "full_explanation"
    ANSWER_REVEALED = "answer_revealed"


class ConfidenceRating(StrEnum):
    CONFIDENT = "confident"
    UNSURE = "unsure"
    GUESSED = "guessed"
    NOT_RECORDED = "not_recorded"


def _enum_value(enum_type: type[StrEnum], value: object, field_name: str) -> StrEnum:
    if not isinstance(value, str):
        raise ValueError(f"{field_name} must be a supported string value")
    try:
        return enum_type(value)
    except ValueError as error:
        raise ValueError(f"{field_name} is not supported: {value}") from error


def _required_identifier(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string")
    return value.strip()


def _occurred_at(value: object) -> datetime:
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value)
        except ValueError as error:
            raise ValueError("occurred_at must be an ISO-8601 timestamp") from error
    if not isinstance(value, datetime):
        raise ValueError("occurred_at must be a datetime or ISO-8601 timestamp")
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


@dataclass(frozen=True, slots=True)
class LearnerEvent:
    """Immutable learner signal accepted by the append-only mastery store."""

    client_event_id: str
    event_type: LearnerEventType
    objective_ids: tuple[str, ...] = ()
    question_version_id: str | None = None
    correct: bool | None = None
    selected_option: str | None = None
    difficulty: int | None = None
    response_duration_ms: int | None = None
    confidence: ConfidenceRating = ConfidenceRating.NOT_RECORDED
    assistance_level: AssistanceLevel = AssistanceLevel.NONE
    occurred_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    source_snapshot_hash: str | None = None
    id: str = field(default_factory=lambda: str(uuid4()))

    _MAX_DIFFICULTY: ClassVar[int] = 5

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "id",
            _required_identifier(self.id, "id"),
        )
        object.__setattr__(
            self,
            "client_event_id",
            _required_identifier(self.client_event_id, "client_event_id"),
        )
        object.__setattr__(
            self,
            "event_type",
            _enum_value(LearnerEventType, self.event_type, "event_type"),
        )
        object.__setattr__(
            self,
            "confidence",
            _enum_value(ConfidenceRating, self.confidence, "confidence"),
        )
        object.__setattr__(
            self,
            "assistance_level",
            _enum_value(AssistanceLevel, self.assistance_level, "assistance_level"),
        )

        if not isinstance(self.objective_ids, (tuple, list)):
            raise ValueError("objective_ids must be a sequence of strings")
        normalized_objectives: list[str] = []
        for objective_id in self.objective_ids:
            normalized = _required_identifier(objective_id, "objective_id")
            if normalized not in normalized_objectives:
                normalized_objectives.append(normalized)
        object.__setattr__(self, "objective_ids", tuple(normalized_objectives))

        for field_name in ("question_version_id", "selected_option", "source_snapshot_hash"):
            value = getattr(self, field_name)
            if value is not None:
                object.__setattr__(
                    self,
                    field_name,
                    _required_identifier(value, field_name),
                )

        if self.event_type is LearnerEventType.QUESTION_ANSWERED:
            if self.question_version_id is None:
                raise ValueError("question_answered requires question_version_id")
            if not self.objective_ids:
                raise ValueError("question_answered requires objective_ids")
            if self.source_snapshot_hash is None:
                raise ValueError("question_answered requires source_snapshot_hash")
            if self.correct is None:
                raise ValueError("question_answered requires correct")
            if self.selected_option is None:
                raise ValueError("question_answered requires selected_option")

        if self.correct is not None and not isinstance(self.correct, bool):
            raise ValueError("correct must be a boolean or None")
        if self.difficulty is not None and (
            isinstance(self.difficulty, bool)
            or not isinstance(self.difficulty, int)
            or not 1 <= self.difficulty <= self._MAX_DIFFICULTY
        ):
            raise ValueError("difficulty must be an integer from 1 through 5")
        if self.response_duration_ms is not None and (
            isinstance(self.response_duration_ms, bool)
            or not isinstance(self.response_duration_ms, int)
            or self.response_duration_ms < 0
        ):
            raise ValueError("response_duration_ms must be a non-negative integer")
        object.__setattr__(self, "occurred_at", _occurred_at(self.occurred_at))


class LearnerEventRecord(Base):
    """Database row for :class:`LearnerEvent`; rows are never updated in-place."""

    __tablename__ = "learner_events"
    __table_args__ = (
        UniqueConstraint("client_event_id", name="uq_learner_events_client_event_id"),
        Index("ix_learner_events_occurred_at", "occurred_at"),
    )

    id: Mapped[str] = mapped_column(String(200), primary_key=True)
    client_event_id: Mapped[str] = mapped_column(String(200), nullable=False)
    event_type: Mapped[str] = mapped_column(String(40), nullable=False)
    objective_ids_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    question_version_id: Mapped[str | None] = mapped_column(String(200), nullable=True)
    correct: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    selected_option: Mapped[str | None] = mapped_column(String(200), nullable=True)
    difficulty: Mapped[int | None] = mapped_column(Integer, nullable=True)
    response_duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    confidence: Mapped[str] = mapped_column(String(20), nullable=False)
    assistance_level: Mapped[str] = mapped_column(String(30), nullable=False)
    occurred_at: Mapped[str] = mapped_column(String(40), nullable=False)
    source_snapshot_hash: Mapped[str | None] = mapped_column(String(200), nullable=True)
    created_at: Mapped[str] = mapped_column(String(40), default=utc_now, nullable=False)

    @property
    def objective_ids(self) -> tuple[str, ...]:
        values = json.loads(self.objective_ids_json)
        if not isinstance(values, list) or not all(isinstance(item, str) for item in values):
            raise ValueError("stored learner event objective IDs are invalid")
        return tuple(values)
