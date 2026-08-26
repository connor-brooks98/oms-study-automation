"""Immutable source-derived objective graph contracts."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum

from oms_hub.models import utc_now


class ObjectiveStatus(StrEnum):
    PROPOSED = "proposed"
    APPROVED = "approved"
    RETIRED = "retired"


class ObjectiveEdgeType(StrEnum):
    PREREQUISITE = "prerequisite"
    PART_OF = "part_of"
    CONTRASTS_WITH = "contrasts_with"
    COMMONLY_CONFUSED_WITH = "commonly_confused_with"


def _required(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must not be blank")
    return value.strip()


def _identifiers(values: object, field_name: str) -> tuple[str, ...]:
    if not isinstance(values, (tuple, list)):
        raise ValueError(f"{field_name} must be a sequence of strings")
    normalized: list[str] = []
    for value in values:
        identifier = _required(value, field_name)
        if identifier not in normalized:
            normalized.append(identifier)
    return tuple(normalized)


def _concept_key(value: object) -> str:
    raw = _required(value, "concept_key").casefold()
    normalized = re.sub(r"[^a-z0-9]+", "-", raw).strip("-")
    if not normalized:
        raise ValueError("concept_key must contain letters or numbers")
    return normalized


def _utc_timestamp(value: object, field_name: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field_name} must be a timezone-aware UTC timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError(f"{field_name} must be a timezone-aware UTC timestamp") from error
    if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
        raise ValueError(f"{field_name} must be a timezone-aware UTC timestamp")
    return parsed.isoformat()


@dataclass(frozen=True, slots=True)
class LearningObjective:
    objective_id: str
    display_name: str
    concept_key: str
    description: str
    course_id: str
    exam_id: str | None = None
    lecture_ids: tuple[str, ...] = ()
    status: ObjectiveStatus = ObjectiveStatus.PROPOSED
    source_revision_ids: tuple[str, ...] = ()
    evidence_ids: tuple[str, ...] = ()
    blueprint_tags: tuple[str, ...] = ()
    created_at: str = field(default_factory=utc_now)
    approved_at: str | None = None
    retired_at: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "objective_id", _required(self.objective_id, "objective_id"))
        object.__setattr__(self, "display_name", _required(self.display_name, "display_name"))
        object.__setattr__(self, "concept_key", _concept_key(self.concept_key))
        object.__setattr__(self, "description", _required(self.description, "description"))
        object.__setattr__(self, "course_id", _required(self.course_id, "course_id"))
        if self.exam_id is not None:
            object.__setattr__(self, "exam_id", _required(self.exam_id, "exam_id"))
        for field_name in (
            "lecture_ids",
            "source_revision_ids",
            "evidence_ids",
            "blueprint_tags",
        ):
            object.__setattr__(
                self,
                field_name,
                _identifiers(getattr(self, field_name), field_name),
            )
        object.__setattr__(self, "status", ObjectiveStatus(self.status))
        object.__setattr__(self, "created_at", _utc_timestamp(self.created_at, "created_at"))

        approved_at = self.approved_at
        retired_at = self.retired_at
        if self.status in (ObjectiveStatus.APPROVED, ObjectiveStatus.RETIRED):
            if not self.evidence_ids or not self.source_revision_ids:
                raise ValueError("approved or retired objectives require evidence")
            approved_at = approved_at or self.created_at
        if self.status is ObjectiveStatus.RETIRED:
            retired_at = retired_at or self.created_at
        if self.status is not ObjectiveStatus.RETIRED and retired_at is not None:
            raise ValueError("retired_at requires retired status")
        if self.status is ObjectiveStatus.PROPOSED and approved_at:
            raise ValueError("proposed objectives cannot have approval or retirement timestamps")
        if approved_at is not None:
            approved_at = _utc_timestamp(approved_at, "approved_at")
        if retired_at is not None:
            retired_at = _utc_timestamp(retired_at, "retired_at")
        if approved_at is not None and approved_at < self.created_at:
            raise ValueError("approved_at cannot precede created_at")
        if retired_at is not None and approved_at is not None and retired_at < approved_at:
            raise ValueError("retired_at cannot precede approved_at")
        object.__setattr__(self, "approved_at", approved_at)
        object.__setattr__(self, "retired_at", retired_at)


@dataclass(frozen=True, slots=True)
class ObjectiveEdge:
    source_objective_id: str
    target_objective_id: str
    edge_type: ObjectiveEdgeType
    created_at: str = field(default_factory=utc_now)

    def __post_init__(self) -> None:
        source = _required(self.source_objective_id, "source_objective_id")
        target = _required(self.target_objective_id, "target_objective_id")
        if source == target:
            raise ValueError("objective edges require different objectives")
        object.__setattr__(self, "source_objective_id", source)
        object.__setattr__(self, "target_objective_id", target)
        object.__setattr__(self, "edge_type", ObjectiveEdgeType(self.edge_type))
        object.__setattr__(self, "created_at", _utc_timestamp(self.created_at, "created_at"))


@dataclass(frozen=True, slots=True)
class ObjectiveEvidenceLink:
    objective_id: str
    source_revision_id: str
    evidence_id: str
    created_at: str = field(default_factory=utc_now)

    def __post_init__(self) -> None:
        for field_name in ("objective_id", "source_revision_id", "evidence_id"):
            object.__setattr__(self, field_name, _required(getattr(self, field_name), field_name))
        object.__setattr__(self, "created_at", _utc_timestamp(self.created_at, "created_at"))


@dataclass(frozen=True, slots=True)
class ObjectiveEvidenceRemap:
    remap_id: str
    objective_id: str
    previous_evidence_ids: tuple[str, ...]
    source_revision_ids: tuple[str, ...]
    evidence_ids: tuple[str, ...]
    reason: str
    created_at: str = field(default_factory=utc_now)

    def __post_init__(self) -> None:
        for field_name in ("remap_id", "objective_id", "reason"):
            object.__setattr__(self, field_name, _required(getattr(self, field_name), field_name))
        for field_name in (
            "previous_evidence_ids",
            "source_revision_ids",
            "evidence_ids",
        ):
            values = _identifiers(getattr(self, field_name), field_name)
            if not values:
                raise ValueError(f"{field_name} must not be empty")
            object.__setattr__(self, field_name, values)
        object.__setattr__(self, "created_at", _utc_timestamp(self.created_at, "created_at"))
