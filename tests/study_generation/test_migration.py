from pathlib import Path

from sqlalchemy import inspect, text

from oms_hub.db import Database
from oms_hub.migrations import LATEST_SCHEMA_VERSION
from oms_hub.models import PublishedQuizModel


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
        "studio_source_operations",
    } <= names
    source_columns = {
        column["name"]
        for column in inspect(database.engine).get_columns(
            "notebook_source_mappings"
        )
    }
    assert "display_title" in source_columns
    studio_source_columns = {
        column["name"]
        for column in inspect(database.engine).get_columns("studio_sources")
    }
    assert {"import_role", "import_attach_to_notebook"} <= studio_source_columns
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


def _v14_database(tmp_path: Path) -> Database:
    database = Database(f"sqlite:///{tmp_path / 'legacy-v14.db'}")
    with database.engine.begin() as connection:
        connection.execute(
            text(
                """
                CREATE TABLE schema_version (
                    id INTEGER NOT NULL PRIMARY KEY,
                    version INTEGER NOT NULL,
                    updated_at VARCHAR(40) NOT NULL
                )
                """
            )
        )
        connection.execute(
            text(
                "INSERT INTO schema_version (id, version, updated_at) "
                "VALUES (1, 14, '2026-08-05T12:00:00+00:00')"
            )
        )
        connection.execute(
            text(
                """
                CREATE TABLE studio_sources (
                    id VARCHAR(36) NOT NULL PRIMARY KEY,
                    subject VARCHAR(100) NOT NULL,
                    subject_key VARCHAR(100) NOT NULL,
                    exam_number INTEGER NOT NULL,
                    source_type VARCHAR(20) NOT NULL,
                    title VARCHAR(500) NOT NULL,
                    original_filename VARCHAR(500),
                    payload_path TEXT,
                    source_url TEXT,
                    state VARCHAR(30) NOT NULL,
                    attempts INTEGER NOT NULL,
                    next_attempt_at VARCHAR(40),
                    diagnostic_source VARCHAR(40),
                    error TEXT,
                    remote_notebook_id VARCHAR(200),
                    remote_source_id VARCHAR(200),
                    converted_from_pptx BOOLEAN NOT NULL,
                    created_at VARCHAR(40) NOT NULL,
                    updated_at VARCHAR(40) NOT NULL
                )
                """
            )
        )
        connection.execute(
            text(
                """
                CREATE TABLE studio_runs (
                    id VARCHAR(36) NOT NULL PRIMARY KEY,
                    subject VARCHAR(100) NOT NULL,
                    subject_key VARCHAR(100) NOT NULL,
                    exam_number INTEGER NOT NULL,
                    destination_subject VARCHAR(100) NOT NULL,
                    destination_subject_key VARCHAR(100) NOT NULL,
                    destination_exam_number INTEGER NOT NULL,
                    label VARCHAR(300) NOT NULL,
                    label_key VARCHAR(300) NOT NULL,
                    prompt TEXT NOT NULL,
                    state VARCHAR(30) NOT NULL,
                    stage VARCHAR(30) NOT NULL,
                    attempts INTEGER NOT NULL,
                    next_attempt_at VARCHAR(40),
                    diagnostic_source VARCHAR(40),
                    error TEXT,
                    notebook_id VARCHAR(200),
                    raw_response TEXT,
                    draft_payload_json TEXT,
                    published_token VARCHAR(64),
                    supersedes_run_id VARCHAR(36),
                    created_at VARCHAR(40) NOT NULL,
                    updated_at VARCHAR(40) NOT NULL
                )
                """
            )
        )
        connection.execute(
            text(
                """
                CREATE TABLE published_quizzes (
                    token VARCHAR(64) NOT NULL PRIMARY KEY,
                    lecture_id INTEGER,
                    job_id VARCHAR(36),
                    studio_run_id VARCHAR(36),
                    destination_subject VARCHAR(100) NOT NULL,
                    destination_subject_key VARCHAR(100) NOT NULL,
                    destination_exam_number INTEGER NOT NULL,
                    label VARCHAR(300) NOT NULL,
                    label_key VARCHAR(300) NOT NULL,
                    title VARCHAR(300) NOT NULL,
                    payload_json TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    active BOOLEAN NOT NULL,
                    created_at VARCHAR(40) NOT NULL,
                    updated_at VARCHAR(40) NOT NULL
                )
                """
            )
        )
        connection.execute(
            text(
                """
                INSERT INTO published_quizzes (
                    token, lecture_id, studio_run_id, destination_subject,
                    destination_subject_key, destination_exam_number, label,
                    label_key, title, payload_json, version, active, created_at, updated_at
                ) VALUES
                    ('lecture-token', 1, NULL, 'Neuro', 'neuro', 1, 'Lecture',
                     'lecture', 'Lecture', '{"title":"Lecture","questions":[]}', 1, 1,
                     '2026-08-05T12:00:00+00:00', '2026-08-05T12:00:00+00:00'),
                    ('studio-token', NULL, 'studio-run', 'Neuro', 'neuro', 1, 'Studio',
                     'studio', 'Studio', '{"title":"Studio","questions":[]}', 1, 1,
                     '2026-08-05T12:00:00+00:00', '2026-08-05T12:00:00+00:00')
                """
            )
        )
    return database


