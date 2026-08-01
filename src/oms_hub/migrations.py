from typing import TYPE_CHECKING

from sqlalchemy import inspect, select, text

import oms_hub.anki.models  # noqa: F401
from oms_hub.domain import StepStatus, V2StepName
from oms_hub.models import (
    LectureModel,
    LectureStepModel,
    SchemaVersionModel,
)

if TYPE_CHECKING:
    from oms_hub.db import Database

LATEST_SCHEMA_VERSION = 11


def _ensure_column(
    database: "Database",
    table_name: str,
    column_name: str,
    definition: str,
) -> None:
    inspector = inspect(database.engine)
    if not inspector.has_table(table_name):
        return
    columns = {
        column["name"]
        for column in inspector.get_columns(table_name)
    }
    if column_name in columns:
        return
    with database.engine.begin() as connection:
        connection.execute(
            text(
                f"ALTER TABLE {table_name} "
                f"ADD COLUMN {column_name} {definition}"
            )
        )


def _upgrade_anki_v4_columns(database: "Database") -> None:
    empty_sha256 = (
        "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
    )
    job_columns = {
        "block_id": "VARCHAR(200)",
        "source_revision_ids_json": "TEXT NOT NULL DEFAULT '[]'",
        "source_revision_hashes_json": "TEXT NOT NULL DEFAULT '{}'",
        "summary_outline_id": "INTEGER",
        "summary_outline_sha256": "VARCHAR(64)",
        "deck_allowlist_json": "TEXT NOT NULL DEFAULT '[]'",
        "tag_allowlist_json": "TEXT NOT NULL DEFAULT '[]'",
        "provider": "VARCHAR(30) NOT NULL DEFAULT 'anthropic'",
        "model": "VARCHAR(200) NOT NULL DEFAULT 'claude-sonnet-5'",
        "semantic_generation": "VARCHAR(200)",
        "companion_generation": "VARCHAR(200)",
        "source_index_generation": "VARCHAR(200)",
        "configuration_sha256": (
            f"VARCHAR(64) NOT NULL DEFAULT '{empty_sha256}'"
        ),
        "apply_state": "VARCHAR(50) NOT NULL DEFAULT 'pending'",
        "lease_owner": "VARCHAR(100)",
        "lease_expires_at": "VARCHAR(40)",
        "available_at": "VARCHAR(40)",
    }
    for name, definition in job_columns.items():
        _ensure_column(database, "anki_curation_jobs", name, definition)
    _ensure_column(
        database,
        "anki_candidates",
        "retrieval_pass",
        "VARCHAR(30) NOT NULL DEFAULT 'pass_1'",
    )
    gap_columns = {
        "source_refs_json": "TEXT NOT NULL DEFAULT '[]'",
        "evidence_ids_json": "TEXT NOT NULL DEFAULT '[]'",
        "provenance_json": "TEXT NOT NULL DEFAULT '{}'",
        "initial_tags_json": "TEXT NOT NULL DEFAULT '[]'",
        "content_hash": f"VARCHAR(64) NOT NULL DEFAULT '{empty_sha256}'",
    }
    for name, definition in gap_columns.items():
        _ensure_column(database, "anki_gap_cards", name, definition)


def _upgrade_generation_job_columns(database: "Database") -> None:
    columns = {
        "pdf_revision_id": "INTEGER",
        "transcript_revision_id": "INTEGER",
        "gemini_quiz_id": "VARCHAR(500)",
        "quiz_url": "TEXT",
    }
    for name, definition in columns.items():
        _ensure_column(database, "generation_jobs", name, definition)


