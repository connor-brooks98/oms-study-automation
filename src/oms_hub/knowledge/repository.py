"""Direct-SQL persistence for the source-trust records."""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Connection
from sqlalchemy.exc import IntegrityError

from oms_hub.db import Database
from oms_hub.knowledge.ids import sha256_text
from oms_hub.knowledge.ids import source_revision_id as make_revision_id
from oms_hub.knowledge.models import (
    EvidenceLocator,
    EvidenceLocatorKind,
    EvidenceUnit,
    KnowledgeSource,
    SourceRevision,
    SourceRevisionState,
)
from oms_hub.models import utc_now
from oms_hub.providers.contracts import AuthorityClass

__all__ = ["KnowledgeRepository"]


class KnowledgeRepository:
    """Persist source-trust records without extending the central ORM metadata."""

    def __init__(self, database: Database) -> None:
        self.database = database

    def initialize(self) -> None:
        """Create source-trust tables and indexes without touching old tables."""
        with self.database.engine.begin() as connection:
            connection.execute(
                text(
                    """
                    CREATE TABLE IF NOT EXISTS knowledge_sources (
                        id TEXT PRIMARY KEY NOT NULL,
                        authority_class TEXT NOT NULL
                    )
                    """
                )
            )
            connection.execute(
                text(
                    """
                    CREATE TABLE IF NOT EXISTS source_revisions (
                        id TEXT PRIMARY KEY NOT NULL,
                        source_document_id TEXT NOT NULL,
                        file_sha256 TEXT NOT NULL,
                        state TEXT NOT NULL,
                        UNIQUE (source_document_id, file_sha256),
                        FOREIGN KEY (source_document_id)
                            REFERENCES knowledge_sources (id)
                    )
                    """
                )
            )
            connection.execute(
                text(
                    """
                    CREATE TABLE IF NOT EXISTS evidence_units (
                        id TEXT PRIMARY KEY NOT NULL,
                        source_revision_id TEXT NOT NULL,
                        authority_class TEXT NOT NULL,
                        course_id TEXT,
                        exam_id TEXT,
                        lecture_id TEXT,
                        locator_kind TEXT NOT NULL,
                        locator_value TEXT NOT NULL,
                        normalized_text TEXT NOT NULL,
                        image_asset_id TEXT,
                        content_sha256 TEXT NOT NULL,
                        source_priority INTEGER NOT NULL DEFAULT 0,
                        created_at TEXT NOT NULL,
                        retired_at TEXT,
                        FOREIGN KEY (source_revision_id)
                            REFERENCES source_revisions (id)
                    )
                    """
                )
            )
            connection.execute(
                text(
                    """
                    CREATE INDEX IF NOT EXISTS ix_evidence_units_scope
                    ON evidence_units (course_id, exam_id, lecture_id, authority_class)
                    """
                )
            )
            connection.execute(
                text(
                    """
                    CREATE INDEX IF NOT EXISTS ix_evidence_units_revision_locator
                    ON evidence_units (source_revision_id, locator_kind, locator_value)
                    """
                )
            )

    def create_source(
        self,
        source_document_id: str | KnowledgeSource | None = None,
        authority_class: AuthorityClass | str | None = None,
        *,
        source: KnowledgeSource | None = None,
    ) -> KnowledgeSource:
        """Create a logical source, returning the existing matching source."""
        if source is not None:
            if source_document_id is not None or authority_class is not None:
                raise TypeError("source cannot be combined with source fields")
            candidate = source
        elif isinstance(source_document_id, KnowledgeSource):
            if authority_class is not None:
                raise TypeError("a KnowledgeSource cannot include authority_class")
            candidate = source_document_id
        else:
            if source_document_id is None or authority_class is None:
                raise TypeError("source_document_id and authority_class are required")
            candidate = KnowledgeSource(
                source_document_id,
                AuthorityClass(authority_class),
            )

        try:
            with self.database.engine.begin() as connection:
                existing = _select_source(connection, candidate.source_document_id)
                if existing is not None:
                    if existing.authority_class is not candidate.authority_class:
                        raise ValueError(
                            "authority_class does not match the existing knowledge source"
                        )
                    return existing
                connection.execute(
                    text(
                        "INSERT INTO knowledge_sources (id, authority_class) "
                        "VALUES (:id, :authority_class)"
                    ),
                    {
                        "id": candidate.source_document_id,
                        "authority_class": candidate.authority_class.value,
                    },
                )
        except IntegrityError as error:
            if not _is_unique_violation(error):
                raise
            with self.database.engine.connect() as connection:
                existing = _select_source(connection, candidate.source_document_id)
            if existing is None:
                raise
            if existing.authority_class is not candidate.authority_class:
                raise ValueError(
                    "authority_class does not match the existing knowledge source"
                ) from error
            return existing
        return candidate

    def create_revision(
        self,
        revision: SourceRevision | str | None = None,
        file_sha256: str | None = None,
        state: SourceRevisionState | str | None = None,
        *,
        source_document_id: str | None = None,
        source_revision_id: str | None = None,
    ) -> SourceRevision:
        """Create or fetch the canonical revision for a source/content hash."""
        if isinstance(revision, SourceRevision):
            if any(
                value is not None
                for value in (
                    file_sha256,
                    state,
                    source_document_id,
                    source_revision_id,
                )
            ):
                raise TypeError("a SourceRevision cannot include revision fields")
            candidate = revision
        else:
            if isinstance(revision, str):
                if source_document_id is not None:
                    raise TypeError("source_document_id was provided twice")
                source_document_id = revision
            if source_document_id is None or file_sha256 is None:
                raise TypeError("source_document_id and file_sha256 are required")
            candidate = SourceRevision(
                source_document_id=source_document_id,
                source_revision_id=source_revision_id
                or make_revision_id(source_document_id, file_sha256),
                file_sha256=file_sha256,
                state=SourceRevisionState(state or SourceRevisionState.STAGED),
            )

        expected_id = make_revision_id(
            candidate.source_document_id,
            candidate.file_sha256,
        )
        if candidate.source_revision_id != expected_id:
            raise ValueError(
                "source_revision_id must match its deterministic source/content identity"
            )

        try:
            with self.database.engine.begin() as connection:
                source_exists = connection.execute(
                    text("SELECT 1 FROM knowledge_sources WHERE id = :id"),
                    {"id": candidate.source_document_id},
                ).first()
                if source_exists is None:
                    raise KeyError(candidate.source_document_id)

                existing = _select_revision_by_hash(
                    connection,
                    candidate.source_document_id,
                    candidate.file_sha256,
                )
                if existing is not None:
                    return existing

                existing_id = _select_revision_by_id(
                    connection,
                    candidate.source_revision_id,
                )
                if existing_id is not None:
                    raise ValueError(
                        "source_revision_id already refers to a different revision"
                    )

                connection.execute(
                    text(
                        "INSERT INTO source_revisions "
                        "(id, source_document_id, file_sha256, state) "
                        "VALUES (:id, :source_document_id, :file_sha256, :state)"
                    ),
                    {
                        "id": candidate.source_revision_id,
                        "source_document_id": candidate.source_document_id,
                        "file_sha256": candidate.file_sha256,
                        "state": candidate.state.value,
                    },
                )
        except IntegrityError as error:
            if not _is_unique_violation(error):
                raise
            with self.database.engine.connect() as connection:
                existing = _select_revision_by_hash(
                    connection,
                    candidate.source_document_id,
                    candidate.file_sha256,
                )
                existing_id = _select_revision_by_id(
                    connection,
                    candidate.source_revision_id,
                )
            if existing is not None:
                return existing
            if existing_id is not None:
                raise ValueError(
                    "source_revision_id already refers to a different revision"
                ) from error
            raise
        return candidate

    def put_evidence_units(
        self,
        revision_id: str,
        units: Sequence[EvidenceUnit],
    ) -> None:
        """Insert immutable evidence after checking its source and content trust."""
        evidence = tuple(units)
        for attempt in range(2):
            try:
                self._put_evidence_units_once(revision_id, evidence)
                return
            except IntegrityError as error:
                if attempt or not _is_unique_violation(error):
                    raise

    def _put_evidence_units_once(
        self,
        revision_id: str,
        evidence: tuple[EvidenceUnit, ...],
    ) -> None:
        with self.database.engine.begin() as connection:
            revision = connection.execute(
                text(
                    """
                    SELECT knowledge_sources.authority_class, source_revisions.state
                    FROM source_revisions
                    JOIN knowledge_sources
                      ON knowledge_sources.id = source_revisions.source_document_id
                    WHERE source_revisions.id = :revision_id
                    """
                ),
                {"revision_id": revision_id},
            ).mappings().first()
            if revision is None:
                raise KeyError(revision_id)
            source_authority = revision["authority_class"]
            revision_state = SourceRevisionState(revision["state"])
            if revision_state is not SourceRevisionState.RETIRED:
                claim = connection.execute(
                    text(
                        "UPDATE source_revisions SET state = state "
                        "WHERE id = :revision_id AND state <> :retired_state"
                    ),
                    {
                        "revision_id": revision_id,
                        "retired_state": SourceRevisionState.RETIRED.value,
                    },
                )
                if claim.rowcount != 1:
                    raise ValueError("cannot add new evidence to a retired revision")

            for unit in evidence:
                if unit.source_revision_id != revision_id:
                    raise ValueError(
                        "evidence source_revision_id does not match the requested revision"
                    )
                if unit.authority_class.value != source_authority:
                    raise ValueError(
                        "evidence authority_class does not match its knowledge source"
                    )
                if unit.content_sha256 != sha256_text(unit.normalized_text):
                    raise ValueError(
                        "content_sha256 must equal sha256_text(normalized_text)"
                    )

            pending: dict[str, EvidenceUnit] = {}
            for unit in evidence:
                previous = pending.get(unit.evidence_id)
                if previous is not None and previous != unit:
                    raise ValueError("duplicate evidence_id has different content")
                pending[unit.evidence_id] = unit

            existing_units: dict[str, EvidenceUnit] = {}
            for unit in pending.values():
                existing = connection.execute(
                    text(
                        "SELECT id, source_revision_id, authority_class, course_id, "
                        "exam_id, lecture_id, locator_kind, locator_value, "
                        "normalized_text, image_asset_id, content_sha256, "
                        "source_priority, created_at, retired_at "
                        "FROM evidence_units WHERE id = :id"
                    ),
                    {"id": unit.evidence_id},
                ).mappings().first()
                if existing is not None:
                    stored = _evidence_from_row(existing)
                    if stored != unit:
                        raise ValueError("evidence_id already refers to different content")
                    existing_units[unit.evidence_id] = stored

            if (
                revision_state is SourceRevisionState.RETIRED
                and len(existing_units) != len(pending)
            ):
                raise ValueError("cannot add new evidence to a retired revision")

            for unit in pending.values():
                if unit.evidence_id in existing_units:
                    continue
                connection.execute(
                    text(
                        """
                        INSERT INTO evidence_units (
                            id, source_revision_id, authority_class, course_id,
                            exam_id, lecture_id, locator_kind, locator_value,
                            normalized_text, image_asset_id, content_sha256,
                            source_priority, created_at, retired_at
                        ) VALUES (
                            :id, :source_revision_id, :authority_class, :course_id,
                            :exam_id, :lecture_id, :locator_kind, :locator_value,
                            :normalized_text, :image_asset_id, :content_sha256,
                            :source_priority, :created_at, :retired_at
                        )
                        """
                    ),
                    _evidence_parameters(unit),
                )

    def get_revision(self, revision_id: str) -> SourceRevision | None:
        with self.database.engine.connect() as connection:
            row = connection.execute(
                text(
                    "SELECT id, source_document_id, file_sha256, state "
                    "FROM source_revisions WHERE id = :id"
                ),
                {"id": revision_id},
            ).mappings().first()
        return None if row is None else _revision_from_row(row)

    def list_evidence(self, revision_id: str) -> tuple[EvidenceUnit, ...]:
        with self.database.engine.connect() as connection:
            rows = connection.execute(
                text(
                    """
                    SELECT id, source_revision_id, authority_class, course_id,
                           exam_id, lecture_id, locator_kind, locator_value,
                           normalized_text, image_asset_id, content_sha256,
                           source_priority, created_at, retired_at
                    FROM evidence_units
                    WHERE source_revision_id = :revision_id
                    ORDER BY created_at, id
                    """
                ),
                {"revision_id": revision_id},
            ).mappings().all()
        return tuple(_evidence_from_row(row) for row in rows)

    def retire_revision(self, revision_id: str) -> None:
        retired_at = utc_now()
        with self.database.engine.begin() as connection:
            retired = connection.execute(
                text(
                    "UPDATE source_revisions SET state = :state WHERE id = :id"
                ),
                {"state": SourceRevisionState.RETIRED.value, "id": revision_id},
            )
            if retired.rowcount != 1:
                raise KeyError(revision_id)
            connection.execute(
                text(
                    "UPDATE evidence_units SET retired_at = COALESCE(retired_at, :retired_at) "
                    "WHERE source_revision_id = :revision_id"
                ),
                {"retired_at": retired_at, "revision_id": revision_id},
            )

    def dependent_artifact_ids(self, revision_id: str) -> tuple[str, ...]:
        """Return no dependencies until Task 5.2 owns artifact provenance."""
        del revision_id
        return ()


