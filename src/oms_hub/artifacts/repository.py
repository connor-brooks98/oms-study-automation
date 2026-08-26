"""Direct-SQL persistence for immutable artifact provenance."""

from __future__ import annotations

import hashlib
from collections.abc import Sequence

from sqlalchemy import text
from sqlalchemy.engine import Connection

from oms_hub.artifacts.models import ArtifactKind
from oms_hub.artifacts.provenance import (
    ArtifactEvidenceLink,
    ArtifactRun,
    compute_artifact_input_hash,
)
from oms_hub.db import Database

__all__ = ["ArtifactRepository"]


class ArtifactRepository:
    def __init__(self, database: Database) -> None:
        self.database = database

    def initialize(self) -> None:
        with self.database.engine.begin() as connection:
            connection.execute(
                text(
                    """
                    CREATE TABLE IF NOT EXISTS artifact_runs (
                        artifact_id TEXT PRIMARY KEY NOT NULL,
                        artifact_kind TEXT NOT NULL,
                        recipe_id TEXT NOT NULL,
                        recipe_version TEXT NOT NULL,
                        provider TEXT,
                        model TEXT,
                        prompt_version TEXT,
                        schema_version TEXT,
                        input_hash TEXT NOT NULL,
                        output_hash TEXT NOT NULL,
                        created_at TEXT NOT NULL,
                        validation_status TEXT NOT NULL,
                        stale_reason TEXT
                    )
                    """
                )
            )
            connection.execute(
                text(
                    """
                    CREATE TABLE IF NOT EXISTS artifact_run_sources (
                        artifact_id TEXT NOT NULL,
                        source_revision_id TEXT NOT NULL,
                        PRIMARY KEY (artifact_id, source_revision_id),
                        FOREIGN KEY (artifact_id) REFERENCES artifact_runs (artifact_id),
                        FOREIGN KEY (source_revision_id) REFERENCES source_revisions (id)
                    )
                    """
                )
            )
            connection.execute(
                text(
                    """
                    CREATE TABLE IF NOT EXISTS artifact_evidence (
                        artifact_id TEXT NOT NULL,
                        source_revision_id TEXT NOT NULL,
                        evidence_id TEXT NOT NULL,
                        PRIMARY KEY (artifact_id, evidence_id),
                        FOREIGN KEY (artifact_id) REFERENCES artifact_runs (artifact_id),
                        FOREIGN KEY (source_revision_id) REFERENCES source_revisions (id),
                        FOREIGN KEY (evidence_id) REFERENCES evidence_units (id)
                    )
                    """
                )
            )
            connection.execute(
                text(
                    """
                    CREATE INDEX IF NOT EXISTS ix_artifact_run_sources_revision
                    ON artifact_run_sources (source_revision_id, artifact_id)
                    """
                )
            )
            connection.execute(
                text(
                    """
                    CREATE INDEX IF NOT EXISTS ix_artifact_evidence_revision
                    ON artifact_evidence (source_revision_id, artifact_id)
                    """
                )
            )

    def record_run(
        self,
        run: ArtifactRun,
        evidence_links: Sequence[ArtifactEvidenceLink] = (),
    ) -> ArtifactRun:
        requested_links = tuple(evidence_links)
        if len(requested_links) != len(set(requested_links)):
            raise ValueError("duplicate artifact evidence link")
        with self.database.engine.begin() as connection:
            resolved_links = self._validate_dependencies(connection, run)
            if requested_links:
                for link in requested_links:
                    if link.artifact_id != run.artifact_id:
                        raise ValueError("evidence link artifact_id does not match its run")
                if _sort_links(requested_links) != resolved_links:
                    raise ValueError(
                        "evidence source revision links do not match run provenance"
                    )

            existing = self._get_run(connection, run.artifact_id)
            if existing is not None:
                if (
                    _immutable_run(existing) != _immutable_run(run)
                    or existing.source_revision_ids != run.source_revision_ids
                    or existing.evidence_ids != run.evidence_ids
                ):
                    raise ValueError("artifact_id already refers to different provenance")
                return existing

            connection.execute(
                text(
                    """
                    INSERT INTO artifact_runs (
                        artifact_id, artifact_kind, recipe_id, recipe_version,
                        provider, model, prompt_version, schema_version,
                        input_hash, output_hash, created_at, validation_status,
                        stale_reason
                    ) VALUES (
                        :artifact_id, :artifact_kind, :recipe_id, :recipe_version,
                        :provider, :model, :prompt_version, :schema_version,
                        :input_hash, :output_hash, :created_at, :validation_status,
                        :stale_reason
                    )
                    """
                ),
                _run_parameters(run),
            )
            for revision_id in run.source_revision_ids:
                connection.execute(
                    text(
                        "INSERT INTO artifact_run_sources "
                        "(artifact_id, source_revision_id) VALUES (:artifact_id, :revision_id)"
                    ),
                    {"artifact_id": run.artifact_id, "revision_id": revision_id},
                )
            for link in resolved_links:
                connection.execute(
                    text(
                        "INSERT INTO artifact_evidence "
                        "(artifact_id, source_revision_id, evidence_id) "
                        "VALUES (:artifact_id, :source_revision_id, :evidence_id)"
                    ),
                    {
                        "artifact_id": link.artifact_id,
                        "source_revision_id": link.source_revision_id,
                        "evidence_id": link.evidence_id,
                    },
                )
        return run

    def get_run(self, artifact_id: str) -> ArtifactRun | None:
        with self.database.engine.connect() as connection:
            return self._get_run(connection, artifact_id)

    def evidence_links(self, artifact_id: str) -> tuple[ArtifactEvidenceLink, ...]:
        with self.database.engine.connect() as connection:
            return self._links(connection, artifact_id)

    def mark_stale_by_revision(self, revision_id: str) -> tuple[str, ...]:
        with self.database.engine.begin() as connection:
            if connection.execute(
                text("SELECT 1 FROM source_revisions WHERE id = :id"),
                {"id": revision_id},
            ).first() is None:
                raise KeyError(revision_id)
            artifact_ids = tuple(
                row[0]
                for row in connection.execute(
                    text(
                        "SELECT artifact_id FROM artifact_run_sources "
                        "WHERE source_revision_id = :revision_id ORDER BY artifact_id"
                    ),
                    {"revision_id": revision_id},
                ).all()
            )
            connection.execute(
                text(
                    """
                    UPDATE artifact_runs
                    SET stale_reason = COALESCE(stale_reason, :reason)
                    WHERE artifact_id IN (
                        SELECT artifact_id FROM artifact_run_sources
                        WHERE source_revision_id = :revision_id
                    )
                    """
                ),
                {
                    "reason": f"source_revision_stale:{revision_id}",
                    "revision_id": revision_id,
                },
            )
        return artifact_ids

    def backfill_legacy_artifacts(self) -> tuple[ArtifactRun, ...]:
        """Backfill durable legacy outline/lecture-quiz records without evidence guesses."""
        with self.database.engine.connect() as connection:
            outlines = connection.execute(
                text(
                    """
                    SELECT o.id, o.job_id, o.sha256, o.created_at,
                           j.prompt_sha256
                    FROM outline_outputs o
                    JOIN generation_jobs j ON j.id = o.job_id
                    ORDER BY o.id
                    """
                )
            ).mappings().all()
            quizzes = connection.execute(
                text(
                    """
                    SELECT q.id, q.job_id, q.url, q.created_at,
                           j.prompt_sha256,
                           (
                               SELECT p.payload_json
                               FROM published_quizzes p
                               WHERE p.job_id = q.job_id
                               ORDER BY p.created_at, p.token
                               LIMIT 1
                           ) AS payload_json
                    FROM quiz_outputs q
                    JOIN generation_jobs j ON j.id = q.job_id
                    ORDER BY q.id
                    """
                )
            ).mappings().all()

        recorded: list[ArtifactRun] = []
        for row in outlines:
            recorded.append(
                self.record_run(
                    ArtifactRun(
                        artifact_id=f"legacy-outline:{row['id']}",
                        artifact_kind=ArtifactKind.LECTURE_OUTLINE,
                        recipe_id="lecture-outline-current",
                        recipe_version="current-v1",
                        provider="notebooklm",
                        model=None,
                        prompt_version=row["prompt_sha256"],
                        schema_version=None,
                        source_revision_ids=(),
                        evidence_ids=(),
                        input_hash=compute_artifact_input_hash(
                            {"job_id": row["job_id"], "kind": "outline"},
                        ),
                        output_hash=row["sha256"],
                        created_at=row["created_at"],
                        validation_status="legacy_unverified",
                    )
                )
            )
        for row in quizzes:
            output = row["payload_json"] or row["url"]
            recorded.append(
                self.record_run(
                    ArtifactRun(
                        artifact_id=f"legacy-lecture-quiz:{row['id']}",
                        artifact_kind=ArtifactKind.LECTURE_QUIZ,
                        recipe_id="lecture-quiz-current",
                        recipe_version="current-v1",
                        provider="notebooklm",
                        model=None,
                        prompt_version=row["prompt_sha256"],
                        schema_version=None,
                        source_revision_ids=(),
                        evidence_ids=(),
                        input_hash=compute_artifact_input_hash(
                            {"job_id": row["job_id"], "kind": "lecture_quiz"},
                        ),
                        output_hash=hashlib.sha256(output.encode("utf-8")).hexdigest(),
                        created_at=row["created_at"],
                        validation_status="legacy_unverified",
                    )
                )
            )
        return tuple(recorded)

    def _validate_dependencies(
        self,
        connection: Connection,
        run: ArtifactRun,
    ) -> tuple[ArtifactEvidenceLink, ...]:
        for revision_id in run.source_revision_ids:
            if connection.execute(
                text("SELECT 1 FROM source_revisions WHERE id = :id"),
                {"id": revision_id},
            ).first() is None:
                raise KeyError(revision_id)
        links: list[ArtifactEvidenceLink] = []
        for evidence_id in run.evidence_ids:
            row = connection.execute(
                text("SELECT source_revision_id FROM evidence_units WHERE id = :id"),
                {"id": evidence_id},
            ).first()
            if row is None:
                raise KeyError(evidence_id)
            source_revision_id = row[0]
            if source_revision_id not in run.source_revision_ids:
                raise ValueError("evidence source revision is not linked to the artifact run")
            links.append(
                ArtifactEvidenceLink(run.artifact_id, source_revision_id, evidence_id)
            )
        return _sort_links(links)

    def _get_run(self, connection: Connection, artifact_id: str) -> ArtifactRun | None:
        row = connection.execute(
            text("SELECT * FROM artifact_runs WHERE artifact_id = :artifact_id"),
            {"artifact_id": artifact_id},
        ).mappings().first()
        if row is None:
            return None
        sources = tuple(
            item[0]
            for item in connection.execute(
                text(
                    "SELECT source_revision_id FROM artifact_run_sources "
                    "WHERE artifact_id = :artifact_id ORDER BY source_revision_id"
                ),
                {"artifact_id": artifact_id},
            ).all()
        )
        evidence = tuple(link.evidence_id for link in self._links(connection, artifact_id))
        return ArtifactRun(
            artifact_id=row["artifact_id"],
            artifact_kind=ArtifactKind(row["artifact_kind"]),
            recipe_id=row["recipe_id"],
            recipe_version=row["recipe_version"],
            provider=row["provider"],
            model=row["model"],
            prompt_version=row["prompt_version"],
            schema_version=row["schema_version"],
            source_revision_ids=sources,
            evidence_ids=evidence,
            input_hash=row["input_hash"],
            output_hash=row["output_hash"],
            created_at=row["created_at"],
            validation_status=row["validation_status"],
            stale_reason=row["stale_reason"],
        )

    @staticmethod
    def _links(
        connection: Connection,
        artifact_id: str,
    ) -> tuple[ArtifactEvidenceLink, ...]:
        rows = connection.execute(
            text(
                "SELECT artifact_id, source_revision_id, evidence_id "
                "FROM artifact_evidence WHERE artifact_id = :artifact_id "
                "ORDER BY source_revision_id, evidence_id"
            ),
            {"artifact_id": artifact_id},
        ).all()
        return tuple(ArtifactEvidenceLink(*row) for row in rows)


def _run_parameters(run: ArtifactRun) -> dict[str, object]:
    return {
        "artifact_id": run.artifact_id,
        "artifact_kind": run.artifact_kind.value,
        "recipe_id": run.recipe_id,
        "recipe_version": run.recipe_version,
        "provider": run.provider,
        "model": run.model,
        "prompt_version": run.prompt_version,
        "schema_version": run.schema_version,
        "input_hash": run.input_hash,
        "output_hash": run.output_hash,
        "created_at": run.created_at,
        "validation_status": run.validation_status,
        "stale_reason": run.stale_reason,
    }


def _immutable_run(run: ArtifactRun) -> tuple[object, ...]:
    return (
        run.artifact_id,
        run.artifact_kind,
        run.recipe_id,
        run.recipe_version,
        run.provider,
        run.model,
        run.prompt_version,
        run.schema_version,
        run.input_hash,
        run.output_hash,
        run.created_at,
        run.validation_status,
    )


def _sort_links(
    links: Sequence[ArtifactEvidenceLink],
) -> tuple[ArtifactEvidenceLink, ...]:
    return tuple(sorted(links, key=lambda link: (link.source_revision_id, link.evidence_id)))