def _upgrade_gap_card_identity(database: "Database") -> None:
    if database.engine.dialect.name != "sqlite":
        return
    inspector = inspect(database.engine)
    if not inspector.has_table("anki_gap_cards"):
        return
    has_legacy_unique = any(
        set(constraint.get("column_names") or ()) == {"job_id", "concept_id"}
        for constraint in inspector.get_unique_constraints("anki_gap_cards")
    )
    if not has_legacy_unique:
        return
    with database.engine.begin() as connection:
        connection.execute(
            text(
                """
                CREATE TABLE anki_gap_cards_v11 (
                    id VARCHAR(36) NOT NULL PRIMARY KEY,
                    job_id VARCHAR(36) NOT NULL,
                    concept_id VARCHAR(200) NOT NULL,
                    text TEXT NOT NULL,
                    extra TEXT NOT NULL,
                    revision INTEGER NOT NULL,
                    selected BOOLEAN NOT NULL,
                    image_state VARCHAR(30) NOT NULL,
                    media_filename TEXT,
                    source_note_id INTEGER,
                    generated_image_json TEXT NOT NULL,
                    validation_state VARCHAR(30) NOT NULL,
                    source_refs_json TEXT NOT NULL DEFAULT '[]',
                    evidence_ids_json TEXT NOT NULL DEFAULT '[]',
                    provenance_json TEXT NOT NULL DEFAULT '{}',
                    initial_tags_json TEXT NOT NULL DEFAULT '[]',
                    content_hash VARCHAR(64) NOT NULL,
                    FOREIGN KEY(job_id) REFERENCES anki_curation_jobs (id)
                )
                """
            )
        )
        connection.execute(
            text(
                """
                INSERT INTO anki_gap_cards_v11 (
                    id, job_id, concept_id, text, extra, revision, selected,
                    image_state, media_filename, source_note_id,
                    generated_image_json, validation_state,
                    source_refs_json, evidence_ids_json, provenance_json,
                    initial_tags_json, content_hash
                )
                SELECT
                    id, job_id, concept_id, text, extra, revision, selected,
                    image_state, media_filename, source_note_id,
                    generated_image_json, validation_state,
                    source_refs_json, evidence_ids_json, provenance_json,
                    initial_tags_json, content_hash
                FROM anki_gap_cards
                """
            )
        )
        connection.execute(text("DROP TABLE anki_gap_cards"))
        connection.execute(
            text("ALTER TABLE anki_gap_cards_v11 RENAME TO anki_gap_cards")
        )


def migrate_database(database: "Database") -> None:
    database.create_schema()
    _upgrade_generation_job_columns(database)
    _upgrade_anki_v4_columns(database)
    _upgrade_gap_card_identity(database)
    usage_columns = {
        column["name"]
        for column in inspect(database.engine).get_columns("study_usage")
    }
    if "provider" not in usage_columns:
        with database.engine.begin() as connection:
            connection.execute(
                text(
                    "ALTER TABLE study_usage ADD COLUMN provider "
                    "VARCHAR(30) NOT NULL DEFAULT 'openai'"
                )
            )
    source_columns = {
        column["name"]
        for column in inspect(database.engine).get_columns(
            "notebook_source_mappings"
        )
    }
    if "display_title" not in source_columns:
        with database.engine.begin() as connection:
            connection.execute(
                text(
                    "ALTER TABLE notebook_source_mappings "
                    "ADD COLUMN display_title VARCHAR(500) NOT NULL DEFAULT ''"
                )
            )
    with database.session() as session:
        version = session.get(SchemaVersionModel, 1)
        if version is not None and version.version >= LATEST_SCHEMA_VERSION:
            return

        existing_steps = set(
            session.execute(
                select(LectureStepModel.lecture_id, LectureStepModel.name)
            ).all()
        )
        lecture_ids = session.scalars(select(LectureModel.id)).all()
        for lecture_id in lecture_ids:
            for step in V2StepName:
                if (lecture_id, step.value) not in existing_steps:
                    session.add(
                        LectureStepModel(
                            lecture_id=lecture_id,
                            name=step.value,
                            status=StepStatus.WAITING.value,
                        )
                    )

        if version is None:
            session.add(
                SchemaVersionModel(id=1, version=LATEST_SCHEMA_VERSION)
            )
        else:
            version.version = LATEST_SCHEMA_VERSION
