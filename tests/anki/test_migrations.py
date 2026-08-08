import sqlite3
from contextlib import closing
from dataclasses import replace
from uuid import UUID

import pytest
from sqlalchemy import inspect, text

from oms_hub.anki.domain import PipelineContractVersion
from oms_hub.anki.pipeline import PinnedInputChanged, StageArtifactStore
from oms_hub.anki.repository import AnkiCurationRepository
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


def test_schema_v19_adds_replay_input_persistence_on_clean_install(tmp_path) -> None:
    with Database(f"sqlite:///{tmp_path / 'hub-v19.db'}") as database:
        database.migrate()
        inspector = inspect(database.engine)
        job_columns = {
            column["name"] for column in inspector.get_columns("anki_curation_jobs")
        }

        assert {
            "lecture_title_snapshot",
            "lecture_metadata_json",
            "lecture_metadata_sha256",
        } <= job_columns
        assert "anki_stage_replay_inputs" in inspector.get_table_names()


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
        "lecture_title_snapshot",
        "lecture_metadata_json",
        "lecture_metadata_sha256",
    } <= columns
    assert {
        "anki_source_evidence",
        "anki_stage_artifacts",
        "anki_stage_replay_inputs",
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


def test_studio_run_active_label_index_is_created_and_migration_is_idempotent(
    tmp_path,
) -> None:
    database_path = tmp_path / "hub-v11-studio-index.db"
    with closing(sqlite3.connect(database_path)) as connection:
        connection.executescript(
            """
            CREATE TABLE schema_version (
                id INTEGER PRIMARY KEY,
                version INTEGER NOT NULL,
                updated_at VARCHAR(40) NOT NULL
            );
            INSERT INTO schema_version (id, version, updated_at)
            VALUES (1, 11, '2026-08-01T00:00:00+00:00');

            CREATE TABLE studio_runs (
                id VARCHAR(36) PRIMARY KEY,
                destination_subject_key VARCHAR(100) NOT NULL DEFAULT '',
                destination_exam_number INTEGER NOT NULL DEFAULT 0,
                label_key VARCHAR(300) NOT NULL DEFAULT '',
                state VARCHAR(30) NOT NULL DEFAULT 'queued'
            );
            """
        )
        connection.commit()

    with Database(f"sqlite:///{database_path}") as database:
        database.migrate()
        database.migrate()

        index_names = {
            row["name"]
            for row in inspect(database.engine).get_indexes("studio_runs")
        }

    assert "ix_studio_runs_active_label" in index_names

    with closing(sqlite3.connect(database_path)) as connection:
        rows = connection.execute("PRAGMA index_list('studio_runs')").fetchall()
        pragma_names = {row[1] for row in rows}

    assert "ix_studio_runs_active_label" in pragma_names


def _create_schema_v12_anki_contract_database(path: str) -> None:
    """Create the actual pre-v13 Anki table shapes, including history."""
    with closing(sqlite3.connect(path)) as connection:
        connection.executescript(
            """
            CREATE TABLE schema_version (
                id INTEGER PRIMARY KEY,
                version INTEGER NOT NULL,
                updated_at VARCHAR(40) NOT NULL
            );
            INSERT INTO schema_version (id, version, updated_at)
            VALUES (1, 12, '2026-08-04T00:00:00+00:00');

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
                '2026-08-04T00:00:00+00:00', '2026-08-04T00:00:00+00:00'
            );

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
                block_id VARCHAR(200),
                source_revision_ids_json TEXT NOT NULL,
                source_revision_hashes_json TEXT NOT NULL,
                summary_outline_id INTEGER,
                summary_outline_sha256 VARCHAR(64),
                deck_allowlist_json TEXT NOT NULL,
                tag_allowlist_json TEXT NOT NULL,
                provider VARCHAR(30) NOT NULL,
                model VARCHAR(200) NOT NULL,
                semantic_generation VARCHAR(200),
                companion_generation VARCHAR(200),
                source_index_generation VARCHAR(200),
                configuration_sha256 VARCHAR(64) NOT NULL,
                apply_state VARCHAR(50) NOT NULL,
                instruction_text TEXT NOT NULL,
                instruction_sha256 VARCHAR(64) NOT NULL,
                lcl_prompt_version VARCHAR(100) NOT NULL,
                judgment_rubric_version VARCHAR(100) NOT NULL,
                gap_prompt_version VARCHAR(100) NOT NULL,
                warnings_json TEXT NOT NULL,
                counts_json TEXT NOT NULL,
                review_revision INTEGER NOT NULL,
                error TEXT,
                lease_owner VARCHAR(100),
                lease_expires_at VARCHAR(40),
                available_at VARCHAR(40),
                started_at VARCHAR(40),
                ready_at VARCHAR(40),
                completed_at VARCHAR(40),
                created_at VARCHAR(40) NOT NULL,
                updated_at VARCHAR(40) NOT NULL,
                FOREIGN KEY(lecture_id) REFERENCES lectures (id)
            );
            INSERT INTO anki_curation_jobs VALUES (
                '00000000-0000-0000-0000-000000000013', 7, 'ready_for_review', 3,
                'OMS::Heme', 'oms::heme', 'snapshot-13', 'legacy input',
                'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',
                'heme-block', '[101,102]', '{"101":"b","102":"c"}',
                41,
                'bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb',
                '["OMS::Heme","OMS::Shared"]', '["oms::heme"]', 'openai',
                'gpt-4.1', 'semantic-9', 'companion-8', 'index-7',
                'cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc',
                'pending', 'legacy instructions',
                'dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd',
                'lcl-v2', 'judgment-v2', 'gap-v2', '["historical warning"]',
                '{"approved":2,"rejected":1}', 6, 'prior error', 'worker-2',
                '2026-08-04T01:00:00+00:00', '2026-08-04T00:30:00+00:00',
                '2026-08-04T00:00:00+00:00', '2026-08-04T00:05:00+00:00',
                '2026-08-04T00:10:00+00:00', '2026-08-04T00:00:00+00:00',
                '2026-08-04T00:15:00+00:00'
            );

            CREATE TABLE anki_stage_artifacts (
                id INTEGER PRIMARY KEY,
                job_id VARCHAR(36) NOT NULL,
                artifact_id VARCHAR(200) NOT NULL,
                stage VARCHAR(30) NOT NULL,
                kind VARCHAR(100) NOT NULL,
                relative_path TEXT NOT NULL,
                input_sha256 VARCHAR(64) NOT NULL,
                content_sha256 VARCHAR(64) NOT NULL,
                metadata_json TEXT NOT NULL,
                created_at VARCHAR(40) NOT NULL,
                FOREIGN KEY(job_id) REFERENCES anki_curation_jobs (id),
                UNIQUE (job_id, artifact_id)
            );
            INSERT INTO anki_stage_artifacts VALUES (
                13, '00000000-0000-0000-0000-000000000013',
                'retrieval_pass_1:8b67a32a27bdda2de0849f3db0749ce4be046ce4b8553229b930401bcdba0df7',
                'retrieval_pass_1', 'classification',
                '00000000-0000-0000-0000-000000000013/retrieval_pass_1/8b67a32a27bdda2de0849f3db0749ce4be046ce4b8553229b930401bcdba0df7.json',
                'eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee',
                '8b67a32a27bdda2de0849f3db0749ce4be046ce4b8553229b930401bcdba0df7',
                '{"note_count":3}', '2026-08-04T00:12:00+00:00'
            );
            """
        )
        connection.commit()


def test_schema_v12_contract_upgrade_backfills_historical_provenance_idempotently(
    tmp_path,
) -> None:
    database_path = tmp_path / "hub-v12-contract.db"
    _create_schema_v12_anki_contract_database(str(database_path))

    legacy_job_columns = (
        "id, lecture_id, state, attempts, target_deck, target_tag, "
        "index_snapshot_id, amboss_input, amboss_sha256, block_id, "
        "source_revision_ids_json, source_revision_hashes_json, summary_outline_id, "
        "summary_outline_sha256, deck_allowlist_json, tag_allowlist_json, provider, "
        "model, semantic_generation, companion_generation, source_index_generation, "
        "configuration_sha256, apply_state, instruction_text, instruction_sha256, "
        "lcl_prompt_version, judgment_rubric_version, gap_prompt_version, warnings_json, "
        "counts_json, review_revision, error, lease_owner, lease_expires_at, "
        "available_at, started_at, ready_at, completed_at, created_at, updated_at"
    )
    legacy_artifact_columns = (
        "id, job_id, artifact_id, stage, kind, relative_path, input_sha256, "
        "content_sha256, metadata_json, created_at"
    )
    with closing(sqlite3.connect(database_path)) as connection:
        legacy_job = connection.execute(
            f"SELECT {legacy_job_columns} FROM anki_curation_jobs"
        ).fetchone()
        legacy_artifact = connection.execute(
            f"SELECT {legacy_artifact_columns} FROM anki_stage_artifacts"
        ).fetchone()

    expected_config = (
        '{"classify_s4":{"fixture_validation_signature":null,"model":"gpt-4.1",'
        '"provider":"openai","thinking_mode":"default"},"gap_fill_s7":'
        '{"fixture_validation_signature":null,"model":"gpt-4.1","provider":'
        '"openai","thinking_mode":"default"},"ledger_s2":'
        '{"fixture_validation_signature":null,"model":"gpt-4.1","provider":'
        '"openai","thinking_mode":"default"},"profile":"legacy_single_model",'
        '"residual_s6":{"fixture_validation_signature":null,"model":"gpt-4.1",'
        '"provider":"openai","thinking_mode":"default"},"residual_unlocked":false}'
    )
    expected_config_sha256 = "3a4f2231901cec8e24440a26576ab5020f074e7bd2e3c5c12fb7fab652ee8f59"
    empty_sha256 = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"

    with Database(f"sqlite:///{database_path}") as database:
        database.migrate()
        repository = AnkiCurationRepository(database)
        job = repository.require_job(UUID("00000000-0000-0000-0000-000000000013"))
        artifact = repository.list_stage_artifacts(job.id)[0]
        artifact_root = tmp_path / "legacy-artifacts"
        artifact_path = artifact_root / artifact.relative_path
        artifact_path.parent.mkdir(parents=True)
        artifact_path.write_text(
            "{\"artifact_version\":1,\"job_id\":\"00000000-0000-0000-0000-000000000013\","
            "\"kind\":\"classification\",\"metadata\":{\"note_count\":3},"
            "\"payload\":{\"note_ids\":[11,12,13]},\"stage\":\"retrieval_pass_1\"}\n",
            encoding="utf-8",
        )
        artifacts = StageArtifactStore(artifact_root)
        assert artifacts.read(artifact, job=job) == {"note_ids": [11, 12, 13]}
        with pytest.raises(PinnedInputChanged, match="invalid provenance"):
            artifacts.read(
                artifact,
                job=replace(
                    job,
                    pipeline_contract_version=PipelineContractVersion.CARD_CENTRIC_V1,
                ),
            )
        with database.session() as session:
            upgraded_job = session.execute(
                text(
                    f"SELECT {legacy_job_columns}, pipeline_contract_version, "
                    "resolved_model_config_json, model_config_sha256 "
                    "FROM anki_curation_jobs"
                )
            ).one()
            upgraded_artifact = session.execute(
                text(
                    f"SELECT {legacy_artifact_columns}, pipeline_contract_version, "
                    "model_config_sha256 FROM anki_stage_artifacts"
                )
            ).one()
            counts = session.execute(
                text(
                    "SELECT (SELECT COUNT(*) FROM anki_curation_jobs), "
                    "(SELECT COUNT(*) FROM anki_stage_artifacts)"
                )
            ).one()

        database.migrate()
        with database.session() as session:
            repeated_job = session.execute(
                text(
                    f"SELECT {legacy_job_columns}, pipeline_contract_version, "
                    "resolved_model_config_json, model_config_sha256 "
                    "FROM anki_curation_jobs"
                )
            ).one()
            repeated_artifact = session.execute(
                text(
                    f"SELECT {legacy_artifact_columns}, pipeline_contract_version, "
                    "model_config_sha256 FROM anki_stage_artifacts"
                )
            ).one()
            repeated_counts = session.execute(
                text(
                    "SELECT (SELECT COUNT(*) FROM anki_curation_jobs), "
                    "(SELECT COUNT(*) FROM anki_stage_artifacts)"
                )
            ).one()

    assert upgraded_job[:-3] == legacy_job
    assert upgraded_job[-3:] == (
        "retrieval_v4",
        expected_config,
        expected_config_sha256,
    )
    assert upgraded_artifact[:-2] == legacy_artifact
    assert upgraded_artifact[-2:] == ("retrieval_v4", empty_sha256)
    assert counts == (1, 1)
    assert repeated_job == upgraded_job
    assert repeated_artifact == upgraded_artifact
    assert repeated_counts == counts
