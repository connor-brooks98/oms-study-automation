import sqlite3
from contextlib import closing

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
    "anki_card_audit_cache",
    "anki_envelopes",
    "anki_envelope_operations",
    "anki_agent_state",
    "anki_stage_settings",
    "anki_source_evidence",
    "anki_stage_artifacts",
    "anki_tag_patches",
}


def _create_schema_v3_database(path: str) -> None:
    with closing(sqlite3.connect(path)) as connection:
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


def test_schema_v3_upgrade_preserves_rows_and_adds_anki_tables(tmp_path) -> None:
    database_path = tmp_path / "hub-v3.db"
    _create_schema_v3_database(str(database_path))
    with Database(f"sqlite:///{database_path}") as database:
        database.migrate()

        tables = set(inspect(database.engine).get_table_names())
        assert APPROVED_ANKI_TABLES <= tables
        assert "anki_agent_commands" in tables
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
    with Database(f"sqlite:///{tmp_path / 'hub.db'}") as database:
        database.migrate()
        database.migrate()

        with database.session() as session:
            assert session.execute(
                text("SELECT COUNT(*) FROM schema_version")
            ).scalar_one() == 1
            assert session.execute(
                text("SELECT COUNT(*) FROM anki_stage_settings")
            ).scalar_one() == 0


def test_schema_v6_upgrade_adds_v4_columns_without_losing_legacy_job(
    tmp_path,
) -> None:
    database_path = tmp_path / "hub-v6.db"
    with closing(sqlite3.connect(database_path)) as connection:
        connection.executescript(
            """
            CREATE TABLE schema_version (
                id INTEGER PRIMARY KEY,
                version INTEGER NOT NULL,
                updated_at VARCHAR(40) NOT NULL
            );
            INSERT INTO schema_version (id, version, updated_at)
            VALUES (1, 6, '2026-07-27T00:00:00+00:00');

            CREATE TABLE anki_curation_jobs (
                id VARCHAR(36) PRIMARY KEY,
                lecture_id INTEGER NOT NULL,
                state VARCHAR(30) NOT NULL,
                attempts INTEGER NOT NULL,
                target_deck TEXT NOT NULL,
                target_tag TEXT NOT NULL,
                index_snapshot_id VARCHAR(200) NOT NULL,
                amboss_input TEXT NOT NULL,
                amboss_sha256 VARCHAR(64) NOT NULL,
                instruction_text TEXT NOT NULL,
                instruction_sha256 VARCHAR(64) NOT NULL,
                lcl_prompt_version VARCHAR(100) NOT NULL,
                judgment_rubric_version VARCHAR(100) NOT NULL,
                gap_prompt_version VARCHAR(100) NOT NULL,
                warnings_json TEXT NOT NULL,
                counts_json TEXT NOT NULL,
                review_revision INTEGER NOT NULL,
                error TEXT,
                started_at VARCHAR(40),
                ready_at VARCHAR(40),
                completed_at VARCHAR(40),
                created_at VARCHAR(40) NOT NULL,
                updated_at VARCHAR(40) NOT NULL
            );
            INSERT INTO anki_curation_jobs (
                id, lecture_id, state, attempts, target_deck, target_tag,
                index_snapshot_id, amboss_input, amboss_sha256,
                instruction_text, instruction_sha256, lcl_prompt_version,
                judgment_rubric_version, gap_prompt_version, warnings_json,
                counts_json, review_revision, created_at, updated_at
            ) VALUES (
                '00000000-0000-0000-0000-000000000001', 7, 'queued', 0,
                'target', 'tag', 'snapshot-1', 'legacy',
                'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',
                '',
                'e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855',
                'lcl-v1', 'judgment-v1', 'gap-v1', '[]', '{}', 0,
                '2026-07-27T00:00:00+00:00',
                '2026-07-27T00:00:00+00:00'
            );
            """
        )
        connection.commit()

    with Database(f"sqlite:///{database_path}") as database:
        database.migrate()
        columns = {
            column["name"]
            for column in inspect(database.engine).get_columns(
                "anki_curation_jobs"
            )
        }
        tables = set(inspect(database.engine).get_table_names())
        with database.session() as session:
            row = session.execute(
                text(
                    "SELECT id, amboss_input, apply_state "
                    "FROM anki_curation_jobs"
                )
            ).one()
            version = session.execute(
                text("SELECT version FROM schema_version WHERE id = 1")
            ).scalar_one()

    assert {
        "source_revision_ids_json",
        "source_revision_hashes_json",
        "summary_outline_id",
        "summary_outline_sha256",
        "deck_allowlist_json",
        "tag_allowlist_json",
        "apply_state",
        "lease_owner",
        "lease_expires_at",
        "available_at",
    } <= columns
    assert {
        "anki_source_evidence",
        "anki_stage_artifacts",
        "anki_tag_patches",
    } <= tables
    assert row == (
        "00000000-0000-0000-0000-000000000001",
        "legacy",
        "pending",
    )
    assert version == LATEST_SCHEMA_VERSION


