"""Durable human-review workflow for extracted objective proposals."""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, replace
from enum import StrEnum

from sqlalchemy import text
from sqlalchemy.engine import Connection, RowMapping

from oms_hub.models import utc_now
from oms_hub.objectives.extraction import (
    ObjectiveExtractor,
    ProposedObjective,
    SuggestedObjectiveLink,
)
from oms_hub.objectives.models import LearningObjective, ObjectiveEdgeType, ObjectiveStatus
from oms_hub.objectives.repository import ObjectiveRepository


class ObjectiveProposalDisposition(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    MERGED = "merged"
    RETIRED = "retired"


@dataclass(frozen=True, slots=True)
class ObjectiveProposalRecord:
    proposal: ProposedObjective
    disposition: ObjectiveProposalDisposition
    created_at: str
    updated_at: str
    approved_objective_id: str | None = None
    merged_into_id: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "disposition",
            ObjectiveProposalDisposition(self.disposition),
        )


class ObjectiveService:
    def __init__(
        self,
        objectives: ObjectiveRepository,
        extractor: ObjectiveExtractor | None = None,
        *,
        clock: Callable[[], str] = utc_now,
    ) -> None:
        self.objectives = objectives
        self.extractor = extractor
        self.clock = clock

    def initialize(self) -> None:
        """Create the isolated proposal table; central migrations remain HELD."""
        with self.objectives.database.engine.begin() as connection:
            connection.execute(
                text(
                    """
                    CREATE TABLE IF NOT EXISTS objective_proposals (
                        id TEXT PRIMARY KEY NOT NULL,
                        payload_json TEXT NOT NULL,
                        disposition TEXT NOT NULL,
                        approved_objective_id TEXT,
                        merged_into_id TEXT,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        FOREIGN KEY (approved_objective_id)
                            REFERENCES learning_objectives (id),
                        FOREIGN KEY (merged_into_id)
                            REFERENCES objective_proposals (id)
                    )
                    """
                )
            )

    def extract(
        self,
        source_revision_ids: tuple[str, ...],
    ) -> tuple[ObjectiveProposalRecord, ...]:
        if self.extractor is None:
            raise ValueError("objective extractor is not configured")
        return self.capture(self.extractor.extract(source_revision_ids))

    def capture(
        self,
        proposals: tuple[ProposedObjective, ...],
    ) -> tuple[ObjectiveProposalRecord, ...]:
        now = self.clock()
        records: list[ObjectiveProposalRecord] = []
        with self._write_connection() as connection:
            for proposal in proposals:
                stored = _select_record(connection, proposal.proposal_id)
                if stored is None:
                    record = ObjectiveProposalRecord(
                        proposal,
                        ObjectiveProposalDisposition.PENDING,
                        now,
                        now,
                    )
                    connection.execute(
                        text(
                            "INSERT INTO objective_proposals "
                            "(id, payload_json, disposition, approved_objective_id, "
                            "merged_into_id, created_at, updated_at) "
                            "VALUES (:id, :payload, :disposition, NULL, NULL, "
                            ":created_at, :updated_at)"
                        ),
                        _record_parameters(record),
                    )
                elif stored.disposition is ObjectiveProposalDisposition.PENDING:
                    combined = _combine(stored.proposal, proposal)
                    record = replace(stored, proposal=combined, updated_at=now)
                    connection.execute(
                        text(
                            "UPDATE objective_proposals SET payload_json = :payload, "
                            "updated_at = :updated_at WHERE id = :id"
                        ),
                        _record_parameters(record),
                    )
                else:
                    record = stored
                records.append(record)
        return tuple(records)

    def list_proposals(self) -> tuple[ObjectiveProposalRecord, ...]:
        with self.objectives.database.engine.connect() as connection:
            rows = (
                connection.execute(
                    text("SELECT * FROM objective_proposals ORDER BY created_at, id")
                )
                .mappings()
                .all()
            )
        return tuple(_record_from_row(row) for row in rows)

    def get_proposal(self, proposal_id: str) -> ObjectiveProposalRecord:
        with self.objectives.database.engine.connect() as connection:
            record = _select_record(connection, proposal_id)
        if record is None:
            raise KeyError(proposal_id)
        return record

    def approve(self, proposal_id: str) -> ObjectiveProposalRecord:
        with self._write_connection() as connection:
            record = _select_record(connection, proposal_id)
            if record is None:
                raise KeyError(proposal_id)
            if record.disposition is ObjectiveProposalDisposition.APPROVED:
                return record
            if record.disposition is not ObjectiveProposalDisposition.PENDING:
                raise ValueError("only pending proposals can be approved")
            proposal = record.proposal
            objective = LearningObjective(
                objective_id=proposal.proposal_id,
                display_name=f"{proposal.observable_verb.title()} {proposal.concept}",
                concept_key=f"{proposal.observable_verb}-{proposal.concept}",
                description=proposal.description,
                course_id=proposal.course_id,
                exam_id=proposal.exam_id,
                lecture_ids=proposal.lecture_ids,
                status=ObjectiveStatus.APPROVED,
                source_revision_ids=proposal.source_revision_ids,
                evidence_ids=proposal.evidence_ids,
                created_at=record.created_at,
                approved_at=self.clock(),
            )
            self.objectives._create_objective_in_transaction(connection, objective)
            connection.execute(
                text(
                    "UPDATE objective_proposals SET disposition = :disposition, "
                    "approved_objective_id = :approved_objective_id, "
                    "updated_at = :updated_at WHERE id = :id"
                ),
                {
                    "id": proposal_id,
                    "disposition": ObjectiveProposalDisposition.APPROVED.value,
                    "approved_objective_id": objective.objective_id,
                    "updated_at": self.clock(),
                },
            )
            approved = _select_record(connection, proposal_id)
            assert approved is not None
        return approved

    def merge(self, proposal_id: str, target_id: str) -> ObjectiveProposalRecord:
        if proposal_id == target_id:
            raise ValueError("a proposal cannot merge into itself")
        now = self.clock()
        with self._write_connection() as connection:
            source = _select_record(connection, proposal_id)
            target = _select_record(connection, target_id)
            if source is None:
                raise KeyError(proposal_id)
            if target is None:
                raise KeyError(target_id)
            if (
                source.disposition is ObjectiveProposalDisposition.MERGED
                and source.merged_into_id == target_id
            ):
                return target
            if source.disposition is not ObjectiveProposalDisposition.PENDING or (
                target.disposition is not ObjectiveProposalDisposition.PENDING
            ):
                raise ValueError("only pending proposals can be merged")
            if _scope(source.proposal) != _scope(target.proposal):
                raise ValueError("merged proposals must have the same scope")
            merged = replace(
                target,
                proposal=_combine(target.proposal, source.proposal),
                updated_at=now,
            )
            connection.execute(
                text(
                    "UPDATE objective_proposals SET payload_json = :payload, "
                    "updated_at = :updated_at WHERE id = :id"
                ),
                _record_parameters(merged),
            )
            connection.execute(
                text(
                    "UPDATE objective_proposals SET disposition = :disposition, "
                    "merged_into_id = :merged_into_id, updated_at = :updated_at "
                    "WHERE id = :id"
                ),
                {
                    "id": proposal_id,
                    "disposition": ObjectiveProposalDisposition.MERGED.value,
                    "merged_into_id": target_id,
                    "updated_at": now,
                },
            )
        return merged

    def retire(self, proposal_id: str) -> ObjectiveProposalRecord:
        record = self.get_proposal(proposal_id)
        if record.disposition is ObjectiveProposalDisposition.RETIRED:
            return record
        if record.disposition is ObjectiveProposalDisposition.MERGED:
            raise ValueError("merged proposals cannot be retired independently")
        if record.disposition is ObjectiveProposalDisposition.APPROVED:
            assert record.approved_objective_id is not None
            self.objectives.retire_objective(
                record.approved_objective_id,
                retired_at=self.clock(),
            )
            expected = ObjectiveProposalDisposition.APPROVED
        else:
            expected = ObjectiveProposalDisposition.PENDING
        return self._transition(
            proposal_id,
            expected,
            ObjectiveProposalDisposition.RETIRED,
        )

    def _transition(
        self,
        proposal_id: str,
        expected: ObjectiveProposalDisposition,
        disposition: ObjectiveProposalDisposition,
        *,
        approved_objective_id: str | None = None,
    ) -> ObjectiveProposalRecord:
        now = self.clock()
        with self._write_connection() as connection:
            updated = connection.execute(
                text(
                    "UPDATE objective_proposals SET disposition = :disposition, "
                    "approved_objective_id = COALESCE(:approved_objective_id, "
                    "approved_objective_id), updated_at = :updated_at "
                    "WHERE id = :id AND disposition = :expected"
                ),
                {
                    "id": proposal_id,
                    "expected": expected.value,
                    "disposition": disposition.value,
                    "approved_objective_id": approved_objective_id,
                    "updated_at": now,
                },
            )
            record = _select_record(connection, proposal_id)
            if updated.rowcount != 1 and not (
                record is not None
                and record.disposition is disposition
                and (
                    approved_objective_id is None
                    or record.approved_objective_id == approved_objective_id
                )
            ):
                raise ValueError("proposal changed during review")
            assert record is not None
        return record

    @contextmanager
    def _write_connection(self) -> Iterator[Connection]:
        engine = self.objectives.database.engine
        if engine.dialect.name != "sqlite":
            raise RuntimeError("atomic objective review requires SQLite serialization")
        with engine.connect() as connection:
            connection.exec_driver_sql("BEGIN IMMEDIATE")
            try:
                yield connection
                connection.commit()
            except Exception:
                connection.rollback()
                raise


