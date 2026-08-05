import hashlib
import json
from typing import TYPE_CHECKING

from sqlalchemy import inspect, select, text

import oms_hub.anki.models  # noqa: F401
from oms_hub.domain import StepStatus, V2StepName
from oms_hub.llm.catalog import FALLBACK_MODELS
from oms_hub.llm.domain import LLMTask, ProviderName
from oms_hub.llm.repository import DEFAULT_MODELS
from oms_hub.models import (
    LectureModel,
    LectureStepModel,
    LLMProviderSettingModel,
    LLMTaskAssignmentModel,
    SchemaVersionModel,
    StudyAISettingModel,
)

if TYPE_CHECKING:
    from oms_hub.db import Database

LATEST_SCHEMA_VERSION = 15


def _ensure_column(
    database: "Database",
    table_name: str,
    column_name: str,
    definition: str,
) -> None:
    inspector = inspect(database.engine)
    if not inspector.has_table(table_name):
        return
    columns = {column["name"] for column in inspector.get_columns(table_name)}
    if column_name in columns:
        return
    with database.engine.begin() as connection:
        connection.execute(text(f"ALTER TABLE {table_name} ADD COLUMN {column_name} {definition}"))


def _upgrade_anki_v4_columns(database: "Database") -> None:
    empty_sha256 = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
    job_columns = {
        "block_id": "VARCHAR(200)",
        "source_revision_ids_json": "TEXT NOT NULL DEFAULT '[]'",
        "source_revision_hashes_json": "TEXT NOT NULL DEFAULT '{}'",
        "summary_outline_id": "INTEGER",
        "summary_outline_sha256": "VARCHAR(64)",
        "deck_allowlist_json": "TEXT NOT NULL DEFAULT '[]'",
        "tag_allowlist_json": "TEXT NOT NULL DEFAULT '[]'",
        "provider": "VARCHAR(30) NOT NULL DEFAULT 'anthropic'",
        "model": "VARCHAR(200) NOT NULL DEFAULT 'claude-sonnet-5'",
        "semantic_generation": "VARCHAR(200)",
        "companion_generation": "VARCHAR(200)",
        "source_index_generation": "VARCHAR(200)",
        "configuration_sha256": (f"VARCHAR(64) NOT NULL DEFAULT '{empty_sha256}'"),
        "apply_state": "VARCHAR(50) NOT NULL DEFAULT 'pending'",
        "lease_owner": "VARCHAR(100)",
        "lease_expires_at": "VARCHAR(40)",
        "available_at": "VARCHAR(40)",
    }
    for name, definition in job_columns.items():
        _ensure_column(database, "anki_curation_jobs", name, definition)
    _ensure_column(
        database,
        "anki_candidates",
        "retrieval_pass",
        "VARCHAR(30) NOT NULL DEFAULT 'pass_1'",
    )
    gap_columns = {
        "source_refs_json": "TEXT NOT NULL DEFAULT '[]'",
        "evidence_ids_json": "TEXT NOT NULL DEFAULT '[]'",
        "provenance_json": "TEXT NOT NULL DEFAULT '{}'",
        "initial_tags_json": "TEXT NOT NULL DEFAULT '[]'",
        "content_hash": f"VARCHAR(64) NOT NULL DEFAULT '{empty_sha256}'",
    }
    for name, definition in gap_columns.items():
        _ensure_column(database, "anki_gap_cards", name, definition)


