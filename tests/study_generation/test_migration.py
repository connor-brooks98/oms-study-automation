import sqlite3

from sqlalchemy import inspect, text

from oms_hub.db import Database
from oms_hub.migrations import LATEST_SCHEMA_VERSION


def test_v9_published_lecture_quiz_is_backfilled_with_destination(tmp_path):
    database = Database(f"sqlite:///{tmp_path / 'backfill.db'}")
    database.create_schema()
    with database.engine.begin() as connection:
        connection.execute(text("DROP TABLE published_quizzes"))
        connection.execute(
            text(
                """CREATE TABLE published_quizzes (
                token VARCHAR(64) PRIMARY KEY, lecture_id INTEGER NOT NULL UNIQUE,
                job_id VARCHAR(36) NOT NULL UNIQUE, title VARCHAR(300) NOT NULL,
                payload_json TEXT NOT NULL, version INTEGER NOT NULL,
                created_at VARCHAR(40) NOT NULL, updated_at VARCHAR(40) NOT NULL)"""
            )
        )
        connection.execute(
            text(
                "INSERT INTO lectures (id, subject, exam_number, lecture_number, topic, "
                "lecturer, created_at, updated_at) VALUES "
                "(1, 'Neuro', 2, 3, 'CNS', '', 'now', 'now')"
            )
        )
        connection.execute(
            text(
                "INSERT INTO generation_jobs "
                "(id, lecture_id, kind, state, stage, attempts, created_at, updated_at) "
                "VALUES ('job-1', 1, 'quiz', 'complete', 'complete', 1, 'now', 'now')"
            )
        )
        payload = (
            '{"title":"Legacy","questions":[{"stem":"Q?","choices":["A","B"],'
            '"correct_index":0,"rationale":"Because."}]}'
        )
        connection.execute(
            text(
                "INSERT INTO published_quizzes VALUES "
                "(:token, 1, 'job-1', 'Legacy', :payload, 1, 'now', 'now')"
            ),
            {"token": "a" * 64, "payload": payload},
        )
        connection.execute(
            text("INSERT INTO schema_version (id, version, updated_at) VALUES (1, 9, 'now')")
        )

    database.migrate()

    with database.engine.connect() as connection:
        row = connection.execute(
            text(
                "SELECT destination_subject, destination_exam_number, studio_run_id, active "
                "FROM published_quizzes"
            )
        ).one()
    assert tuple(row) == ("Neuro", 2, None, 1)
    database.close()


def test_schema_v11_adds_native_quiz_studio_jobs_and_image_review(tmp_path):
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
        "studio_runs",
        "studio_run_sources",
        "studio_run_attempts",
        "studio_quiz_image_requirements",
        "studio_quiz_image_overrides",
        "published_quiz_media",
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
    run_indexes = {index["name"] for index in inspect(database.engine).get_indexes("studio_runs")}
    assert {
        "ix_studio_runs_poll",
        "ix_studio_runs_scope",
        "ix_studio_runs_supersedes",
    } <= run_indexes
    run_columns = {
        column["name"] for column in inspect(database.engine).get_columns("studio_runs")
    }
    assert "draft_payload_json" in run_columns
    published_columns = {
        column["name"] for column in inspect(database.engine).get_columns("published_quizzes")
    }
    assert {
        "studio_run_id",
        "destination_subject_key",
        "destination_exam_number",
        "label_key",
        "active",
    } <= published_columns
    published_indexes = {
        index["name"]
        for index in inspect(database.engine).get_indexes("published_quizzes")
    }
    assert {
        "uq_published_lecture_origin",
        "uq_published_studio_label",
    } <= published_indexes
    assert version == LATEST_SCHEMA_VERSION == 11


def test_schema_v11_repairs_dangling_published_media_foreign_key(tmp_path):
    path = tmp_path / "broken-media.db"
    database = Database(f"sqlite:///{path}")
    database.create_schema()
    payload = (
        '{"title":"Legacy","questions":[{"stem":"Q?","choices":["A","B"],'
        '"correct_index":0,"rationale":"Because."}]}'
    )
    token = "a" * 64
    with database.engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO lectures (id, subject, exam_number, lecture_number, topic, "
                "lecturer, created_at, updated_at) VALUES "
                "(1, 'Neuro', 2, 3, 'CNS', '', 'now', 'now')"
            )
        )
        connection.execute(
            text(
                "INSERT INTO generation_jobs "
                "(id, lecture_id, kind, state, stage, attempts, created_at, updated_at) "
                "VALUES ('job-1', 1, 'quiz', 'complete', 'complete', 1, 'now', 'now')"
            )
        )
        connection.execute(
            text(
                "INSERT INTO published_quizzes "
                "(token, lecture_id, job_id, studio_run_id, destination_subject, "
                "destination_subject_key, destination_exam_number, label, label_key, "
                "title, payload_json, version, active, created_at, updated_at) VALUES "
                "(:token, 1, 'job-1', NULL, 'Neuro', 'neuro', 2, 'Legacy', 'legacy', "
                "'Legacy', :payload, 1, 1, 'now', 'now')"
            ),
            {"token": token, "payload": payload},
        )
        connection.execute(
            text("INSERT INTO schema_version (id, version, updated_at) VALUES (1, 11, 'now')")
        )
    database.close()

    raw = sqlite3.connect(path)
    try:
        raw.executescript(
            """
            DROP TABLE published_quiz_media;
            CREATE TABLE published_quiz_media (
                id INTEGER PRIMARY KEY,
                quiz_token VARCHAR(64) NOT NULL REFERENCES published_quizzes_v9(token),
                image_key VARCHAR(64) NOT NULL,
                path TEXT NOT NULL,
                sha256 VARCHAR(64) NOT NULL,
                media_type VARCHAR(50) NOT NULL,
                width INTEGER NOT NULL,
                height INTEGER NOT NULL,
                alt_text VARCHAR(1000) NOT NULL,
                created_at VARCHAR(40) NOT NULL,
                UNIQUE (quiz_token, image_key)
            );
            """
        )
        raw.execute(
            "INSERT INTO published_quiz_media "
            "(quiz_token, image_key, path, sha256, media_type, width, height, alt_text, "
            "created_at) VALUES (?, 'image-1', 'image.png', ?, 'image/png', 12, 8, "
            "'Reference image', 'now')",
            (token, "b" * 64),
        )
        raw.commit()
    finally:
        raw.close()

    database = Database(f"sqlite:///{path}")
    database.migrate()

    foreign_keys = inspect(database.engine).get_foreign_keys("published_quiz_media")
    with database.engine.begin() as connection:
        media = connection.execute(
            text(
                "SELECT quiz_token, image_key, path, alt_text "
                "FROM published_quiz_media"
            )
        ).one()
        connection.execute(
            text("DELETE FROM published_quiz_media WHERE quiz_token = :token"),
            {"token": token},
        )
    assert {item["referred_table"] for item in foreign_keys} == {"published_quizzes"}
    assert tuple(media) == (token, "image-1", "image.png", "Reference image")
    database.close()


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
