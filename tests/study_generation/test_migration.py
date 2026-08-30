from pathlib import Path

import pytest
from sqlalchemy import inspect, text

from oms_hub.db import Database
from oms_hub.migrations import LATEST_SCHEMA_VERSION
from oms_hub.models import LectureModel, PublishedQuizModel
from oms_hub.repositories import CatalogRepository, LectureInput


def test_latest_schema_adds_native_quiz_and_notebook_source_registry(tmp_path):
    database = Database(f"sqlite:///{tmp_path / 'hub.db'}")
    database.migrate()

    names = set(inspect(database.engine).get_table_names())
    assert {
        "google_connection",
        "study_prompt_settings",
        "notebook_mappings",
        "notebook_scope_leases",
        "notebook_source_mappings",
        "course_quiz_documents",
        "exam_quiz_tabs",
        "generation_jobs",
        "outline_outputs",
        "quiz_outputs",
        "published_quizzes",
        "published_quiz_flags",
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
    operation_columns = {
        column["name"]
        for column in inspect(database.engine).get_columns("studio_source_operations")
    }
    assert {
        "lease_owner",
        "lease_expires_at",
        "subject_key",
        "exam_number",
    } <= operation_columns
    operation_indexes = {
        index["name"]
        for index in inspect(database.engine).get_indexes("studio_source_operations")
    }
    assert "ix_studio_source_operations_scope_active" in operation_indexes
    revision_indexes = {
        index["name"]
        for index in inspect(database.engine).get_indexes("study_revisions")
    }
    assert "uq_study_revisions_transcript_cleaning_lecture" in revision_indexes
    with database.session() as session:
        version = session.execute(
            text("SELECT version FROM schema_version WHERE id = 1")
        ).scalar_one()
    assert version == LATEST_SCHEMA_VERSION


def test_reconciles_the_historical_schema_29_without_losing_its_version(tmp_path):
    database = Database(f"sqlite:///{tmp_path / 'hub.db'}")
    database.migrate()
    with database.engine.begin() as connection:
        connection.execute(text("UPDATE schema_version SET version=29 WHERE id=1"))
        connection.execute(text("DROP TABLE notebook_scope_leases"))
        connection.execute(text("DROP TABLE published_quiz_flags"))
        connection.execute(text("DROP TABLE studio_source_operations"))
        connection.execute(text("ALTER TABLE studio_sources DROP COLUMN import_role"))
        connection.execute(
            text("ALTER TABLE studio_sources DROP COLUMN import_attach_to_notebook")
        )

    database.migrate()
    inspector = inspect(database.engine)

    assert {
        "notebook_scope_leases",
        "published_quiz_flags",
        "studio_source_operations",
    } <= set(inspector.get_table_names())
    assert {"import_attach_to_notebook", "import_role"} <= {
        column["name"] for column in inspector.get_columns("studio_sources")
    }
    with database.engine.connect() as connection:
        assert connection.execute(
            text("SELECT version FROM schema_version WHERE id=1")
        ).scalar_one() == LATEST_SCHEMA_VERSION


def test_v30_creates_and_backfills_lecture_passes_idempotently(tmp_path) -> None:
    database = Database(f"sqlite:///{tmp_path / 'hub.db'}")
    database.migrate()
    first_id = CatalogRepository(database).upsert_lecture(
        LectureInput("Neuro", 1, 1, "Brain", "", None)
    )
    second_id = CatalogRepository(database).upsert_lecture(
        LectureInput("Neuro", 1, 2, "Spine", "", None)
    )
    with database.engine.begin() as connection:
        if inspect(database.engine).has_table("lecture_passes"):
            connection.execute(text("DROP TABLE lecture_passes"))
        connection.execute(text("UPDATE schema_version SET version=29 WHERE id=1"))

    database.migrate()
    database.migrate()

    inspector = inspect(database.engine)
    assert inspector.has_table("lecture_passes")
    assert {"lecture_id", "position", "completed_on", "resource"} <= {
        column["name"] for column in inspector.get_columns("lecture_passes")
    }
    with database.engine.connect() as connection:
        rows = connection.execute(
            text(
                "SELECT lecture_id, position, completed_on, resource "
                "FROM lecture_passes ORDER BY lecture_id, position"
            )
        ).all()
        version = connection.execute(
            text("SELECT version FROM schema_version WHERE id=1")
        ).scalar_one()

    assert rows == [
        (lecture_id, position, None, None)
        for lecture_id in (first_id, second_id)
        for position in range(1, 6)
    ]
    assert version == 30


def test_claimed_v30_missing_lecture_passes_fails_closed(tmp_path) -> None:
    database = Database(f"sqlite:///{tmp_path / 'hub.db'}")
    database.migrate()
    with database.engine.begin() as connection:
        connection.execute(text("DROP TABLE lecture_passes"))

    with pytest.raises(RuntimeError, match="schema v30 is missing lecture passes"):
        database.migrate()

    assert not inspect(database.engine).has_table("lecture_passes")


def test_claimed_v30_missing_v29_reconciliation_fails_closed(tmp_path) -> None:
    database = Database(f"sqlite:///{tmp_path / 'hub.db'}")
    database.migrate()
    with database.engine.begin() as connection:
        connection.execute(text("DROP TABLE notebook_scope_leases"))

    with pytest.raises(RuntimeError, match="schema v30 v29 reconciliation is incomplete"):
        database.migrate()

    assert not inspect(database.engine).has_table("notebook_scope_leases")


def test_reconciles_the_deployed_non_anki_schema_23_without_losing_data(tmp_path):
    database = Database(f"sqlite:///{tmp_path / 'hub.db'}")
    database.migrate()
    lecture_id = CatalogRepository(database).upsert_lecture(
        LectureInput("Neuro", 1, 7, "Brainstem", "", None)
    )
    with database.engine.begin() as connection:
        connection.execute(text("DROP TABLE outline_replacement_reviews"))
        connection.execute(text("DROP TABLE existing_artifact_imports"))
        connection.execute(text("UPDATE schema_version SET version=23 WHERE id=1"))

    database.migrate()
    inspector = inspect(database.engine)

    assert {"existing_artifact_imports", "outline_replacement_reviews"} <= set(
        inspector.get_table_names()
    )
    with database.session() as session:
        lecture = session.get(LectureModel, lecture_id)
        assert lecture is not None
        assert lecture.topic == "Brainstem"
        assert session.execute(
            text("SELECT version FROM schema_version WHERE id=1")
        ).scalar_one() == LATEST_SCHEMA_VERSION


def test_v23_adds_public_question_flags_from_v22_idempotently(tmp_path: Path) -> None:
    database = Database(f"sqlite:///{tmp_path / 'hub.db'}")
    database.migrate()
    with database.engine.begin() as connection:
        connection.execute(text("DROP TABLE published_quiz_flags"))
        connection.execute(text("UPDATE schema_version SET version=22 WHERE id=1"))

    database.migrate()
    database.migrate()

    inspector = inspect(database.engine)
    assert inspector.has_table("published_quiz_flags")
    assert {
        "quiz_token",
        "quiz_version",
        "question_id",
        "reason",
        "occurrence_count",
        "status",
        "created_at",
        "updated_at",
    } <= {column["name"] for column in inspector.get_columns("published_quiz_flags")}
    assert "ix_published_quiz_flags_open" in {
        index["name"] for index in inspector.get_indexes("published_quiz_flags")
    }
    with database.engine.connect() as connection:
        version = connection.execute(
            text("SELECT version FROM schema_version WHERE id=1")
        ).scalar_one()
    assert version == LATEST_SCHEMA_VERSION


def test_v22_reservation_indexes_and_operation_scope_upgrade_idempotently(
    tmp_path: Path,
) -> None:
    database = Database(f"sqlite:///{tmp_path / 'hub.db'}")
    database.migrate()
    with database.engine.begin() as connection:
        connection.execute(
            text("DROP INDEX uq_study_revisions_transcript_cleaning_lecture")
        )
        connection.execute(text("DROP INDEX ix_studio_source_operations_scope_active"))
        connection.execute(text("ALTER TABLE studio_source_operations DROP COLUMN lease_owner"))
        connection.execute(
            text("ALTER TABLE studio_source_operations DROP COLUMN lease_expires_at")
        )
        connection.execute(
            text("ALTER TABLE studio_source_operations DROP COLUMN subject_key")
        )
        connection.execute(
            text("ALTER TABLE studio_source_operations DROP COLUMN exam_number")
        )
        connection.execute(text("DROP TABLE notebook_scope_leases"))
        connection.execute(text("UPDATE schema_version SET version=20 WHERE id=1"))

    database.migrate()
    database.migrate()

    with database.engine.connect() as connection:
        index_sql = connection.execute(
            text(
                "SELECT sql FROM sqlite_master WHERE type='index' AND name="
                "'uq_study_revisions_transcript_cleaning_lecture'"
            )
        ).scalar_one()
    assert "UNIQUE INDEX" in index_sql
    assert "ON study_revisions(lecture_id)" in index_sql
    assert "kind='transcripts' AND state='cleaning'" in index_sql
    operation_columns = {
        column["name"]
        for column in inspect(database.engine).get_columns("studio_source_operations")
    }
    assert {
        "lease_owner",
        "lease_expires_at",
        "subject_key",
        "exam_number",
    } <= operation_columns
    operation_indexes = {
        index["name"]
        for index in inspect(database.engine).get_indexes("studio_source_operations")
    }
    assert "ix_studio_source_operations_scope_active" in operation_indexes
    assert inspect(database.engine).has_table("notebook_scope_leases")


def test_v21_scope_upgrade_fails_closed_on_duplicate_active_operations(
    tmp_path: Path,
) -> None:
    database = Database(f"sqlite:///{tmp_path / 'hub.db'}")
    database.migrate()
    with database.engine.begin() as connection:
        connection.execute(text("DROP INDEX ix_studio_source_operations_scope_active"))
        connection.execute(
            text(
                "INSERT INTO studio_sources "
                "(id, subject, subject_key, exam_number, source_type, title, purpose, "
                "import_attach_to_notebook, state, attempts, converted_from_pptx, "
                "created_at, updated_at) VALUES "
                "('source-1', 'Neuro', 'neuro', 1, 'text', 'First', 'notebook', "
                "0, 'attaching', 1, 0, '2026-08-09T00:00:00+00:00', "
                "'2026-08-09T00:00:00+00:00'), "
                "('source-2', 'Neuro', 'neuro', 1, 'text', 'Second', 'notebook', "
                "0, 'attaching', 1, 0, '2026-08-09T00:00:01+00:00', "
                "'2026-08-09T00:00:01+00:00')"
            )
        )
        connection.execute(
            text(
                "INSERT INTO studio_source_operations "
                "(id, source_id, operation_kind, state, subject_key, exam_number, "
                "baseline_remote_ids_json, attempts, created_at, updated_at) VALUES "
                "('operation-1', 'source-1', 'add', 'queued', 'neuro', 1, '[]', 0, "
                "'2026-08-09T00:00:00+00:00', '2026-08-09T00:00:00+00:00'), "
                "('operation-2', 'source-2', 'add', 'queued', 'neuro', 1, '[]', 0, "
                "'2026-08-09T00:00:01+00:00', '2026-08-09T00:00:01+00:00')"
            )
        )
        connection.execute(text("UPDATE schema_version SET version=20 WHERE id=1"))

    database.migrate()
    database.migrate()

    with database.engine.connect() as connection:
        operations = connection.execute(
            text("SELECT id, state FROM studio_source_operations ORDER BY id")
        ).all()
        second_source = connection.execute(
            text(
                "SELECT state, diagnostic_source, error FROM studio_sources "
                "WHERE id='source-2'"
            )
        ).one()
    assert operations == [("operation-1", "queued"), ("operation-2", "needs_review")]
    assert second_source.state == "needs_review"
    assert second_source.diagnostic_source == "migration"
    assert "retained operation operation-1" in second_source.error


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


def test_active_label_migration_retains_the_single_active_publication_owner(
    tmp_path: Path,
) -> None:
    database = _v14_database(tmp_path)
    with database.engine.begin() as connection:
        connection.execute(text(
            "INSERT INTO studio_runs (id, subject, subject_key, exam_number, "
            "destination_subject, destination_subject_key, destination_exam_number, "
            "label, label_key, prompt, state, stage, attempts, created_at, updated_at) VALUES "
            "('earlier', 'Neuro', 'neuro', 1, 'Neuro', 'neuro', 1, 'Duplicate', "
            "'duplicate', '', 'queued', 'validate', 0, "
            "'2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00'), "
            "('publication-owner', 'Neuro', 'neuro', 1, 'Neuro', 'neuro', 1, "
            "'Duplicate', 'duplicate', '', 'running', 'publish', 1, "
            "'2026-01-02T00:00:00+00:00', '2026-01-02T00:00:00+00:00')"
        ))
        connection.execute(text(
            "INSERT INTO published_quizzes (token, lecture_id, job_id, studio_run_id, "
            "destination_subject, destination_subject_key, destination_exam_number, label, "
            "label_key, title, payload_json, version, active, created_at, updated_at) VALUES "
            "('owner-token', NULL, NULL, 'publication-owner', 'Neuro', 'neuro', 1, "
            "'Duplicate', 'duplicate', 'Duplicate', '{\"title\":\"Duplicate\","
            "\"questions\":[]}', 1, 1, '2026-01-02T00:00:00+00:00', "
            "'2026-01-02T00:00:00+00:00')"
        ))

    database.migrate()
    with database.engine.connect() as connection:
        first_pass = connection.execute(text(
            "SELECT id, state, diagnostic_source, error FROM studio_runs "
            "WHERE id IN ('earlier', 'publication-owner') ORDER BY id"
        )).all()
    database.migrate()
    with database.engine.connect() as connection:
        second_pass = connection.execute(text(
            "SELECT id, state, diagnostic_source, error FROM studio_runs "
            "WHERE id IN ('earlier', 'publication-owner') ORDER BY id"
        )).all()

    assert first_pass == second_pass == [
        (
            "earlier",
            "failed",
            "migration",
            "migration active-label conflict; retained active publication owner "
            "publication-owner",
        ),
        ("publication-owner", "running", None, None),
    ]


def test_active_label_migration_fails_closed_on_multiple_active_publications(
    tmp_path: Path,
) -> None:
    database = _v14_database(tmp_path)
    with database.engine.begin() as connection:
        connection.execute(text(
            "INSERT INTO studio_runs (id, subject, subject_key, exam_number, "
            "destination_subject, destination_subject_key, destination_exam_number, "
            "label, label_key, prompt, state, stage, attempts, created_at, updated_at) VALUES "
            "('first-owner', 'Neuro', 'neuro', 1, 'Neuro', 'neuro', 1, 'Duplicate', "
            "'duplicate', '', 'queued', 'validate', 0, "
            "'2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00'), "
            "('second-owner', 'Neuro', 'neuro', 1, 'Neuro', 'neuro', 1, 'Duplicate', "
            "'duplicate', '', 'running', 'publish', 1, "
            "'2026-01-02T00:00:00+00:00', '2026-01-02T00:00:00+00:00')"
        ))
        connection.execute(text(
            "INSERT INTO published_quizzes (token, lecture_id, job_id, studio_run_id, "
            "destination_subject, destination_subject_key, destination_exam_number, label, "
            "label_key, title, payload_json, version, active, created_at, updated_at) VALUES "
            "('first-owner-token', NULL, NULL, 'first-owner', 'Neuro', 'neuro', 1, "
            "'Duplicate', 'duplicate', 'First', '{}', 1, 1, "
            "'2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00'), "
            "('second-owner-token', NULL, NULL, 'second-owner', 'Neuro', 'neuro', 1, "
            "'Duplicate', 'duplicate', 'Second', '{}', 1, 1, "
            "'2026-01-02T00:00:00+00:00', '2026-01-02T00:00:00+00:00')"
        ))

    with pytest.raises(
        RuntimeError,
        match=(
            "migration recovery conflict: multiple active Studio publications exist "
            "for neuro exam 1 label duplicate"
        ),
    ):
        database.migrate()