def _upgrade_anki_contract_v13(database: "Database") -> None:
    empty_sha256 = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
    for name, definition in {
        "pipeline_contract_version": "VARCHAR(30) NOT NULL DEFAULT 'retrieval_v4'",
        "resolved_model_config_json": "TEXT NOT NULL DEFAULT '{}'",
        "model_config_sha256": f"VARCHAR(64) NOT NULL DEFAULT '{empty_sha256}'",
    }.items():
        _ensure_column(database, "anki_curation_jobs", name, definition)
    for name, definition in {
        "pipeline_contract_version": "VARCHAR(30) NOT NULL DEFAULT 'retrieval_v4'",
        "model_config_sha256": f"VARCHAR(64) NOT NULL DEFAULT '{empty_sha256}'",
    }.items():
        _ensure_column(database, "anki_stage_artifacts", name, definition)
    with database.engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE anki_curation_jobs SET pipeline_contract_version = "
                "'retrieval_v4' WHERE pipeline_contract_version IS NULL OR "
                "pipeline_contract_version = ''"
            )
        )
        rows = connection.execute(
            text("SELECT id, provider, model, resolved_model_config_json FROM anki_curation_jobs")
        ).mappings()
        for row in rows:
            config = row["resolved_model_config_json"]
            if not config or config == "{}":
                stage = {
                    "provider": row["provider"],
                    "model": row["model"],
                    "thinking_mode": "default",
                    "fixture_validation_signature": None,
                }
                config = json.dumps(
                    {
                        "profile": "legacy_single_model",
                        "ledger_s2": stage,
                        "classify_s4": stage,
                        "residual_s6": stage,
                        "gap_fill_s7": stage,
                        "residual_unlocked": False,
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                    ensure_ascii=False,
                )
            digest = hashlib.sha256(config.encode("utf-8")).hexdigest()
            connection.execute(
                text(
                    "UPDATE anki_curation_jobs SET "
                    "resolved_model_config_json=:config, "
                    "model_config_sha256=:digest WHERE id=:id"
                ),
                {"config": config, "digest": digest, "id": row["id"]},
            )


def _upgrade_generation_job_columns(database: "Database") -> None:
    columns = {
        "pdf_revision_id": "INTEGER",
        "transcript_revision_id": "INTEGER",
        "gemini_quiz_id": "VARCHAR(500)",
        "supersedes_job_id": "VARCHAR(36)",
        "quiz_url": "TEXT",
    }
    for name, definition in columns.items():
        _ensure_column(database, "generation_jobs", name, definition)


def _upgrade_studio_columns(database: "Database") -> None:
    """Backfill columns introduced with NotebookLM Studio.

    ``create_schema`` creates all new tables, but it intentionally does not
    alter tables from an older installation. Keep these additions additive so
    existing Anki and lecture-generation data remains intact.
    """
    published_columns = {
        "studio_run_id": "VARCHAR(36)",
        "destination_subject": "VARCHAR(100) NOT NULL DEFAULT ''",
        "destination_subject_key": "VARCHAR(100) NOT NULL DEFAULT ''",
        "destination_exam_number": "INTEGER NOT NULL DEFAULT 0",
        "label": "VARCHAR(300) NOT NULL DEFAULT ''",
        "label_key": "VARCHAR(300) NOT NULL DEFAULT ''",
        "active": "BOOLEAN NOT NULL DEFAULT 1",
    }
    for name, definition in published_columns.items():
        _ensure_column(database, "published_quizzes", name, definition)

    with database.engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE published_quizzes SET "
                "destination_subject = COALESCE(NULLIF(destination_subject, ''), "
                "(SELECT subject FROM lectures "
                "WHERE lectures.id = published_quizzes.lecture_id)), "
                "destination_subject_key = COALESCE(NULLIF(destination_subject_key, ''), "
                "lower(trim((SELECT subject FROM lectures "
                "WHERE lectures.id = published_quizzes.lecture_id)))), "
                "destination_exam_number = CASE WHEN destination_exam_number = 0 THEN "
                "COALESCE((SELECT exam_number FROM lectures "
                "WHERE lectures.id = published_quizzes.lecture_id), 0) "
                "ELSE destination_exam_number END, "
                "label = COALESCE(NULLIF(label, ''), title), "
                "label_key = COALESCE(NULLIF(label_key, ''), lower(trim(title)) )"
            )
        )
        connection.execute(
            text(
                "CREATE INDEX IF NOT EXISTS ix_generation_jobs_poll "
                "ON generation_jobs (state, next_attempt_at, created_at)"
            )
        )
        connection.execute(
            text(
                "CREATE INDEX IF NOT EXISTS ix_generation_jobs_supersedes "
                "ON generation_jobs (supersedes_job_id)"
            )
        )


