"""Persistence for source-derived objectives and their immutable graph links."""

from __future__ import annotations

import json
from dataclasses import replace

from sqlalchemy import text
from sqlalchemy.engine import Connection, RowMapping

from oms_hub.db import Database
from oms_hub.knowledge.models import EvidenceUnit, SourceRevisionState
from oms_hub.knowledge.policy import SourceScopeError, filter_allowed_evidence
from oms_hub.knowledge.repository import KnowledgeRepository
from oms_hub.objectives.models import (
    LearningObjective,
    ObjectiveEdge,
    ObjectiveEdgeType,
    ObjectiveEvidenceLink,
    ObjectiveEvidenceRemap,
    ObjectiveStatus,
)
from oms_hub.providers.contracts import RetrievalScope, TruthMode


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
                    CREATE TABLE IF NOT EXISTS objective_evidence_remaps (
                        id TEXT PRIMARY KEY NOT NULL,
                        objective_id TEXT NOT NULL,
                        previous_evidence_ids_json TEXT NOT NULL,
                        source_revision_ids_json TEXT NOT NULL,
                        evidence_ids_json TEXT NOT NULL,
                        reason TEXT NOT NULL,
                        created_at TEXT NOT NULL,
                        FOREIGN KEY (objective_id) REFERENCES learning_objectives (id)
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
        if self.database.engine.dialect.name != "sqlite":
            raise RuntimeError("atomic objective creation requires SQLite write serialization")
        with self.database.engine.connect() as connection:
            connection.exec_driver_sql("BEGIN IMMEDIATE")
            try:
                links = self._validated_links(objective)
                result = self._insert_objective(connection, objective, links)
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        return result

    def _insert_objective(
        self,
        connection: Connection,
        objective: LearningObjective,
        links: tuple[ObjectiveEvidenceLink, ...],
    ) -> LearningObjective:
        existing = (
            connection.execute(
                text("SELECT * FROM learning_objectives WHERE id = :id"),
                {"id": objective.objective_id},
            )
            .mappings()
            .first()
        )
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
            row = (
                connection.execute(
                    text("SELECT * FROM learning_objectives WHERE id = :id"),
                    {"id": objective_id},
                )
                .mappings()
                .first()
            )
        return None if row is None else _objective_from_row(row)

    def evidence_links(self, objective_id: str) -> tuple[ObjectiveEvidenceLink, ...]:
        with self.database.engine.connect() as connection:
            rows = (
                connection.execute(
                    text(
                        "SELECT objective_id, source_revision_id, evidence_id, created_at "
                        "FROM objective_evidence WHERE objective_id = :objective_id "
                        "ORDER BY created_at, evidence_id"
                    ),
                    {"objective_id": objective_id},
                )
                .mappings()
                .all()
            )
        return tuple(
            ObjectiveEvidenceLink(
                objective_id=row["objective_id"],
                source_revision_id=row["source_revision_id"],
                evidence_id=row["evidence_id"],
                created_at=row["created_at"],
            )
            for row in rows
        )

    def record_evidence_remap(
        self,
        objective_id: str,
        *,
        remap_id: str,
        source_revision_ids: tuple[str, ...],
        evidence_ids: tuple[str, ...],
        reason: str,
        created_at: str,
    ) -> ObjectiveEvidenceRemap:
        if self.database.engine.dialect.name != "sqlite":
            raise RuntimeError("atomic evidence remapping requires SQLite write serialization")
        with self.database.engine.connect() as connection:
            connection.exec_driver_sql("BEGIN IMMEDIATE")
            try:
                row = (
                    connection.execute(
                        text("SELECT * FROM learning_objectives WHERE id = :id"),
                        {"id": objective_id},
                    )
                    .mappings()
                    .first()
                )
                if row is None:
                    raise KeyError(objective_id)
                objective = _objective_from_row(row)
                existing = (
                    connection.execute(
                        text("SELECT * FROM objective_evidence_remaps WHERE id = :id"),
                        {"id": remap_id},
                    )
                    .mappings()
                    .first()
                )
                if existing is not None:
                    stored = _remap_from_row(existing)
                    retry = ObjectiveEvidenceRemap(
                        remap_id=remap_id,
                        objective_id=objective_id,
                        previous_evidence_ids=stored.previous_evidence_ids,
                        source_revision_ids=source_revision_ids,
                        evidence_ids=evidence_ids,
                        reason=reason,
                        created_at=created_at,
                    )
                    if stored != retry:
                        raise ValueError("remap_id already refers to a different remap")
                    connection.commit()
                    return stored
                previous = (
                    connection.execute(
                        text(
                            "SELECT evidence_ids_json FROM objective_evidence_remaps "
                            "WHERE objective_id = :objective_id "
                            "ORDER BY created_at DESC, id DESC LIMIT 1"
                        ),
                        {"objective_id": objective_id},
                    )
                    .mappings()
                    .first()
                )
                remap = ObjectiveEvidenceRemap(
                    remap_id=remap_id,
                    objective_id=objective_id,
                    previous_evidence_ids=(
                        tuple(json.loads(previous["evidence_ids_json"]))
                        if previous is not None
                        else objective.evidence_ids
                    ),
                    source_revision_ids=source_revision_ids,
                    evidence_ids=evidence_ids,
                    reason=reason,
                    created_at=created_at,
                )

                candidate = replace(
                    objective,
                    source_revision_ids=remap.source_revision_ids,
                    evidence_ids=remap.evidence_ids,
                )
                links = self._validated_links(candidate, created_at=remap.created_at)
                for link in links:
                    existing_link = connection.execute(
                        text(
                            "SELECT source_revision_id FROM objective_evidence "
                            "WHERE objective_id = :objective_id AND evidence_id = :evidence_id"
                        ),
                        {
                            "objective_id": link.objective_id,
                            "evidence_id": link.evidence_id,
                        },
                    ).first()
                    if existing_link is None:
                        connection.execute(
                            text(
                                "INSERT INTO objective_evidence "
                                "(objective_id, source_revision_id, evidence_id, created_at) "
                                "VALUES (:objective_id, :source_revision_id, "
                                ":evidence_id, :created_at)"
                            ),
                            {
                                "objective_id": link.objective_id,
                                "source_revision_id": link.source_revision_id,
                                "evidence_id": link.evidence_id,
                                "created_at": link.created_at,
                            },
                        )
                    elif existing_link.source_revision_id != link.source_revision_id:
                        raise ValueError("evidence link already uses a different source revision")
                connection.execute(
                    text(
                        "INSERT INTO objective_evidence_remaps "
                        "(id, objective_id, previous_evidence_ids_json, "
                        "source_revision_ids_json, evidence_ids_json, reason, created_at) "
                        "VALUES (:id, :objective_id, :previous_evidence_ids_json, "
                        ":source_revision_ids_json, :evidence_ids_json, :reason, :created_at)"
                    ),
                    _remap_parameters(remap),
                )
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        return remap

    def evidence_remaps(self, objective_id: str) -> tuple[ObjectiveEvidenceRemap, ...]:
        with self.database.engine.connect() as connection:
            rows = (
                connection.execute(
                    text(
                        "SELECT * FROM objective_evidence_remaps "
                        "WHERE objective_id = :objective_id ORDER BY created_at, id"
                    ),
                    {"objective_id": objective_id},
                )
                .mappings()
                .all()
            )
        return tuple(_remap_from_row(row) for row in rows)

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
            rows = (
                connection.execute(
                    text(
                        "SELECT source_objective_id, target_objective_id, edge_type, created_at "
                        "FROM objective_edges WHERE source_objective_id = :id "
                        "OR target_objective_id = :id "
                        "ORDER BY created_at, source_objective_id, target_objective_id, edge_type"
                    ),
                    {"id": objective_id},
                )
                .mappings()
                .all()
            )
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
        *,
        created_at: str | None = None,
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

        scope = RetrievalScope(
            course_id=objective.course_id,
            exam_id=objective.exam_id,
            lecture_ids=objective.lecture_ids,
            truth_mode=TruthMode.COURSE_AND_LITERATURE,
            source_revision_ids=objective.source_revision_ids,
        )
        try:
            allowed = {unit.evidence_id for unit in filter_allowed_evidence(scope, units.values())}
        except SourceScopeError as error:
            raise ValueError(str(error)) from error

        links: list[ObjectiveEvidenceLink] = []
        for evidence_id in objective.evidence_ids:
            unit = units[evidence_id]
            if (
                evidence_id not in allowed
                or not unit.supports_medical_claims
                or unit.retired_at is not None
            ):
                raise ValueError("objective evidence authority is not allowed")
            links.append(
                ObjectiveEvidenceLink(
                    objective_id=objective.objective_id,
                    source_revision_id=unit.source_revision_id,
                    evidence_id=unit.evidence_id,
                    created_at=created_at or objective.created_at,
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


def _remap_parameters(remap: ObjectiveEvidenceRemap) -> dict[str, object]:
    return {
        "id": remap.remap_id,
        "objective_id": remap.objective_id,
        "previous_evidence_ids_json": _json(remap.previous_evidence_ids),
        "source_revision_ids_json": _json(remap.source_revision_ids),
        "evidence_ids_json": _json(remap.evidence_ids),
        "reason": remap.reason,
        "created_at": remap.created_at,
    }


def _remap_from_row(row: RowMapping) -> ObjectiveEvidenceRemap:
    return ObjectiveEvidenceRemap(
        remap_id=row["id"],
        objective_id=row["objective_id"],
        previous_evidence_ids=tuple(json.loads(row["previous_evidence_ids_json"])),
        source_revision_ids=tuple(json.loads(row["source_revision_ids_json"])),
        evidence_ids=tuple(json.loads(row["evidence_ids_json"])),
        reason=row["reason"],
        created_at=row["created_at"],
    )
