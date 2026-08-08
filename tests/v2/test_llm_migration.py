import sqlite3

from sqlalchemy import select, text

from oms_hub.db import Database
from oms_hub.llm.catalog import FALLBACK_MODELS
from oms_hub.llm.domain import LLMTask, ProviderName
from oms_hub.llm.repository import DEFAULT_MODELS
from oms_hub.migrations import LATEST_SCHEMA_VERSION
from oms_hub.models import LLMTaskAssignmentModel


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


def test_fresh_database_seeds_task_assignments_with_anthropic_defaults(tmp_path):
    database = Database(f"sqlite:///{tmp_path / 'fresh.db'}")

    database.migrate()

    with database.session() as session:
        rows = {
            row.task: (row.provider, row.model)
            for row in session.scalars(select(LLMTaskAssignmentModel)).all()
        }

    assert rows[LLMTask.TRANSCRIPTS.value] == (
        ProviderName.ANTHROPIC.value,
        DEFAULT_MODELS[ProviderName.ANTHROPIC],
    )
    assert rows[LLMTask.ANKI_CURATION.value] == (
        ProviderName.ANTHROPIC.value,
        DEFAULT_MODELS[ProviderName.ANTHROPIC],
    )
    assert rows[LLMTask.ACCURACY_REVIEW.value] == (
        ProviderName.OPENROUTER.value,
        FALLBACK_MODELS[ProviderName.OPENROUTER][0],
    )
    assert rows[LLMTask.QUIZ_EXTRACTION.value] == (
        ProviderName.OPENAI.value,
        DEFAULT_MODELS[ProviderName.OPENAI],
    )
    assert rows[LLMTask.QUIZ_ANSWER_GENERATION.value] == (
        ProviderName.OPENAI.value,
        DEFAULT_MODELS[ProviderName.OPENAI],
    )


def test_task_assignments_seed_from_active_provider_and_openrouter_model(tmp_path):
    database_path = tmp_path / "legacy-with-active-provider.db"
    connection = sqlite3.connect(database_path)
    connection.executescript(
        """
        CREATE TABLE llm_provider_settings (
            provider VARCHAR(30) NOT NULL PRIMARY KEY,
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
        ) VALUES
            ('openai', 'gpt-5.2', 0, '2026-07-25T00:00:00+00:00'),
            ('gemini', 'gemini-3.6-flash', 1, '2026-07-25T00:00:00+00:00'),
            ('anthropic', 'claude-sonnet-5', 0, '2026-07-25T00:00:00+00:00');

        CREATE TABLE study_ai_settings (
            id INTEGER PRIMARY KEY,
            openrouter_model VARCHAR(200) NOT NULL,
            accuracy_gate_enabled BOOLEAN NOT NULL DEFAULT 0,
            updated_at VARCHAR(40) NOT NULL
        );
        INSERT INTO study_ai_settings (
            id, openrouter_model, accuracy_gate_enabled, updated_at
        ) VALUES (
            1, 'anthropic/claude-3.5-sonnet', 0, '2026-07-25T00:00:00+00:00'
        );
        """
    )
    connection.commit()
    connection.close()

    database = Database(f"sqlite:///{database_path}")
    database.migrate()
    database.migrate()

    with database.session() as session:
        rows = {
            row.task: (row.provider, row.model)
            for row in session.scalars(select(LLMTaskAssignmentModel)).all()
        }

    assert rows[LLMTask.TRANSCRIPTS.value] == ("gemini", "gemini-3.6-flash")
    assert rows[LLMTask.ANKI_CURATION.value] == ("gemini", "gemini-3.6-flash")
    assert rows[LLMTask.ACCURACY_REVIEW.value] == (
        "openrouter",
        "anthropic/claude-3.5-sonnet",
    )
    assert rows[LLMTask.QUIZ_EXTRACTION.value] == (
        "openai",
        DEFAULT_MODELS[ProviderName.OPENAI],
    )
    assert rows[LLMTask.QUIZ_ANSWER_GENERATION.value] == (
        "openai",
        DEFAULT_MODELS[ProviderName.OPENAI],
    )
    assert len(rows) == 5
