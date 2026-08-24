from __future__ import annotations

from collections.abc import Callable, Iterable
from datetime import datetime

from oms_hub.mastery.engine import MasteryEngine, MasterySnapshot, ObjectiveRecallRetention
from oms_hub.mastery.models import LearnerEvent


class MasteryService:
    """Stateless mastery recomputation over caller-supplied complete history."""

    def __init__(
        self,
        *,
        engine: MasteryEngine | None = None,
        now: Callable[[], datetime] | datetime | None = None,
    ) -> None:
        if engine is not None and now is not None:
            raise ValueError("provide engine or now, not both")
        self.engine = engine or MasteryEngine(now=now)

    def recompute_on_event(
        self,
        objective_id: str,
        prior_events: Iterable[LearnerEvent],
        new_event: LearnerEvent,
        anki_snapshot: ObjectiveRecallRetention | None = None,
        *,
        as_of: datetime,
    ) -> MasterySnapshot:
        if not isinstance(new_event, LearnerEvent):
            raise TypeError("new_event must be a LearnerEvent")
        return self.engine.compute(
            objective_id,
            (*prior_events, new_event),
            anki_snapshot,
            as_of=as_of,
        )

    def rebuild(
        self,
        objective_id: str,
        events: Iterable[LearnerEvent],
        anki_snapshot: ObjectiveRecallRetention | None = None,
        *,
        as_of: datetime,
    ) -> MasterySnapshot:
        return self.engine.compute(objective_id, events, anki_snapshot, as_of=as_of)
