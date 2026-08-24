from dataclasses import fields
from datetime import UTC, date, datetime

import pytest

from oms_hub.planning.models import (
    BoardRunwaySnapshot,
    BoardTarget,
    ExternalAssessment,
    StudyAllocation,
    StudyPlanDay,
)


def test_default_board_target_is_the_editable_comlex_window() -> None:
    target = BoardTarget.default()

    assert target.exam_family == "COMLEX Level 1"
    assert target.earliest_date == date(2027, 5, 1)
    assert target.latest_date == date(2027, 7, 31)

    edited = BoardTarget(
        exam_family=target.exam_family,
        earliest_date=date(2027, 4, 1),
        latest_date=date(2027, 6, 30),
    )
    assert edited.earliest_date == date(2027, 4, 1)
    assert edited.latest_date == date(2027, 6, 30)


def test_board_runway_has_separate_preparation_dimensions_without_predictions() -> None:
    field_names = {
        field.name
        for model in (
            BoardTarget,
            StudyPlanDay,
            StudyAllocation,
            ExternalAssessment,
            BoardRunwaySnapshot,
        )
        for field in fields(model)
    }
    assert not {
        "pass_probability",
        "predicted_score",
        "guaranteed_ready",
    } & field_names

    snapshot = BoardRunwaySnapshot(
        captured_at=datetime(2026, 8, 23, 12, tzinfo=UTC),
        recall_retention=0.81,
        application_mastery=0.67,
        timed_application=0.59,
        blueprint_exposure=0.42,
        question_volume=128,
        anki_due_overdue_load=34,
        external_assessment_history=(),
        data_freshness=datetime(2026, 8, 23, 11, 30, tzinfo=UTC),
    )

    assert snapshot.recall_retention == 0.81
    assert snapshot.application_mastery == 0.67
    assert snapshot.timed_application == 0.59
    assert snapshot.blueprint_exposure == 0.42
    assert snapshot.question_volume == 128
    assert snapshot.anki_due_overdue_load == 34
    assert snapshot.external_assessment_history == ()
    assert snapshot.data_freshness == datetime(2026, 8, 23, 11, 30, tzinfo=UTC)


def test_external_assessment_is_immutable_and_keeps_required_fields() -> None:
    assessment = ExternalAssessment(
        assessment_name="COMSAE Phase 1",
        date=date(2026, 8, 20),
        score_result="510",
        scale="200-800",
        notes="Timed self-assessment",
        source="user",
    )

    assert assessment.assessment_name == "COMSAE Phase 1"
    assert assessment.date == date(2026, 8, 20)
    assert assessment.score_result == "510"
    assert assessment.scale == "200-800"
    assert assessment.notes == "Timed self-assessment"
    assert assessment.source == "user"
    assert assessment.retired_at is None

    with pytest.raises(AttributeError):
        assessment.score_result = "511"  # type: ignore[misc]


def test_models_reject_invalid_target_window_and_metric_values() -> None:
    with pytest.raises(ValueError, match="earliest_date"):
        BoardTarget(
            exam_family="COMLEX Level 1",
            earliest_date=date(2027, 8, 1),
            latest_date=date(2027, 7, 31),
        )

    with pytest.raises(ValueError, match="recall_retention"):
        BoardRunwaySnapshot(recall_retention=1.1)
