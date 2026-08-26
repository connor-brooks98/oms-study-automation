"""Isolated planning repository contract pending central schema wiring."""

from dataclasses import replace
from datetime import UTC, date, datetime
from typing import cast
from uuid import uuid4

from oms_hub.planning.models import (
    BoardRunwaySnapshot,
    BoardTarget,
    ExternalAssessment,
    StudyPlanDay,
)


class PlanningRepository:
    """Deterministic repository for planning contracts.

    This implementation is intentionally isolated and in-memory. Sol-0 must
    wire these immutable records into the existing database conventions before
    the feature is enabled in the application.
    """

    def __init__(self, target: BoardTarget | None = None) -> None:
        initial_target = target or BoardTarget.default()
        self._targets: dict[str, BoardTarget] = {initial_target.target_id: initial_target}
        self._current_target_id = initial_target.target_id
        self._plan_days: dict[str, StudyPlanDay] = {}
        self._snapshots: dict[str, BoardRunwaySnapshot] = {}
        self._assessments: dict[str, ExternalAssessment] = {}

    def get_target(self) -> BoardTarget:
        return self._targets[self._current_target_id]

    @property
    def target(self) -> BoardTarget:
        return self.get_target()

    def save_target(self, target: BoardTarget) -> BoardTarget:
        existing = self._targets.get(target.target_id)
        if existing is not None:
            if existing == target:
                self._current_target_id = target.target_id
                return existing
            target = replace(target, target_id=f"target_{uuid4()}")
        self._targets[target.target_id] = target
        self._current_target_id = target.target_id
        return target

    def get_target_revision(self, target_id: str) -> BoardTarget | None:
        return self._targets.get(target_id)

    def list_target_revisions(self) -> tuple[BoardTarget, ...]:
        return tuple(self._targets.values())

    def get_board_target(self) -> BoardTarget:
        return self.get_target()

    def save_board_target(self, target: BoardTarget) -> BoardTarget:
        return self.save_target(target)

    def save_plan_day(self, plan: StudyPlanDay) -> StudyPlanDay:
        existing = self._plan_days.get(plan.plan_id)
        if existing is not None:
            if existing == plan:
                return existing
            plan = replace(plan, plan_id=f"plan_{uuid4()}")
        self._plan_days[plan.plan_id] = plan
        return plan

    def get_plan_day(self, plan_date: date) -> StudyPlanDay | None:
        revisions = self.list_plan_day_revisions(plan_date)
        return revisions[-1] if revisions else None

    def get_plan_day_revision(self, plan_id: str) -> StudyPlanDay | None:
        return self._plan_days.get(plan_id)

    def list_plan_day_revisions(self, plan_date: date) -> tuple[StudyPlanDay, ...]:
        return tuple(plan for plan in self._plan_days.values() if plan.date == plan_date)

    def list_plan_days(
        self,
        *,
        start: date | None = None,
        end: date | None = None,
    ) -> tuple[StudyPlanDay, ...]:
        if start is not None and end is not None and start > end:
            raise ValueError("start must not be after end")
        latest_by_date: dict[date, StudyPlanDay] = {}
        for plan in self._plan_days.values():
            latest_by_date[plan.date] = plan
        return tuple(
            plan
            for plan in sorted(latest_by_date.values(), key=lambda item: item.date)
            if (start is None or plan.date >= start) and (end is None or plan.date <= end)
        )

    def save_snapshot(self, snapshot: BoardRunwaySnapshot) -> BoardRunwaySnapshot:
        existing = self._snapshots.get(snapshot.snapshot_id)
        if existing is not None:
            if existing == snapshot:
                return existing
            raise ValueError("snapshot_id already exists with different content")
        self._snapshots[snapshot.snapshot_id] = snapshot
        return snapshot

    def get_snapshot(self, snapshot_id: str) -> BoardRunwaySnapshot | None:
        return self._snapshots.get(snapshot_id)

    def latest_snapshot(self) -> BoardRunwaySnapshot | None:
        return max(
            self._snapshots.values(),
            key=lambda item: (item.captured_at, item.snapshot_id),
            default=None,
        )

    def add_external_assessment(self, assessment: ExternalAssessment) -> ExternalAssessment:
        if assessment.assessment_id in self._assessments:
            raise ValueError("assessment_id already exists")
        self._assessments[assessment.assessment_id] = assessment
        return assessment

    def save_external_assessment(self, assessment: ExternalAssessment) -> ExternalAssessment:
        return self.add_external_assessment(assessment)

    def get_external_assessment(self, assessment_id: str) -> ExternalAssessment | None:
        return self._assessments.get(assessment_id)

    def list_external_assessments(
        self,
        *,
        include_retired: bool = False,
    ) -> tuple[ExternalAssessment, ...]:
        assessments = (
            assessment
            for assessment in self._assessments.values()
            if include_retired or not assessment.retired
        )
        return tuple(
            sorted(
                assessments,
                key=lambda item: (item.date, item.recorded_at, item.assessment_id),
            )
        )

    def correct_external_assessment(
        self,
        assessment_id: str,
        replacement: ExternalAssessment | None = None,
        **changes: object,
    ) -> ExternalAssessment:
        original = self._assessments.get(assessment_id)
        if original is None:
            raise KeyError(assessment_id)
        if original.retired:
            raise ValueError("cannot correct a retired assessment")
        if replacement is not None and changes:
            raise ValueError("provide replacement or field changes, not both")
        allowed = {
            "assessment_name",
            "date",
            "score_result",
            "scale",
            "notes",
            "source",
        }
        unknown = set(changes) - allowed
        if unknown:
            raise TypeError(f"unknown assessment fields: {sorted(unknown)}")
        base = replacement or original
        if changes:
            values = {
                field_name: getattr(original, field_name)
                for field_name in allowed
            }
            values.update(changes)
            base = ExternalAssessment(
                assessment_name=cast(str, values["assessment_name"]),
                date=cast(date, values["date"]),
                score_result=cast(str | int | float, values["score_result"]),
                scale=cast(str, values["scale"]),
                notes=cast(str, values["notes"]),
                source=cast(str, values["source"]),
            )
        now = datetime.now(UTC)
        replacement_record = replace(
            base,
            assessment_id=f"assessment_{uuid4()}",
            recorded_at=now,
            retired_at=None,
            replacement_id=None,
            replaces_assessment_id=original.assessment_id,
        )
        self._assessments[original.assessment_id] = replace(
            original,
            retired_at=now,
            replacement_id=replacement_record.assessment_id,
        )
        self._assessments[replacement_record.assessment_id] = replacement_record
        return replacement_record

    def replace_external_assessment(
        self,
        assessment_id: str,
        replacement: ExternalAssessment | None = None,
        **changes: object,
    ) -> ExternalAssessment:
        return self.correct_external_assessment(assessment_id, replacement, **changes)


__all__ = ["PlanningRepository"]