def _upgrade_studio_run_active_label_index(database: "Database") -> None:
    """Backfill the partial unique index guarding concurrent Studio runs.

    ``create_schema`` only creates missing tables; it does not retrofit
    indexes onto ``studio_runs`` tables that already existed before this
    index was introduced. Recreate it explicitly so both fresh and upgraded
    installs reject two active runs for the same destination/label.
    """
    if database.engine.dialect.name != "sqlite":
        return
    inspector = inspect(database.engine)
    if not inspector.has_table("studio_runs"):
        return
    with database.engine.begin() as connection:
        connection.execute(
            text(
                "CREATE UNIQUE INDEX IF NOT EXISTS ix_studio_runs_active_label "
                "ON studio_runs "
                "(destination_subject_key, destination_exam_number, label_key) "
                "WHERE state IN ('queued', 'running', 'retrying')"
            )
        )


def _upgrade_quiz_import_v15(database: "Database") -> None:
    """Add durable direct-import provenance without disturbing older workflows."""
    source_columns = {
        "purpose": "VARCHAR(30) NOT NULL DEFAULT 'notebook'",
        "snapshot_sha256": "VARCHAR(64)",
        "media_type": "VARCHAR(100)",
        "final_url": "TEXT",
    }
    for name, definition in source_columns.items():
        _ensure_column(database, "studio_sources", name, definition)

    run_columns = {
        "workflow_kind": "VARCHAR(30) NOT NULL DEFAULT 'notebook_generation'",
        "content_kind": "VARCHAR(30) NOT NULL DEFAULT 'exam_review'",
    }
    for name, definition in run_columns.items():
        _ensure_column(database, "studio_runs", name, definition)

    _ensure_column(
        database,
        "published_quizzes",
        "content_kind",
        "VARCHAR(30) NOT NULL DEFAULT 'lecture_quiz'",
    )
    inspector = inspect(database.engine)
    if not inspector.has_table("published_quizzes"):
        return
    with database.engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE published_quizzes SET content_kind = "
                "CASE WHEN studio_run_id IS NOT NULL THEN 'exam_review' "
                "ELSE 'lecture_quiz' END "
                "WHERE content_kind IS NULL OR content_kind = '' "
                "OR (studio_run_id IS NOT NULL AND content_kind = 'lecture_quiz')"
            )
        )


def _upgrade_gap_card_identity(database: "Database") -> None:
    if database.engine.dialect.name != "sqlite":
        return
    inspector = inspect(database.engine)
    if not inspector.has_table("anki_gap_cards"):
        return
    has_legacy_unique = any(
        set(constraint.get("column_names") or ()) == {"job_id", "concept_id"}
        for constraint in inspector.get_unique_constraints("anki_gap_cards")
    )
    if has_legacy_unique:
        _rebuild_gap_card_table(database)
    with database.engine.begin() as connection:
        connection.execute(
            text(
                "CREATE INDEX IF NOT EXISTS ix_anki_gap_cards_job_concept "
                "ON anki_gap_cards (job_id, concept_id)"
            )
        )


def _rebuild_gap_card_table(database: "Database") -> None:
    with database.engine.begin() as connection:
        connection.execute(
            text(
                """
                CREATE TABLE anki_gap_cards_v11 (
                    id VARCHAR(36) NOT NULL PRIMARY KEY,
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
                    FOREIGN KEY(job_id) REFERENCES anki_curation_jobs (id)
                )
                """
            )
        )
        connection.execute(
            text(
                """
                INSERT INTO anki_gap_cards_v11 (
                    id, job_id, concept_id, text, extra, revision, selected,
                    image_state, media_filename, source_note_id,
                    generated_image_json, validation_state,
                    source_refs_json, evidence_ids_json, provenance_json,
                    initial_tags_json, content_hash
                )
                SELECT
                    id, job_id, concept_id, text, extra, revision, selected,
                    image_state, media_filename, source_note_id,
                    generated_image_json, validation_state,
                    source_refs_json, evidence_ids_json, provenance_json,
                    initial_tags_json, content_hash
                FROM anki_gap_cards
                """
            )
        )
        connection.execute(text("DROP TABLE anki_gap_cards"))
        connection.execute(text("ALTER TABLE anki_gap_cards_v11 RENAME TO anki_gap_cards"))


