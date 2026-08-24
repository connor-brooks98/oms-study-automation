import json
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from oms_hub.db import Database
from oms_hub.mastery.models import (
    AssistanceLevel,
    ConfidenceRating,
    LearnerEvent,
    LearnerEventRecord,
    LearnerEventType,
)


class MasteryRepository:
    """Append-only storage for immutable learner and assistance events."""

    def __init__(self, database: Database):
        self.database = database

    def append_event(self, event: LearnerEvent) -> LearnerEvent:
        if not isinstance(event, LearnerEvent):
            raise TypeError("event must be a LearnerEvent")
        record = LearnerEventRecord(
            id=event.id,
            client_event_id=event.client_event_id,
            event_type=event.event_type.value,
            objective_ids_json=json.dumps(
                list(event.objective_ids),
                ensure_ascii=False,
                separators=(",", ":"),
            ),
            question_version_id=event.question_version_id,
            correct=event.correct,
            selected_option=event.selected_option,
            difficulty=event.difficulty,
            response_duration_ms=event.response_duration_ms,
            confidence=event.confidence.value,
            assistance_level=event.assistance_level.value,
            occurred_at=event.occurred_at.isoformat(),
            source_snapshot_hash=event.source_snapshot_hash,
        )
        with self.database.session() as session:
            session.add(record)
            try:
                session.flush()
            except IntegrityError:
                session.rollback()
                existing = session.scalar(
                    select(LearnerEventRecord).where(
                        LearnerEventRecord.client_event_id == event.client_event_id
                    )
                )
                if existing is None:
                    raise
                return self._to_event(existing)
            return event

    def events_for_objective(self, objective_id: str) -> list[LearnerEvent]:
        if not isinstance(objective_id, str) or not objective_id.strip():
            raise ValueError("objective_id must be a non-empty string")
        with self.database.session() as session:
            # ponytail: JSON scan is simple for the personal deployment ceiling; add a
            # normalized event-objective link table if event volume needs indexed lookup.
            records = session.scalars(
                select(LearnerEventRecord).order_by(
                    LearnerEventRecord.occurred_at,
                    LearnerEventRecord.id,
                )
            ).all()
            return [
                self._to_event(record)
                for record in records
                if objective_id in record.objective_ids
            ]

    @staticmethod
    def _to_event(record: LearnerEventRecord) -> LearnerEvent:
        return LearnerEvent(
            id=record.id,
            client_event_id=record.client_event_id,
            event_type=LearnerEventType(record.event_type),
            objective_ids=record.objective_ids,
            question_version_id=record.question_version_id,
            correct=record.correct,
            selected_option=record.selected_option,
            difficulty=record.difficulty,
            response_duration_ms=record.response_duration_ms,
            confidence=ConfidenceRating(record.confidence),
            assistance_level=AssistanceLevel(record.assistance_level),
            occurred_at=datetime.fromisoformat(record.occurred_at),
            source_snapshot_hash=record.source_snapshot_hash,
        )