def test_schema_v10_upgrade_allows_multiple_gap_cards_per_concept(
    tmp_path,
) -> None:
    database_path = tmp_path / "hub-v10.db"
    with closing(sqlite3.connect(database_path)) as connection:
        connection.executescript(
            """
            CREATE TABLE schema_version (
                id INTEGER PRIMARY KEY,
                version INTEGER NOT NULL,
                updated_at VARCHAR(40) NOT NULL
            );
            INSERT INTO schema_version (id, version, updated_at)
            VALUES (1, 10, '2026-07-30T00:00:00+00:00');

            CREATE TABLE anki_curation_jobs (
                id VARCHAR(36) PRIMARY KEY
            );
            INSERT INTO anki_curation_jobs (id) VALUES (
                '00000000-0000-0000-0000-000000000001'
            );

            CREATE TABLE anki_gap_cards (
                id VARCHAR(36) PRIMARY KEY,
                job_id VARCHAR(36) NOT NULL,
                concept_id VARCHAR(200) NOT NULL,
                text TEXT NOT NULL,
                extra TEXT NOT NULL,
                revision INTEGER NOT NULL,
                selected BOOLEAN NOT NULL,
                image_state VARCHAR(30) NOT NULL,
                media_filename TEXT,
                source_note_id INTEGER,
                generated_image_json TEXT NOT NULL,
                validation_state VARCHAR(30) NOT NULL,
                source_refs_json TEXT NOT NULL DEFAULT '[]',
                evidence_ids_json TEXT NOT NULL DEFAULT '[]',
                provenance_json TEXT NOT NULL DEFAULT '{}',
                initial_tags_json TEXT NOT NULL DEFAULT '[]',
                content_hash VARCHAR(64) NOT NULL,
                UNIQUE (job_id, concept_id)
            );
            INSERT INTO anki_gap_cards VALUES (
                '00000000-0000-0000-0000-000000000101',
                '00000000-0000-0000-0000-000000000001',
                'C01', '{{c1::first}}', 'legacy card', 1, 1, 'none',
                NULL, NULL, '{}', 'valid', '[]', '[]', '{}', '[]',
                'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa'
            );
            """
        )
        connection.commit()

    with Database(f"sqlite:///{database_path}") as database:
        database.migrate()
        with database.engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO anki_gap_cards ("
                    "id, job_id, concept_id, text, extra, revision, selected, "
                    "image_state, generated_image_json, validation_state, "
                    "source_refs_json, evidence_ids_json, provenance_json, "
                    "initial_tags_json, content_hash"
                    ") VALUES ("
                    "'00000000-0000-0000-0000-000000000102', "
                    "'00000000-0000-0000-0000-000000000001', "
                    "'C01', '{{c1::second}}', 'split card', 1, 1, 'none', "
                    "'{}', 'valid', '[]', '[]', '{}', '[]', "
                    "'bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb'"
                    ")"
                )
            )
            count = connection.execute(
                text(
                    "SELECT COUNT(*) FROM anki_gap_cards "
                    "WHERE job_id = "
                    "'00000000-0000-0000-0000-000000000001' "
                    "AND concept_id = 'C01'"
                )
            ).scalar_one()

    assert count == 2


def test_gap_card_job_concept_index_is_created_and_migration_is_idempotent(
    tmp_path,
) -> None:
    database_path = tmp_path / "hub-v10-index.db"
    with closing(sqlite3.connect(database_path)) as connection:
        connection.executescript(
            """
            CREATE TABLE schema_version (
                id INTEGER PRIMARY KEY,
                version INTEGER NOT NULL,
                updated_at VARCHAR(40) NOT NULL
            );
            INSERT INTO schema_version (id, version, updated_at)
            VALUES (1, 10, '2026-07-30T00:00:00+00:00');

            CREATE TABLE anki_curation_jobs (
                id VARCHAR(36) PRIMARY KEY
            );
            INSERT INTO anki_curation_jobs (id) VALUES (
                '00000000-0000-0000-0000-000000000001'
            );

            CREATE TABLE anki_gap_cards (
                id VARCHAR(36) PRIMARY KEY,
                job_id VARCHAR(36) NOT NULL,
                concept_id VARCHAR(200) NOT NULL,
                text TEXT NOT NULL,
                extra TEXT NOT NULL,
                revision INTEGER NOT NULL,
                selected BOOLEAN NOT NULL,
                image_state VARCHAR(30) NOT NULL,
                media_filename TEXT,
                source_note_id INTEGER,
                generated_image_json TEXT NOT NULL,
                validation_state VARCHAR(30) NOT NULL,
                source_refs_json TEXT NOT NULL DEFAULT '[]',
                evidence_ids_json TEXT NOT NULL DEFAULT '[]',
                provenance_json TEXT NOT NULL DEFAULT '{}',
                initial_tags_json TEXT NOT NULL DEFAULT '[]',
                content_hash VARCHAR(64) NOT NULL,
                UNIQUE (job_id, concept_id)
            );
            """
        )
        connection.commit()

    with Database(f"sqlite:///{database_path}") as database:
        database.migrate()
        database.migrate()

        index_names = {
            row["name"]
            for row in inspect(database.engine).get_indexes("anki_gap_cards")
        }

    assert "ix_anki_gap_cards_job_concept" in index_names

    with closing(sqlite3.connect(database_path)) as connection:
        rows = connection.execute(
            "PRAGMA index_list('anki_gap_cards')"
        ).fetchall()
        pragma_names = {row[1] for row in rows}

    assert "ix_anki_gap_cards_job_concept" in pragma_names
