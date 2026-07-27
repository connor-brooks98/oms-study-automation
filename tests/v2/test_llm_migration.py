import sqlite3

from sqlalchemy import text

from oms_hub.db import Database
from oms_hub.migrations import LATEST_SCHEMA_VERSION


def test_v2_database_migration_attributes_existing_usage_to_openai(tmp_path):
    database_path = tmp_path / "legacy-v2.db"
    connection = sqlite3.connect(database_path)
    connection.executescript(
        """
        CREATE TABLE schema_version (
            id INTEGER PRIMARY KEY,
            version INTEGER NOT NULL,
            updated_at VARCHAR(40) NOT NULL
        );
        INSERT INTO schema_version (id, version, updated_at)
        VALUES (1, 2, '2026-07-25T00:00:00+00:00');

        CREATE TABLE study_usage (
            id INTEGER PRIMARY KEY,
            revision_id INTEGER NOT NULL UNIQUE,
            model VARCHAR(100) NOT NULL,
            request_id VARCHAR(200) NOT NULL,
            input_tokens INTEGER NOT NULL,
            output_tokens INTEGER NOT NULL,
            cost_microusd INTEGER NOT NULL,
            created_at VARCHAR(40) NOT NULL
        );
        INSERT INTO study_usage (
            id, revision_id, model, request_id, input_tokens,
            output_tokens, cost_microusd, created_at
        ) VALUES (
            1, 1, 'gpt-5.1', 'request-1', 10, 5, 100,
            '2026-07-25T00:00:00+00:00'
        );
        """
    )
    connection.commit()
    connection.close()

    database = Database(f"sqlite:///{database_path}")
    database.migrate()

    with database.session() as session:
        provider = session.execute(
            text("SELECT provider FROM study_usage WHERE id = 1")
        ).scalar_one()
        version = session.execute(
            text("SELECT version FROM schema_version WHERE id = 1")
        ).scalar_one()

    assert provider == "openai"
    assert version == LATEST_SCHEMA_VERSION
