from dataclasses import FrozenInstanceError

import pytest

from oms_hub.objectives.models import (
    LearningObjective,
    ObjectiveEdge,
    ObjectiveEdgeType,
    ObjectiveEvidenceLink,
    ObjectiveStatus,
)


def _objective(**overrides: object) -> LearningObjective:
    values: dict[str, object] = {
        "objective_id": "obj-hit",
        "display_name": "Recognize HIT",
        "concept_key": "  Recognize HIT!  ",
        "description": "Distinguish heparin-induced thrombocytopenia.",
        "course_id": "heme",
        "exam_id": "exam-2",
        "lecture_ids": ("lecture-13",),
        "status": ObjectiveStatus.APPROVED,
        "source_revision_ids": ("sr-hit",),
        "evidence_ids": ("ev-hit",),
        "blueprint_tags": ("hematology",),
        "created_at": "2026-08-25T12:00:00+00:00",
    }
    values.update(overrides)
    return LearningObjective(**values)  # type: ignore[arg-type]


def test_learning_objective_normalizes_concept_key_and_collections() -> None:
    objective = _objective(
        lecture_ids=[" lecture-13 ", "lecture-13"],
        source_revision_ids=["sr-hit", " sr-hit "],
        evidence_ids=["ev-hit", "ev-hit"],
    )

    assert objective.concept_key == "recognize-hit"
    assert objective.lecture_ids == ("lecture-13",)
    assert objective.source_revision_ids == ("sr-hit",)
    assert objective.evidence_ids == ("ev-hit",)
    assert objective.approved_at == objective.created_at


def test_approved_objective_requires_allowed_evidence_identifiers() -> None:
    with pytest.raises(ValueError, match="evidence"):
        _objective(source_revision_ids=(), evidence_ids=())


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("objective_id", " "),
        ("display_name", ""),
        ("concept_key", "!!!"),
        ("description", " "),
        ("course_id", ""),
    ],
)
def test_objective_rejects_blank_required_fields(field: str, value: str) -> None:
    with pytest.raises(ValueError, match=field):
        _objective(**{field: value})


def test_objective_edge_rejects_self_reference() -> None:
    with pytest.raises(ValueError, match="different objectives"):
        ObjectiveEdge(
            source_objective_id="obj-hit",
            target_objective_id="obj-hit",
            edge_type=ObjectiveEdgeType.PREREQUISITE,
        )


def test_evidence_links_are_immutable() -> None:
    link = ObjectiveEvidenceLink(
        objective_id="obj-hit",
        source_revision_id="sr-hit",
        evidence_id="ev-hit",
        created_at="2026-08-25T12:00:00+00:00",
    )

    with pytest.raises(FrozenInstanceError):
        link.evidence_id = "ev-other"  # type: ignore[misc]

