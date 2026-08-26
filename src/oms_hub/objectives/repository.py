"""Persistence for source-derived objectives and their immutable graph links."""

from __future__ import annotations

import json

from sqlalchemy import text
from sqlalchemy.engine import RowMapping

from oms_hub.db import Database
from oms_hub.knowledge.models import EvidenceUnit, SourceRevisionState
from oms_hub.knowledge.repository import KnowledgeRepository
from oms_hub.objectives.models import (
    LearningObjective,
    ObjectiveEdge,
    ObjectiveEdgeType,
    ObjectiveEvidenceLink,
    ObjectiveStatus,
)


class ObjectiveRepository:
    def __init__(self, database: Database, knowledge: KnowledgeRepository) -> None:
        self.database = database
        self.knowledge = knowledge

    def initialize(self) -> None:
        """Create isolated objective tables; runtime/bootstrap wiring remains Sol-0-owned."""
        with self.database.engine.begin() as connection:
            connection.execute(
                text(
                    """
                    CREATE TABLE IF NOT EXISTS learning_objectives (
                        id TEXT PRIMARY KEY NOT NULL,
                        display_name TEXT NOT NULL,
                        concept_key TEXT NOT NULL,
                        description TEXT NOT NULL,
                        course_id TEXT NOT NULL,
                        exam_id TEXT,
                        lecture_ids_json TEXT NOT NULL,
                        status TEXT NOT NULL,
                        source_revision_ids_json TEXT NOT NULL,
                        evidence_ids_json TEXT NOT NULL,
                        blueprint_tags_json TEXT NOT NULL,
                        created_at TEXT NOT NULL,
                        approved_at TEXT,
                        retired_at TEXT,
                        UNIQUE (course_id, concept_key)
                    )
                    """
                )
            )
            connection.execute(
                text(
                    """
                    CREATE TABLE IF NOT EXISTS objective_evidence (
                        objective_id TEXT NOT NULL,
                        source_revision_id TEXT NOT NULL,
                        evidence_id TEXT NOT NULL,
                        created_at TEXT NOT NULL,
                        PRIMARY KEY (objective_id, evidence_id),
                        FOREIGN KEY (objective_id) REFERENCES learning_objectives (id),
                        FOREIGN KEY (source_revision_id) REFERENCES source_revisions (id),
                        FOREIGN KEY (evidence_id) REFERENCES evidence_units (id)
                    )
                    """
                )
            )
            connection.execute(
                text(
                    """
                    CREATE TABLE IF NOT EXISTS objective_edges (
                        source_objective_id TEXT NOT NULL,
                        target_objective_id TEXT NOT NULL,
                        edge_type TEXT NOT NULL,
                        created_at TEXT NOT NULL,
                        PRIMARY KEY (source_objective_id, target_objective_id, edge_type),
                        FOREIGN KEY (source_objective_id) REFERENCES learning_objectives (id),
                        FOREIGN KEY (target_objective_id) REFERENCES learning_objectives (id)
                    )
                    """
                )
            )

    def create_objective(self, objective: LearningObjective) -> LearningObjective:
        stored = self.get_objective(objective.objective_id)
        if stored is not None:
            if stored != objective:
                raise ValueError("objective_id already refers to a different objective")
            return stored
        links = self._validated_links(objective)
        with self.database.engine.begin() as connection:
            existing = connection.execute(
                text("SELECT * FROM learning_objectives WHERE id = :id"),
                {"id": objective.objective_id},
            ).mappings().first()
            if existing is not None:
                stored = _objective_from_row(existing)
                if stored != objective:
                    raise ValueError("objective_id already refers to a different objective")
                return stored

            concept = connection.execute(
                text(
                    "SELECT id FROM learning_objectives "
                    "WHERE course_id = :course_id AND concept_key = :concept_key"
                ),
                {
                    "course_id": objective.course_id,
                    "concept_key": objective.concept_key,
                },
            ).first()
            if concept is not None:
                raise ValueError("concept key already exists within course")

            connection.execute(
                text(
                    """
                    INSERT INTO learning_objectives (
                        id, display_name, concept_key, description, course_id,
                        exam_id, lecture_ids_json, status,
                        source_revision_ids_json, evidence_ids_json,
                        blueprint_tags_json, created_at, approved_at, retired_at
                    ) VALUES (
                        :id, :display_name, :concept_key, :description, :course_id,
                        :exam_id, :lecture_ids_json, :status,
                        :source_revision_ids_json, :evidence_ids_json,
                        :blueprint_tags_json, :created_at, :approved_at, :retired_at
                    )
                    """
                ),
                _objective_parameters(objective),
            )
            for link in links:
                connection.execute(
                    text(
                        "INSERT INTO objective_evidence "
                        "(objective_id, source_revision_id, evidence_id, created_at) "
                        "VALUES (:objective_id, :source_revision_id, :evidence_id, :created_at)"
                    ),
                    {
                        "objective_id": link.objective_id,
                        "source_revision_id": link.source_revision_id,
                        "evidence_id": link.evidence_id,
                        "created_at": link.created_at,
                    },
                )
        return objective

    def get_objective(self, objective_id: str) -> LearningObjective | None:
        with self.database.engine.connect() as connection:
            row = connection.execute(
                text("SELECT * FROM learning_objectives WHERE id = :id"),
                {"id": objective_id},
            ).mappings().first()
        return None if row is None else _objective_from_row(row)

    def evidence_links(self, objective_id: str) -> tuple[ObjectiveEvidenceLink, ...]:
        with self.database.engine.connect() as connection:
            rows = connection.execute(
                text(
                    "SELECT objective_id, source_revision_id, evidence_id, created_at "
                    "FROM objective_evidence WHERE objective_id = :objective_id "
                    "ORDER BY created_at, evidence_id"
                ),
                {"objective_id": objective_id},
            ).mappings().all()
        return tuple(
            ObjectiveEvidenceLink(
                objective_id=row["objective_id"],
                source_revision_id=row["source_revision_id"],
                evidence_id=row["evidence_id"],
                created_at=row["created_at"],
            )
            for row in rows
        )

    def add_edge(self, edge: ObjectiveEdge) -> ObjectiveEdge:
        with self.database.engine.begin() as connection:
            for objective_id in (edge.source_objective_id, edge.target_objective_id):
                exists = connection.execute(
                    text("SELECT 1 FROM learning_objectives WHERE id = :id"),
                    {"id": objective_id},
                ).first()
                if exists is None:
                    raise KeyError(objective_id)
            existing = connection.execute(
                text(
                    "SELECT created_at FROM objective_edges "
                    "WHERE source_objective_id = :source AND target_objective_id = :target "
                    "AND edge_type = :edge_type"
                ),
                {
                    "source": edge.source_objective_id,
                    "target": edge.target_objective_id,
                    "edge_type": edge.edge_type.value,
                },
            ).first()
            if existing is not None:
                return ObjectiveEdge(
                    source_objective_id=edge.source_objective_id,
                    target_objective_id=edge.target_objective_id,
                    edge_type=edge.edge_type,
                    created_at=existing.created_at,
                )
            connection.execute(
                text(
                    "INSERT INTO objective_edges "
                    "(source_objective_id, target_objective_id, edge_type, created_at) "
                    "VALUES (:source, :target, :edge_type, :created_at)"
                ),
                {
                    "source": edge.source_objective_id,
                    "target": edge.target_objective_id,
                    "edge_type": edge.edge_type.value,
                    "created_at": edge.created_at,
                },
            )
        return edge

    def edges_for_objective(self, objective_id: str) -> tuple[ObjectiveEdge, ...]:
        with self.database.engine.connect() as connection:
            rows = connection.execute(
                text(
                    "SELECT source_objective_id, target_objective_id, edge_type, created_at "
                    "FROM objective_edges WHERE source_objective_id = :id "
                    "OR target_objective_id = :id "
                    "ORDER BY created_at, source_objective_id, target_objective_id, edge_type"
                ),
                {"id": objective_id},
            ).mappings().all()
        return tuple(
            ObjectiveEdge(
                source_objective_id=row["source_objective_id"],
                target_objective_id=row["target_objective_id"],
                edge_type=ObjectiveEdgeType(row["edge_type"]),
                created_at=row["created_at"],
            )
            for row in rows
        )

    def _validated_links(
        self,
        objective: LearningObjective,
    ) -> tuple[ObjectiveEvidenceLink, ...]:
        if not objective.evidence_ids:
            return ()
        units: dict[str, EvidenceUnit] = {}
        used_revisions: set[str] = set()
        for revision_id in objective.source_revision_ids:
            revision = self.knowledge.get_revision(revision_id)
            if revision is None:
                raise KeyError(revision_id)
            if revision.state is not SourceRevisionState.READY:
                raise ValueError("objective evidence must come from a ready source revision")
            for unit in self.knowledge.list_evidence(revision_id):
                if unit.evidence_id in objective.evidence_ids:
                    units[unit.evidence_id] = unit
                    used_revisions.add(revision_id)

        missing = set(objective.evidence_ids) - set(units)
        if missing:
            raise KeyError(f"unknown evidence IDs: {sorted(missing)}")
        if used_revisions != set(objective.source_revision_ids):
            raise ValueError("source revision IDs must match objective evidence")

        links: list[ObjectiveEvidenceLink] = []
        for evidence_id in objective.evidence_ids:
            unit = units[evidence_id]
            if not unit.supports_medical_claims or unit.retired_at is not None:
                raise ValueError("objective evidence authority is not allowed")
            if unit.course_id is not None and unit.course_id != objective.course_id:
                raise ValueError("objective evidence does not match course scope")
            if (
                objective.exam_id is not None
                and unit.exam_id is not None
                and unit.exam_id != objective.exam_id
            ):
                raise ValueError("objective evidence does not match exam scope")
            if (
                objective.lecture_ids
                and unit.lecture_id is not None
                and unit.lecture_id not in objective.lecture_ids
            ):
                raise ValueError("objective evidence does not match lecture scope")
            links.append(
                ObjectiveEvidenceLink(
                    objective_id=objective.objective_id,
                    source_revision_id=unit.source_revision_id,
                    evidence_id=unit.evidence_id,
                    created_at=objective.created_at,
                )
            )
        return tuple(links)


