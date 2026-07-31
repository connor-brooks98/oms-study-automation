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

LATEST_SCHEMA_VERSION = 10


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


def migrate_database(database: "Database") -> None:
    database.create_schema()
    _upgrade_anki_v4_columns(database)
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
