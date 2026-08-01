from sqlalchemy import inspect, text

from oms_hub.db import Database
from oms_hub.migrations import LATEST_SCHEMA_VERSION


def test_latest_schema_adds_native_quiz_and_notebook_source_registry(tmp_path):
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
        "outline_outputs",
        "quiz_outputs",
        "published_quizzes",
    } <= names
    source_columns = {
        column["name"]
        for column in inspect(database.engine).get_columns(
            "notebook_source_mappings"
        )
    }
    assert "display_title" in source_columns
    with database.session() as session:
        version = session.execute(
            text("SELECT version FROM schema_version WHERE id = 1")
        ).scalar_one()
    assert version == LATEST_SCHEMA_VERSION


def test_existing_generation_jobs_gain_later_optional_columns(tmp_path):
    database = Database(f"sqlite:///{tmp_path / 'hub.db'}")
    with database.engine.begin() as connection:
        connection.execute(
            text(
                """
                CREATE TABLE generation_jobs (
                    id VARCHAR(36) NOT NULL PRIMARY KEY,
                    lecture_id INTEGER NOT NULL,
                    kind VARCHAR(20) NOT NULL,
                    state VARCHAR(30) NOT NULL,
                    stage VARCHAR(30) NOT NULL,
                    attempts INTEGER NOT NULL,
                    next_attempt_at VARCHAR(40),
                    error TEXT,
                    prompt_path TEXT,
                    prompt_sha256 VARCHAR(64),
                    notebook_id VARCHAR(200),
                    pdf_source_id VARCHAR(200),
                    transcript_source_id VARCHAR(200),
                    notebook_answer TEXT,
                    created_at VARCHAR(40) NOT NULL,
                    updated_at VARCHAR(40) NOT NULL
                )
                """
            )
        )
        connection.execute(
            text(
                """
                INSERT INTO generation_jobs (
                    id, lecture_id, kind, state, stage, attempts,
                    created_at, updated_at
                ) VALUES (
                    'legacy-job', 1, 'outline', 'complete', 'done', 1,
                    '2026-07-25T12:00:00+00:00',
                    '2026-07-25T12:05:00+00:00'
                )
                """
            )
        )

    database.migrate()
    database.migrate()

    columns = {
        column["name"]
        for column in inspect(database.engine).get_columns("generation_jobs")
    }
    assert {
        "pdf_revision_id",
        "transcript_revision_id",
        "gemini_quiz_id",
        "quiz_url",
    } <= columns
    with database.engine.connect() as connection:
        preserved = connection.execute(
            text(
                "SELECT kind, state, attempts FROM generation_jobs "
                "WHERE id = 'legacy-job'"
            )
        ).one()
    assert preserved == ("outline", "complete", 1)