def _select_source(
    connection: Connection,
    source_document_id: str,
) -> KnowledgeSource | None:
    row = connection.execute(
        text(
            "SELECT id, authority_class FROM knowledge_sources "
            "WHERE id = :id"
        ),
        {"id": source_document_id},
    ).mappings().first()
    return None if row is None else _source_from_row(row)


def _select_revision_by_hash(
    connection: Connection,
    source_document_id: str,
    file_sha256: str,
) -> SourceRevision | None:
    row = connection.execute(
        text(
            "SELECT id, source_document_id, file_sha256, state "
            "FROM source_revisions "
            "WHERE source_document_id = :source_document_id "
            "AND file_sha256 = :file_sha256"
        ),
        {
            "source_document_id": source_document_id,
            "file_sha256": file_sha256,
        },
    ).mappings().first()
    return None if row is None else _revision_from_row(row)


def _select_revision_by_id(
    connection: Connection,
    revision_id: str,
) -> SourceRevision | None:
    row = connection.execute(
        text(
            "SELECT id, source_document_id, file_sha256, state "
            "FROM source_revisions WHERE id = :id"
        ),
        {"id": revision_id},
    ).mappings().first()
    return None if row is None else _revision_from_row(row)


def _is_unique_violation(error: IntegrityError) -> bool:
    original = error.orig
    if getattr(original, "pgcode", None) == "23505":
        return True
    sqlite_code = getattr(original, "sqlite_errorcode", None)
    if sqlite_code in {
        sqlite3.SQLITE_CONSTRAINT_PRIMARYKEY,
        sqlite3.SQLITE_CONSTRAINT_UNIQUE,
    }:
        return True
    message = str(original).lower()
    return "unique constraint" in message or "duplicate key" in message