def _seed_llm_task_assignments(database: "Database") -> None:
    """Seed per-task LLM assignments the first time they are missing.

    Existing rows are left untouched so operator overrides survive re-runs.
    """
    inspector = inspect(database.engine)
    if not inspector.has_table("llm_task_assignments"):
        return
    with database.session() as session:
        existing = {row.task for row in session.scalars(select(LLMTaskAssignmentModel)).all()}
        missing = {task.value for task in LLMTask} - existing
        if not missing:
            return

        active_row = session.scalar(
            select(LLMProviderSettingModel).where(LLMProviderSettingModel.active.is_(True))
        )
        if active_row is not None:
            default_provider = active_row.provider
            default_model = active_row.model
        else:
            default_provider = ProviderName.ANTHROPIC.value
            default_model = DEFAULT_MODELS[ProviderName.ANTHROPIC]

        ai_settings = session.get(StudyAISettingModel, 1)
        if ai_settings is not None and ai_settings.openrouter_model.strip():
            accuracy_model = ai_settings.openrouter_model
        else:
            accuracy_model = FALLBACK_MODELS[ProviderName.OPENROUTER][0]

        seeds = {
            LLMTask.TRANSCRIPTS.value: (default_provider, default_model),
            LLMTask.ANKI_CURATION.value: (default_provider, default_model),
            LLMTask.ACCURACY_REVIEW.value: (
                ProviderName.OPENROUTER.value,
                accuracy_model,
            ),
            LLMTask.QUIZ_EXTRACTION.value: (
                ProviderName.OPENAI.value,
                DEFAULT_MODELS[ProviderName.OPENAI],
            ),
            LLMTask.QUIZ_ANSWER_GENERATION.value: (
                ProviderName.OPENAI.value,
                DEFAULT_MODELS[ProviderName.OPENAI],
            ),
        }
        for task_value in missing:
            provider_value, model_value = seeds[task_value]
            session.add(
                LLMTaskAssignmentModel(
                    task=task_value,
                    provider=provider_value,
                    model=model_value,
                )
            )


def migrate_database(database: "Database") -> None:
    database.create_schema()
    _upgrade_generation_job_columns(database)
    _upgrade_studio_columns(database)
    _upgrade_studio_run_active_label_index(database)
    _upgrade_quiz_import_v15(database)
    _upgrade_anki_v4_columns(database)
    _upgrade_anki_contract_v13(database)
    _upgrade_gap_card_identity(database)
    _seed_llm_task_assignments(database)
    usage_columns = {
        column["name"] for column in inspect(database.engine).get_columns("study_usage")
    }
    if "provider" not in usage_columns:
        with database.engine.begin() as connection:
            connection.execute(
                text(
                    "ALTER TABLE study_usage ADD COLUMN provider "
                    "VARCHAR(30) NOT NULL DEFAULT 'openai'"
                )
            )
    source_columns = {
        column["name"]
        for column in inspect(database.engine).get_columns("notebook_source_mappings")
    }
    if "display_title" not in source_columns:
        with database.engine.begin() as connection:
            connection.execute(
                text(
                    "ALTER TABLE notebook_source_mappings "
                    "ADD COLUMN display_title VARCHAR(500) NOT NULL DEFAULT ''"
                )
            )
    with database.session() as session:
        version = session.get(SchemaVersionModel, 1)
        if version is not None and version.version >= LATEST_SCHEMA_VERSION:
            return

        existing_steps = set(
            session.execute(select(LectureStepModel.lecture_id, LectureStepModel.name)).all()
        )
        lecture_ids = session.scalars(select(LectureModel.id)).all()
        for lecture_id in lecture_ids:
            for step in V2StepName:
                if (lecture_id, step.value) not in existing_steps:
                    session.add(
                        LectureStepModel(
                            lecture_id=lecture_id,
                            name=step.value,
                            status=StepStatus.WAITING.value,
                        )
                    )

        if version is None:
            session.add(SchemaVersionModel(id=1, version=LATEST_SCHEMA_VERSION))
        else:
            version.version = LATEST_SCHEMA_VERSION