def test_v15_migration_backfills_existing_quiz_and_studio_rows_idempotently(
    tmp_path: Path,
) -> None:
    database = _v14_database(tmp_path)

    database.migrate()
    database.migrate()

    with database.session() as session:
        lecture_quiz = session.get(PublishedQuizModel, "lecture-token")
        studio_quiz = session.get(PublishedQuizModel, "studio-token")
        assert lecture_quiz is not None
        assert studio_quiz is not None
        assert lecture_quiz.content_kind == "lecture_quiz"
        assert studio_quiz.content_kind == "exam_review"
        assert lecture_quiz.display_order == 0
        assert studio_quiz.display_order == 0
    names = set(inspect(database.engine).get_table_names())
    assert {
        "studio_import_run_sources",
        "studio_run_artifacts",
        "studio_question_reviews",
    } <= names
    run_columns = {
        column["name"] for column in inspect(database.engine).get_columns("studio_runs")
    }
    assert "history_hidden_at" in run_columns


def test_active_label_migration_repairs_legacy_duplicates_before_index(tmp_path: Path) -> None:
    database = _v14_database(tmp_path)
    with database.engine.begin() as connection:
        connection.execute(text(
            "INSERT INTO studio_runs (id, subject, subject_key, exam_number, "
            "destination_subject, destination_subject_key, destination_exam_number, "
            "label, label_key, prompt, state, stage, attempts, created_at, updated_at) VALUES "
            "('first', 'Neuro', 'neuro', 1, 'Neuro', 'neuro', 1, 'Duplicate', 'duplicate', "
            "'', 'queued', 'validate', 0, '2026-01-01T00:00:00+00:00', "
            "'2026-01-01T00:00:00+00:00'), "
            "('later', 'Neuro', 'neuro', 1, 'Neuro', 'neuro', 1, 'Duplicate', 'duplicate', "
            "'', 'running', 'chat', 0, '2026-01-02T00:00:00+00:00', '2026-01-02T00:00:00+00:00')"
        ))
    database.migrate()
    database.migrate()
    with database.engine.connect() as connection:
        rows = connection.execute(text(
            "SELECT id, state, diagnostic_source, error FROM studio_runs "
            "WHERE id IN ('first', 'later') ORDER BY id"
        )).all()
        index_names = {
            index[1]
            for index in connection.execute(text("PRAGMA index_list('studio_runs')"))
        }
    assert rows == [
        ("first", "queued", None, None),
        ("later", "failed", "migration", "migration active-label conflict; retained run first"),
    ]
    assert "ix_studio_runs_active_label" in index_names
