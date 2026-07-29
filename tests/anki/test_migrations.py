import sqlite3

from sqlalchemy import inspect, text

from oms_hub.db import Database
from oms_hub.migrations import LATEST_SCHEMA_VERSION

APPROVED_ANKI_TABLES = {
    "anki_curation_instructions",
    "anki_curation_jobs",
    "anki_job_stages",
    "anki_candidates",
    "anki_gap_cards",
    "anki_verdict_cache",
    "anki_envelopes",
    "anki_envelope_operations",
    "anki_stage_settings",
}


def _create_schema_v3_database(path: str) -> None:
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE schema_version (
            id INTEGER PRIMARY KEY,
            version INTEGER NOT NULL,
            updated_at VARCHAR(40) NOT NULL
        );
        INSERT INTO schema_version (id, version, updated_at)
        VALUES (1, 3, '2026-07-27T00:00:00+00:00');

        CREATE TABLE lectures (
            id INTEGER PRIMARY KEY,
            subject VARCHAR(100) NOT NULL,
            exam_number INTEGER NOT NULL,
            lecture_number INTEGER NOT NULL,
            topic VARCHAR(300) NOT NULL,
            lecturer VARCHAR(300) NOT NULL,
            exam_date VARCHAR(10),
            scheduled_start_utc VARCHAR(40),
            campus VARCHAR(20),
            created_at VARCHAR(40) NOT NULL,
            updated_at VARCHAR(40) NOT NULL,
            UNIQUE (subject, exam_number, lecture_number)
        );
        INSERT INTO lectures (
            id, subject, exam_number, lecture_number, topic, lecturer,
            created_at, updated_at
        ) VALUES (
            7, 'Heme Lymph', 1, 4, 'Anemia I', 'Professor',
            '2026-07-27T00:00:00+00:00', '2026-07-27T00:00:00+00:00'
        );

        CREATE TABLE study_usage (
            id INTEGER PRIMARY KEY,
            revision_id INTEGER NOT NULL UNIQUE,
            provider VARCHAR(30) NOT NULL DEFAULT 'openai',
            model VARCHAR(100) NOT NULL,
            request_id VARCHAR(200) NOT NULL,
            input_tokens INTEGER NOT NULL,
            output_tokens INTEGER NOT NULL,
            cost_microusd INTEGER NOT NULL,
            created_at VARCHAR(40) NOT NULL
        );

        CREATE TABLE llm_provider_settings (
            provider VARCHAR(30) PRIMARY KEY,
            model VARCHAR(200) NOT NULL,
            active BOOLEAN NOT NULL,
            last_test_state VARCHAR(30),
            last_tested_at VARCHAR(40),
            diagnostic_source VARCHAR(40),
            diagnostic_message TEXT,
            http_status INTEGER,
            provider_request_id VARCHAR(200),
            updated_at VARCHAR(40) NOT NULL
        );
        INSERT INTO llm_provider_settings (
            provider, model, active, updated_at
        ) VALUES (
            'anthropic', 'claude-sonnet-5', 1, '2026-07-27T00:00:00+00:00'
        );
        """
    )
    connection.commit()
    connection.close()


def test_schema_v3_upgrade_preserves_rows_and_adds_anki_tables(tmp_path) -> None:
    database_path = tmp_path / "hub-v3.db"
    _create_schema_v3_database(str(database_path))
    database = Database(f"sqlite:///{database_path}")

    database.migrate()

    tables = set(inspect(database.engine).get_table_names())
    assert APPROVED_ANKI_TABLES <= tables
    assert "anki_agent_commands" not in tables
    assert "anki_agent_state" not in tables
    with database.session() as session:
        lecture = session.execute(
            text("SELECT subject, topic FROM lectures WHERE id = 7")
        ).one()
        provider = session.execute(
            text(
                "SELECT provider, model FROM llm_provider_settings "
                "WHERE provider = 'anthropic'"
            )
        ).one()
        version = session.execute(
            text("SELECT version FROM schema_version WHERE id = 1")
        ).scalar_one()
    assert lecture == ("Heme Lymph", "Anemia I")
    assert provider == ("anthropic", "claude-sonnet-5")
    assert version == LATEST_SCHEMA_VERSION


def test_anki_migration_is_repeatable(tmp_path) -> None:
    database = Database(f"sqlite:///{tmp_path / 'hub.db'}")

    database.migrate()
    database.migrate()

    with database.session() as session:
        assert session.execute(
            text("SELECT COUNT(*) FROM schema_version")
        ).scalar_one() == 1
        assert session.execute(
            text("SELECT COUNT(*) FROM anki_stage_settings")
        ).scalar_one() == 0


def test_schema_upgrade_retires_only_disposable_agent_tables(tmp_path) -> None:
    database_path = tmp_path / "hub-v6.db"
    database = Database(f"sqlite:///{database_path}")
    database.migrate()
    with database.engine.begin() as connection:
        connection.execute(
            text(
                "CREATE TABLE IF NOT EXISTS anki_agent_state "
                "(id INTEGER PRIMARY KEY)"
            )
        )
        connection.execute(
            text(
                "CREATE TABLE IF NOT EXISTS anki_agent_commands "
                "(id VARCHAR(36) PRIMARY KEY)"
            )
        )
        connection.execute(
            text("UPDATE schema_version SET version = 6 WHERE id = 1")
        )

    database.migrate()

    tables = set(inspect(database.engine).get_table_names())
    assert "anki_agent_commands" not in tables
    assert "anki_agent_state" not in tables
    assert "anki_curation_jobs" in tables
    assert "anki_envelopes" in tables
