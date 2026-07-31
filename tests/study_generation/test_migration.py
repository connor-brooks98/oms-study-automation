from sqlalchemy import inspect, text

from oms_hub.db import Database
from oms_hub.migrations import LATEST_SCHEMA_VERSION


def test_schema_v8_adds_native_quiz_and_studio_source_registry(tmp_path):
    database = Database(f"sqlite:///{tmp_path / 'hub.db'}")
    database.migrate()

    names = set(inspect(database.engine).get_table_names())
    assert {
        "google_connection",
        "study_prompt_settings",
        "notebook_mappings",
        "notebook_source_mappings",
        "course_quiz_documents",
        "exam_quiz_tabs",
        "generation_jobs",
        "generation_attempts",
        "outline_outputs",
        "quiz_outputs",
        "published_quizzes",
        "studio_sources",
    } <= names
    source_columns = {
        column["name"]
        for column in inspect(database.engine).get_columns("notebook_source_mappings")
    }
    assert "display_title" in source_columns
    generation_columns = {
        column["name"] for column in inspect(database.engine).get_columns("generation_jobs")
    }
    assert "supersedes_job_id" in generation_columns
    assert "gemini_quiz_id" not in generation_columns
    generation_indexes = {
        index["name"] for index in inspect(database.engine).get_indexes("generation_jobs")
    }
    assert {
        "ix_generation_jobs_poll",
        "ix_generation_jobs_supersedes",
    } <= generation_indexes
    with database.session() as session:
        version = session.execute(
            text("SELECT version FROM schema_version WHERE id = 1")
        ).scalar_one()
    studio_columns = {
        column["name"] for column in inspect(database.engine).get_columns("studio_sources")
    }
    assert {
        "subject_key",
        "exam_number",
        "state",
        "attempts",
        "next_attempt_at",
        "remote_notebook_id",
        "remote_source_id",
        "created_at",
        "updated_at",
    } <= studio_columns
    studio_indexes = {
        index["name"] for index in inspect(database.engine).get_indexes("studio_sources")
    }
    assert "ix_studio_sources_scope_state" in studio_indexes
    assert version == LATEST_SCHEMA_VERSION == 8


def test_v6_generation_jobs_are_upgraded_without_losing_rows(tmp_path):
    database = Database(f"sqlite:///{tmp_path / 'hub.db'}")
    with database.engine.begin() as connection:
        connection.execute(
            text(
                """CREATE TABLE generation_jobs (
                id VARCHAR(36) PRIMARY KEY,
                lecture_id INTEGER NOT NULL REFERENCES lectures(id),
                kind VARCHAR(20) NOT NULL,
                state VARCHAR(30) NOT NULL,
                stage VARCHAR(30) NOT NULL,
                attempts INTEGER NOT NULL,
                next_attempt_at VARCHAR(40),
                error TEXT,
                prompt_path TEXT,
                prompt_sha256 VARCHAR(64),
                pdf_revision_id INTEGER,
                transcript_revision_id INTEGER,
                notebook_id VARCHAR(200),
                pdf_source_id VARCHAR(200),
                transcript_source_id VARCHAR(200),
                notebook_answer TEXT,
                gemini_quiz_id VARCHAR(500),
                quiz_url TEXT,
                created_at VARCHAR(40) NOT NULL,
                updated_at VARCHAR(40) NOT NULL
                )"""
            )
        )
    database.create_schema()
    with database.engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO lectures "
                "(id, subject, exam_number, lecture_number, topic, lecturer, "
                "created_at, updated_at) VALUES "
                "(1, 'Neuro', 1, 1, 'Seizures', '', 'now', 'now')"
            )
        )
        connection.execute(
            text(
                "INSERT INTO generation_jobs "
                "(id, lecture_id, kind, state, stage, attempts, created_at, updated_at) "
                "VALUES ('job-v6', 1, 'quiz', 'failed', 'quiz_validate', 1, 'now', 'now')"
            )
        )
        connection.execute(
            text("INSERT INTO schema_version (id, version, updated_at) VALUES (1, 6, 'now')")
        )

    database.migrate()

    columns = {column["name"] for column in inspect(database.engine).get_columns("generation_jobs")}
    assert "supersedes_job_id" in columns
    assert "gemini_quiz_id" not in columns
    with database.engine.connect() as connection:
        assert connection.execute(text("SELECT id FROM generation_jobs")).scalar_one() == "job-v6"