def _source_from_row(row: Any) -> KnowledgeSource:
    return KnowledgeSource(row["id"], AuthorityClass(row["authority_class"]))


def _revision_from_row(row: Any) -> SourceRevision:
    return SourceRevision(
        source_document_id=row["source_document_id"],
        source_revision_id=row["id"],
        file_sha256=row["file_sha256"],
        state=SourceRevisionState(row["state"]),
    )


def _evidence_from_row(row: Any) -> EvidenceUnit:
    return EvidenceUnit(
        evidence_id=row["id"],
        source_revision_id=row["source_revision_id"],
        authority_class=AuthorityClass(row["authority_class"]),
        course_id=row["course_id"],
        exam_id=row["exam_id"],
        lecture_id=row["lecture_id"],
        locator=EvidenceLocator(
            EvidenceLocatorKind(row["locator_kind"]),
            row["locator_value"],
        ),
        normalized_text=row["normalized_text"],
        content_sha256=row["content_sha256"],
        image_asset_id=row["image_asset_id"],
        source_priority=row["source_priority"],
        created_at=row["created_at"],
        retired_at=row["retired_at"],
    )


def _evidence_parameters(unit: EvidenceUnit) -> dict[str, object]:
    return {
        "id": unit.evidence_id,
        "source_revision_id": unit.source_revision_id,
        "authority_class": unit.authority_class.value,
        "course_id": unit.course_id,
        "exam_id": unit.exam_id,
        "lecture_id": unit.lecture_id,
        "locator_kind": unit.locator.kind.value,
        "locator_value": unit.locator.value,
        "normalized_text": unit.normalized_text,
        "image_asset_id": unit.image_asset_id,
        "content_sha256": unit.content_sha256,
        "source_priority": unit.source_priority,
        "created_at": unit.created_at,
        "retired_at": unit.retired_at,
    }