def _scope(proposal: ProposedObjective) -> tuple[object, ...]:
    return proposal.course_id, proposal.exam_id, proposal.lecture_ids


def _union(left: tuple[str, ...], right: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(dict.fromkeys((*left, *right)))


def _combine(left: ProposedObjective, right: ProposedObjective) -> ProposedObjective:
    return replace(
        left,
        source_revision_ids=_union(left.source_revision_ids, right.source_revision_ids),
        evidence_ids=_union(left.evidence_ids, right.evidence_ids),
        suggested_links=tuple(dict.fromkeys((*left.suggested_links, *right.suggested_links))),
    )


def _proposal_payload(proposal: ProposedObjective) -> dict[str, object]:
    return {
        "observable_verb": proposal.observable_verb,
        "concept": proposal.concept,
        "description": proposal.description,
        "course_id": proposal.course_id,
        "exam_id": proposal.exam_id,
        "lecture_ids": proposal.lecture_ids,
        "source_revision_ids": proposal.source_revision_ids,
        "evidence_ids": proposal.evidence_ids,
        "suggested_links": [
            {"edge_type": link.edge_type.value, "target_concept": link.target_concept}
            for link in proposal.suggested_links
        ],
    }


def _proposal_from_payload(payload: dict[str, object]) -> ProposedObjective:
    raw_links = payload.get("suggested_links", [])
    if not isinstance(raw_links, list):
        raise ValueError("stored proposal links are invalid")
    links: list[SuggestedObjectiveLink] = []
    for link in raw_links:
        if not isinstance(link, dict):
            raise ValueError("stored proposal link is invalid")
        links.append(
            SuggestedObjectiveLink(
                ObjectiveEdgeType(_stored_text(link, "edge_type")),
                _stored_text(link, "target_concept"),
            )
        )
    exam_id = payload.get("exam_id")
    if exam_id is not None and not isinstance(exam_id, str):
        raise ValueError("stored proposal exam_id is invalid")
    return ProposedObjective(
        observable_verb=_stored_text(payload, "observable_verb"),
        concept=_stored_text(payload, "concept"),
        description=_stored_text(payload, "description"),
        course_id=_stored_text(payload, "course_id"),
        exam_id=exam_id,
        lecture_ids=_stored_identifiers(payload, "lecture_ids"),
        source_revision_ids=_stored_identifiers(payload, "source_revision_ids"),
        evidence_ids=_stored_identifiers(payload, "evidence_ids"),
        suggested_links=tuple(links),
    )


def _stored_text(payload: dict[str, object], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str):
        raise ValueError(f"stored proposal {key} is invalid")
    return value


def _stored_identifiers(payload: dict[str, object], key: str) -> tuple[str, ...]:
    value = payload.get(key)
    if not isinstance(value, (tuple, list)) or not all(
        isinstance(item, str) for item in value
    ):
        raise ValueError(f"stored proposal {key} is invalid")
    return tuple(value)


def _record_parameters(record: ObjectiveProposalRecord) -> dict[str, object]:
    return {
        "id": record.proposal.proposal_id,
        "payload": json.dumps(_proposal_payload(record.proposal), separators=(",", ":")),
        "disposition": record.disposition.value,
        "approved_objective_id": record.approved_objective_id,
        "merged_into_id": record.merged_into_id,
        "created_at": record.created_at,
        "updated_at": record.updated_at,
    }


def _record_from_row(row: RowMapping) -> ObjectiveProposalRecord:
    payload = json.loads(row["payload_json"])
    if not isinstance(payload, dict):
        raise ValueError("stored proposal payload is invalid")
    proposal = _proposal_from_payload(payload)
    if proposal.proposal_id != row["id"]:
        raise ValueError("stored proposal identity is invalid")
    return ObjectiveProposalRecord(
        proposal,
        ObjectiveProposalDisposition(row["disposition"]),
        row["created_at"],
        row["updated_at"],
        row["approved_objective_id"],
        row["merged_into_id"],
    )


def _select_record(
    connection: Connection,
    proposal_id: str,
) -> ObjectiveProposalRecord | None:
    row = (
        connection.execute(
            text("SELECT * FROM objective_proposals WHERE id = :id"),
            {"id": proposal_id},
        )
        .mappings()
        .first()
    )
    return None if row is None else _record_from_row(row)
