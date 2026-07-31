from typing import TYPE_CHECKING

from sqlalchemy import inspect, select, text

from oms_hub.domain import StepStatus, V2StepName
from oms_hub.models import (
    LectureModel,
    LectureStepModel,
    PublishedQuizModel,
    SchemaVersionModel,
)

if TYPE_CHECKING:
    from oms_hub.db import Database

LATEST_SCHEMA_VERSION = 11


def migrate_database(database: "Database") -> None:
    database.create_schema()
    _migrate_published_quizzes(database)
    run_columns = {column["name"] for column in inspect(database.engine).get_columns("studio_runs")}
    if "label_key" not in run_columns:
        with database.engine.begin() as connection:
            connection.execute(
                text(
                    "ALTER TABLE studio_runs ADD COLUMN label_key VARCHAR(300) NOT NULL DEFAULT ''"
                )
            )
            connection.execute(text("UPDATE studio_runs SET label_key = lower(trim(label))"))
    if "published_token" not in run_columns:
        with database.engine.begin() as connection:
            connection.execute(
                text("ALTER TABLE studio_runs ADD COLUMN published_token VARCHAR(64)")
            )
    if "draft_payload_json" not in run_columns:
        with database.engine.begin() as connection:
            connection.execute(
                text("ALTER TABLE studio_runs ADD COLUMN draft_payload_json TEXT")
            )
    usage_columns = {
        column["name"] for column in inspect(database.engine).get_columns("study_usage")
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
        for column in inspect(database.engine).get_columns("notebook_source_mappings")
    }
    if "display_title" not in source_columns:
        with database.engine.begin() as connection:
            connection.execute(
                text(
                    "ALTER TABLE notebook_source_mappings "
                    "ADD COLUMN display_title VARCHAR(500) NOT NULL DEFAULT ''"
                )
            )
    generation_columns = {
        column["name"] for column in inspect(database.engine).get_columns("generation_jobs")
    }
    if "supersedes_job_id" not in generation_columns:
        with database.engine.begin() as connection:
            connection.execute(
                text(
                    "ALTER TABLE generation_jobs ADD COLUMN "
                    "supersedes_job_id VARCHAR(36) REFERENCES generation_jobs(id)"
                )
            )
    if "gemini_quiz_id" in generation_columns:
        with database.engine.begin() as connection:
            connection.execute(text("ALTER TABLE generation_jobs DROP COLUMN gemini_quiz_id"))
    with database.engine.begin() as connection:
        connection.execute(
            text(
                "CREATE INDEX IF NOT EXISTS ix_ingestion_jobs_poll "
                "ON ingestion_jobs (state, next_attempt_at, created_at)"
            )
        )
        connection.execute(
            text(
                "CREATE INDEX IF NOT EXISTS ix_generation_jobs_poll "
                "ON generation_jobs (state, next_attempt_at, created_at)"
            )
        )
        connection.execute(
            text(
                "CREATE INDEX IF NOT EXISTS ix_generation_jobs_supersedes "
                "ON generation_jobs (supersedes_job_id)"
            )
        )
    with database.session() as session:
        version = session.get(SchemaVersionModel, 1)
        if version is not None and version.version >= LATEST_SCHEMA_VERSION:
            return

        existing_steps = set(
            session.execute(select(LectureStepModel.lecture_id, LectureStepModel.name)).all()
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
            session.add(SchemaVersionModel(id=1, version=LATEST_SCHEMA_VERSION))
        else:
            version.version = LATEST_SCHEMA_VERSION


def _migrate_published_quizzes(database: "Database") -> None:
    columns = {
        column["name"] for column in inspect(database.engine).get_columns("published_quizzes")
    }
    if "studio_run_id" in columns:
        return
    with database.engine.begin() as connection:
        connection.execute(text("ALTER TABLE published_quizzes RENAME TO published_quizzes_v9"))
        PublishedQuizModel.metadata.tables["published_quizzes"].create(connection)
        connection.execute(
            text(
                """INSERT INTO published_quizzes (
                token, lecture_id, job_id, studio_run_id,
                destination_subject, destination_subject_key,
                destination_exam_number, label, label_key,
                title, payload_json, version, active, created_at, updated_at
                )
                SELECT p.token, p.lecture_id, p.job_id, NULL,
                       l.subject, lower(trim(l.subject)), l.exam_number,
                       p.title, lower(trim(p.title)), p.title,
                       p.payload_json, p.version, 1, p.created_at, p.updated_at
                FROM published_quizzes_v9 AS p
                JOIN lectures AS l ON l.id = p.lecture_id"""
            )
        )
        connection.execute(text("DROP TABLE published_quizzes_v9"))
