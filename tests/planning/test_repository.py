from dataclasses import replace
from datetime import UTC, date, datetime, timedelta

import pytest

from oms_hub.planning.models import (
    BoardRunwaySnapshot,
    BoardTarget,
    ExternalAssessment,
    StudyAllocation,
    StudyPlanDay,
)
from oms_hub.planning.repository import PlanningRepository


def _assessment(**overrides: object) -> ExternalAssessment:
    values: dict[str, object] = {
        "assessment_name": "COMSAE Phase 1",
        "date": date(2026, 8, 20),
        "score_result": "510",
        "scale": "200-800",
        "notes": "Timed self-assessment",
        "source": "user",
    }
    values.update(overrides)
    return ExternalAssessment(**values)  # type: ignore[arg-type]


def test_repository_starts_with_default_target_and_allows_user_edits() -> None:
    repository = PlanningRepository()

    assert repository.get_target() == BoardTarget.default()

    edited = BoardTarget(
        exam_family="COMLEX Level 1",
        earliest_date=date(2027, 4, 1),
        latest_date=date(2027, 6, 30),
    )
    assert repository.save_target(edited) == edited
    assert repository.get_target() == edited


def test_repository_retains_target_revisions_and_current_target() -> None:
    repository = PlanningRepository()
    initial = repository.get_target()
    edited = replace(initial, earliest_date=date(2027, 4, 1))

    saved = repository.save_target(edited)

    assert saved.target_id != initial.target_id
    assert repository.get_target() == saved
    assert repository.get_target_revision(initial.target_id) == initial
    assert repository.list_target_revisions() == (initial, saved)


def test_repository_round_trips_plan_days_and_latest_snapshots() -> None:
    repository = PlanningRepository()
    day = StudyPlanDay(
        date=date(2026, 8, 24),
        allocations=(
            StudyAllocation(
                category="current_course_weak_objectives",
                planned_count=20,
                objective_ids=("obj-1",),
                rationale="Current-course gap",
            ),
        ),
    )
    first = repository.save_plan_day(day)
    assert repository.get_plan_day(day.date) == first
    assert repository.list_plan_days() == (first,)

    older = BoardRunwaySnapshot(
        captured_at=datetime(2026, 8, 23, 11, tzinfo=UTC),
        data_freshness=datetime(2026, 8, 23, 10, tzinfo=UTC),
    )
    newer = BoardRunwaySnapshot(
        captured_at=datetime(2026, 8, 24, 11, tzinfo=UTC),
        data_freshness=datetime(2026, 8, 24, 10, tzinfo=UTC),
    )
    repository.save_snapshot(older)
    repository.save_snapshot(newer)

    assert repository.latest_snapshot() == newer


def test_repository_retains_plan_day_revisions_and_current_day() -> None:
    repository = PlanningRepository()
    first = StudyPlanDay(
        date=date(2026, 8, 24),
        allocations=(StudyAllocation(category="cumulative_board_questions", planned_count=10),),
    )
    edited = replace(
        first,
        allocations=(StudyAllocation(category="current_course_weak_objectives", planned_count=20),),
        created_at=first.created_at + timedelta(seconds=1),
    )

    repository.save_plan_day(first)
    saved = repository.save_plan_day(edited)

    assert saved.plan_id != first.plan_id
    assert repository.get_plan_day(first.date) == saved
    assert repository.get_plan_day_revision(first.plan_id) == first
    assert repository.list_plan_day_revisions(first.date) == (first, saved)
    assert repository.list_plan_days() == (saved,)


def test_correcting_external_assessment_creates_replacement_and_retires_old() -> None:
    repository = PlanningRepository()
    original = repository.add_external_assessment(_assessment())

    replacement = repository.correct_external_assessment(
        original.assessment_id,
        score_result="511",
        notes="Corrected score entry",
    )

    assert replacement.assessment_id != original.assessment_id
    assert replacement.score_result == "511"
    assert replacement.notes == "Corrected score entry"
    assert replacement.replaced_assessment_id == original.assessment_id
    assert replacement.retired_at is None

    stored_original = repository.get_external_assessment(original.assessment_id)
    assert stored_original is not None
    assert stored_original.score_result == "510"
    assert stored_original.retired_at is not None
    assert stored_original.replacement_id == replacement.assessment_id
    assert repository.list_external_assessments() == (replacement,)
    assert repository.list_external_assessments(include_retired=True) == (
        stored_original,
        replacement,
    )


def test_correction_rejects_missing_or_already_retired_records() -> None:
    repository = PlanningRepository()
    with pytest.raises(KeyError):
        repository.correct_external_assessment("missing", score_result="1")

    original = repository.add_external_assessment(_assessment())
    repository.correct_external_assessment(original.assessment_id, score_result="511")
    with pytest.raises(ValueError, match="retired"):
        repository.correct_external_assessment(original.assessment_id, score_result="512")
