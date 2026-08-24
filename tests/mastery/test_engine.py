from dataclasses import FrozenInstanceError
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from oms_hub.mastery.engine import MasteryEngine, MasterySnapshot
from oms_hub.mastery.models import (
    AssistanceLevel,
    ConfidenceRating,
    LearnerEvent,
    LearnerEventType,
)

NOW = datetime(2026, 8, 23, 12, tzinfo=UTC)


class RecallSnapshot:
    def __init__(self, value: float | None) -> None:
        self.value = value

    def recall_retention(self, objective_id: str) -> float | None:
        assert objective_id == "objective-1"
        return self.value


def _question(
    client_event_id: str,
    *,
    occurred_at: datetime,
    correct: bool,
    objective_ids: tuple[str, ...] = ("objective-1",),
    assistance_level: AssistanceLevel = AssistanceLevel.NONE,
    difficulty: int = 3,
    confidence: ConfidenceRating = ConfidenceRating.NOT_RECORDED,
) -> LearnerEvent:
    return LearnerEvent(
        client_event_id=client_event_id,
        event_type=LearnerEventType.QUESTION_ANSWERED,
        objective_ids=objective_ids,
        question_version_id=f"question-version-{client_event_id}",
        correct=correct,
        selected_option="B",
        difficulty=difficulty,
        confidence=confidence,
        assistance_level=assistance_level,
        occurred_at=occurred_at,
        source_snapshot_hash="snapshot-hash",
    )


def test_empty_history_uses_neutral_prior() -> None:
    engine = MasteryEngine(now=lambda: NOW)

    snapshot = engine.compute("obj-1", [], anki_snapshot=None)

    assert snapshot.application_score == pytest.approx(50.0)
    assert snapshot.evidence_weight == 0.0
    assert snapshot.assistance_dependence is None
    assert snapshot.timed_application_score is None
    assert snapshot.recall_retention is None
    assert snapshot.last_tested_at is None
    assert snapshot.status == "untested"
    assert snapshot.algorithm_version == "mastery-beta-v1"


def test_beta_score_filters_objective_and_decays_evidence() -> None:
    engine = MasteryEngine(now=lambda: NOW)
    events = [
        _question(
            "correct-now",
            occurred_at=NOW,
            correct=True,
            confidence=ConfidenceRating.CONFIDENT,
        ),
        _question(
            "incorrect-60-days",
            occurred_at=NOW - timedelta(days=60),
            correct=False,
            assistance_level=AssistanceLevel.CONCEPT_HINT,
            difficulty=4,
        ),
        _question(
            "other-objective",
            occurred_at=NOW,
            correct=False,
            objective_ids=("objective-2",),
        ),
        LearnerEvent(
            client_event_id="source-opened",
            event_type=LearnerEventType.SOURCE_OPENED,
            objective_ids=("objective-1",),
            occurred_at=NOW,
        ),
    ]

    snapshot = engine.compute(" objective-1 ", events, anki_snapshot=None)

    expected_correct = 1.10
    expected_incorrect = 0.70 * 1.20 * 0.50
    expected_alpha = 2.0 + expected_correct
    expected_beta = 2.0 + expected_incorrect
    assert snapshot.application_score == pytest.approx(
        100.0 * expected_alpha / (expected_alpha + expected_beta)
    )
    assert snapshot.evidence_weight == pytest.approx(expected_correct + expected_incorrect)
    assert snapshot.last_tested_at == NOW
    assert snapshot.status == "tested"


def test_assistance_dependence_is_recency_weighted_and_separate() -> None:
    engine = MasteryEngine(now=lambda: NOW)
    snapshot = engine.compute(
        "objective-1",
        [
            _question(
                "unaided",
                occurred_at=NOW,
                correct=False,
                assistance_level=AssistanceLevel.NONE,
                difficulty=5,
                confidence=ConfidenceRating.CONFIDENT,
            ),
            _question(
                "revealed",
                occurred_at=NOW - timedelta(days=60),
                correct=True,
                assistance_level=AssistanceLevel.ANSWER_REVEALED,
                difficulty=1,
                confidence=ConfidenceRating.GUESSED,
            ),
        ],
        anki_snapshot=None,
    )

    expected = 100.0 * (0.0 * 1.0 + 0.90 * 0.5) / (1.0 + 0.5)
    assert snapshot.assistance_dependence == pytest.approx(expected)


def test_recall_retention_uses_validated_objective_scoped_adapter() -> None:
    engine = MasteryEngine(now=lambda: NOW)

    assert engine.compute("objective-1", [], RecallSnapshot(0.725)).recall_retention == 0.725
    assert engine.compute("objective-1", [], RecallSnapshot(None)).recall_retention is None

    for invalid in (-0.1, 100.1, float("nan"), float("inf"), True):
        with pytest.raises(ValueError):
            engine.compute("objective-1", [], RecallSnapshot(invalid))


def test_future_event_is_clamped_to_current_weight() -> None:
    event = _question("future", occurred_at=NOW + timedelta(days=5), correct=True)
    snapshot = MasteryEngine(now=lambda: NOW).compute("objective-1", [event], None)

    assert snapshot.evidence_weight == pytest.approx(1.0)
    assert snapshot.last_tested_at == NOW + timedelta(days=5)


def test_snapshot_is_frozen_and_serialization_is_deterministic() -> None:
    snapshot = MasteryEngine(now=lambda: NOW).compute("objective-1", [], None)

    assert isinstance(snapshot, MasterySnapshot)
    with pytest.raises(FrozenInstanceError):
        snapshot.status = "tested"  # type: ignore[misc]
    assert snapshot.serialize() == snapshot.serialize()
    assert snapshot.to_json().encode("utf-8") == snapshot.serialize()


@pytest.mark.parametrize("objective_id", ["", "   ", None, 42])
def test_compute_rejects_invalid_objective_id(objective_id: object) -> None:
    with pytest.raises(ValueError):
        MasteryEngine(now=lambda: NOW).compute(objective_id, [], None)  # type: ignore[arg-type]


def test_engine_rejects_non_utc_now() -> None:
    with pytest.raises(ValueError, match="now"):
        MasteryEngine(now=lambda: datetime(2026, 8, 23, 12)).compute("objective-1", [], None)


def test_engine_rejects_non_utc_as_of() -> None:
    with pytest.raises(ValueError, match="as_of"):
        MasteryEngine(now=lambda: NOW).compute(
            "objective-1",
            [],
            None,
            as_of=datetime(2026, 8, 23, 12),
        )


def test_task_modules_do_not_import_anki_or_copy_sol7_types() -> None:
    root = Path(__file__).parents[2] / "src" / "oms_hub" / "mastery"
    source = "\n".join(path.read_text() for path in root.glob("*.py"))

    assert "oms_hub.anki" not in source
    assert "learning_contracts" not in source
    assert "AnkiLearningSnapshot" not in source