def _json(values: tuple[str, ...]) -> str:
    return json.dumps(values, separators=(",", ":"))


def _objective_parameters(objective: LearningObjective) -> dict[str, object]:
    return {
        "id": objective.objective_id,
        "display_name": objective.display_name,
        "concept_key": objective.concept_key,
        "description": objective.description,
        "course_id": objective.course_id,
        "exam_id": objective.exam_id,
        "lecture_ids_json": _json(objective.lecture_ids),
        "status": objective.status.value,
        "source_revision_ids_json": _json(objective.source_revision_ids),
        "evidence_ids_json": _json(objective.evidence_ids),
        "blueprint_tags_json": _json(objective.blueprint_tags),
        "created_at": objective.created_at,
        "approved_at": objective.approved_at,
        "retired_at": objective.retired_at,
    }


def _objective_from_row(row: RowMapping) -> LearningObjective:
    return LearningObjective(
        objective_id=row["id"],
        display_name=row["display_name"],
        concept_key=row["concept_key"],
        description=row["description"],
        course_id=row["course_id"],
        exam_id=row["exam_id"],
        lecture_ids=tuple(json.loads(row["lecture_ids_json"])),
        status=ObjectiveStatus(row["status"]),
        source_revision_ids=tuple(json.loads(row["source_revision_ids_json"])),
        evidence_ids=tuple(json.loads(row["evidence_ids_json"])),
        blueprint_tags=tuple(json.loads(row["blueprint_tags_json"])),
        created_at=row["created_at"],
        approved_at=row["approved_at"],
        retired_at=row["retired_at"],
    )
