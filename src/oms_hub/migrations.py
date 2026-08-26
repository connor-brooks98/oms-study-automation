import hashlib
import json
import re
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast
from uuid import UUID

from sqlalchemy import Table, inspect, select, text

import oms_hub.anki.models  # noqa: F401
from oms_hub.anki.card_centric import (
    _redacted_invalid_response,
    validate_persisted_s2_generation_parameters,
)
from oms_hub.anki.cost_estimator import FrozenRateTable
from oms_hub.anki.course_policy import CourseCurationPolicy
from oms_hub.anki.models import CourseCurationPolicyModel
from oms_hub.domain import StepStatus, V2StepName
from oms_hub.files.trusted_paths import (
    is_indirection,
    trusted_existing_directory,
    trusted_managed_path,
)
from oms_hub.llm.catalog import FALLBACK_MODELS
from oms_hub.llm.domain import DiagnosticSource, LLMTask, ProviderName
from oms_hub.llm.repository import DEFAULT_MODELS
from oms_hub.models import (
    LectureModel,
    LectureStepModel,
    LLMProviderSettingModel,
    LLMTaskAssignmentModel,
    RuntimeSettingAuditModel,
    RuntimeSettingModel,
    SchemaVersionModel,
    StudyAISettingModel,
)

if TYPE_CHECKING:
    from oms_hub.db import Database

LATEST_SCHEMA_VERSION = 29


class StudioPublicationMigrationConflict(RuntimeError):
    """Legacy Studio publication ownership is ambiguous and needs recovery."""


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


def _validate_card_ledger_attempts_v25(database: "Database") -> None:
    """Require append-only S2 repair and transport evidence on v25 schemas."""
    inspector = inspect(database.engine)
    table = "anki_card_ledger_attempts"
    if not inspector.has_table(table):
        raise RuntimeError("schema v25 is missing card-ledger attempt evidence")
    column_rows = {item["name"]: item for item in inspector.get_columns(table)}
    columns = set(column_rows)
    required = {
        "id",
        "job_id",
        "stage",
        "stage_attempt",
        "call_index",
        "kind",
        "outcome",
        "provider",
        "model",
        "instruction_sha256",
        "generation_parameters_json",
        "generation_parameters_sha256",
        "request_id",
        "input_tokens",
        "output_tokens",
        "cost_microusd",
        "validation_error",
        "invalid_response_sha256",
        "invalid_response",
        "diagnostic_source",
        "http_status",
        "created_at",
    }
    missing = sorted(required - columns)
    if missing:
        raise RuntimeError(
            "schema v25 card-ledger attempt evidence is incomplete: " + ", ".join(missing)
        )
    unique_sets = {
        tuple(index["column_names"])
        for index in inspector.get_indexes(table)
        if index.get("unique")
    }
    unique_sets.update(
        tuple(constraint["column_names"]) for constraint in inspector.get_unique_constraints(table)
    )
    if ("job_id", "stage", "stage_attempt", "call_index") not in unique_sets:
        raise RuntimeError("schema v25 card-ledger attempt identity is not unique")
    expected_nullable = {
        "id": False,
        "job_id": False,
        "stage": False,
        "stage_attempt": False,
        "call_index": False,
        "kind": False,
        "outcome": False,
        "provider": False,
        "model": False,
        "instruction_sha256": False,
        "generation_parameters_json": False,
        "generation_parameters_sha256": False,
        "request_id": True,
        "input_tokens": False,
        "output_tokens": False,
        "cost_microusd": False,
        "validation_error": True,
        "invalid_response_sha256": True,
        "invalid_response": True,
        "diagnostic_source": True,
        "http_status": True,
        "created_at": False,
    }
    if any(
        bool(column_rows[name]["nullable"]) != nullable
        for name, nullable in expected_nullable.items()
    ):
        raise RuntimeError("schema v25 card-ledger attempt nullability is invalid")
    primary_key = tuple(inspector.get_pk_constraint(table).get("constrained_columns") or ())
    if primary_key != ("id",):
        raise RuntimeError("schema v25 card-ledger attempt primary identity is invalid")
    expected_fk = {
        "constrained_columns": ["job_id"],
        "referred_table": "anki_curation_jobs",
        "referred_columns": ["id"],
    }
    foreign_keys = inspector.get_foreign_keys(table)
    matching_foreign_keys = [
        foreign_key
        for foreign_key in foreign_keys
        if all(foreign_key.get(key) == value for key, value in expected_fk.items())
    ]
    if len(matching_foreign_keys) != 1:
        raise RuntimeError("schema v25 card-ledger attempt foreign key is invalid")
    fk_options = matching_foreign_keys[0].get("options", {})
    if (fk_options.get("ondelete") or "NO ACTION").upper() != "NO ACTION":
        raise RuntimeError("schema v25 card-ledger attempt foreign key action is invalid")
    index_sets = {tuple(index["column_names"]) for index in inspector.get_indexes(table)}
    if ("job_id", "stage_attempt") not in index_sets:
        raise RuntimeError("schema v25 card-ledger attempt lookup index is missing")
    with database.engine.connect() as connection:
        rows = list(
            connection.execute(
                text(
                    "SELECT * FROM anki_card_ledger_attempts "
                    "ORDER BY job_id, stage_attempt, call_index"
                )
            ).mappings()
        )
        for row in rows:
            _validate_card_ledger_attempt_row(row)
        stage_transports = {
            (row["job_id"], row["stage"]): (row["provider"], row["model"])
            for row in connection.execute(
                text("SELECT job_id, stage, provider, model FROM anki_job_stages")
            ).mappings()
        }
    _validate_card_ledger_attempt_lifecycles_v25(rows, stage_transports)


def _validate_provider_attempt_events_v26(database: "Database") -> None:
    """Require append-only, batch-bound provider evidence on v27 schemas."""
    inspector = inspect(database.engine)
    table = "anki_provider_attempt_events"
    if not inspector.has_table(table):
        raise RuntimeError("schema v26 is missing provider-attempt event evidence")
    column_rows = {item["name"]: item for item in inspector.get_columns(table)}
    required = {
        "id",
        "job_id",
        "stage",
        "stage_attempt",
        "mode",
        "call_index",
        "subcall_ordinal",
        "batch_index",
        "batch_note_ids_json",
        "batch_note_ids_sha256",
        "kind",
        "event",
        "provider",
        "model",
        "instruction_sha256",
        "input_sha256",
        "output_schema_sha256",
        "generation_parameters_json",
        "generation_parameters_sha256",
        "cache_prefix_sha256",
        "request_sha256",
        "request_id",
        "input_tokens",
        "output_tokens",
        "cost_microusd",
        "cache_creation_input_tokens",
        "cache_read_input_tokens",
        "response_sha256",
        "response_text",
        "validation_error",
        "missing_note_ids_json",
        "extra_note_ids_json",
        "duplicate_note_ids_json",
        "diagnostic_source",
        "http_status",
        "created_at",
    }
    missing = sorted(required - set(column_rows))
    if missing:
        raise RuntimeError(
            "schema v26 provider-attempt event evidence is incomplete: " + ", ".join(missing)
        )
    unique_sets = {
        tuple(index["column_names"])
        for index in inspector.get_indexes(table)
        if index.get("unique")
    }
    unique_sets.update(
        tuple(constraint["column_names"]) for constraint in inspector.get_unique_constraints(table)
    )
    identity = (
        "job_id",
        "stage",
        "stage_attempt",
        "mode",
        "call_index",
        "subcall_ordinal",
        "event",
    )
    if identity not in unique_sets:
        raise RuntimeError("schema v26 provider-attempt event identity is not unique")
    index_sets = {tuple(index["column_names"]) for index in inspector.get_indexes(table)}
    if ("job_id", "stage", "stage_attempt") not in index_sets:
        raise RuntimeError("schema v26 provider-attempt execution index is missing")
    expected_fk = {
        "constrained_columns": ["job_id"],
        "referred_table": "anki_curation_jobs",
        "referred_columns": ["id"],
    }
    if not any(
        all(foreign_key.get(key) == value for key, value in expected_fk.items())
        for foreign_key in inspector.get_foreign_keys(table)
    ):
        raise RuntimeError("schema v26 provider-attempt foreign key is invalid")


def _upgrade_provider_attempt_subcall_ordinal_v27(database: "Database") -> None:
    """Preserve v26 evidence while making subcalls a durable identity field."""
    _ensure_column(
        database,
        "anki_provider_attempt_events",
        "subcall_ordinal",
        "INTEGER NOT NULL DEFAULT 0",
    )
    inspector = inspect(database.engine)
    if not inspector.has_table("anki_provider_attempt_events"):
        return
    with database.engine.begin() as connection:
        connection.execute(
            text(
                "CREATE UNIQUE INDEX IF NOT EXISTS "
                "ux_anki_provider_attempt_events_identity_v27 "
                "ON anki_provider_attempt_events "
                "(job_id, stage, stage_attempt, mode, call_index, subcall_ordinal, event)"
            )
        )


def _upgrade_course_curation_policy_v28(database: "Database") -> None:
    """Add immutable policy revisions and an optional v3 job pin."""
    policy_table = cast(Table, CourseCurationPolicyModel.__table__)
    policy_table.create(database.engine, checkfirst=True)
    _ensure_column(database, "anki_curation_jobs", "policy_sha256", "VARCHAR(64)")


def _upgrade_v3_durable_reservations_v29(database: "Database") -> None:
    _ensure_column(
        database, "anki_curation_jobs", "offline_replay_only", "BOOLEAN NOT NULL DEFAULT 0"
    )
    _ensure_column(database, "anki_curation_jobs", "v3_rate_table_json", "TEXT")
    _ensure_column(database, "anki_curation_jobs", "v3_rate_table_sha256", "VARCHAR(64)")
    _ensure_column(database, "anki_provider_attempt_events", "cost_reservation_json", "TEXT")
    _ensure_column(
        database, "anki_provider_attempt_events", "cost_reservation_sha256", "VARCHAR(64)"
    )


def _validate_v3_durable_reservations_v29(database: "Database") -> None:
    inspector = inspect(database.engine)
    events = {column["name"] for column in inspector.get_columns("anki_provider_attempt_events")}
    jobs = {column["name"] for column in inspector.get_columns("anki_curation_jobs")}
    if {"cost_reservation_json", "cost_reservation_sha256"} - events or {
        "offline_replay_only",
        "v3_rate_table_json",
        "v3_rate_table_sha256",
    } - jobs:
        raise RuntimeError("schema v29 durable v3 reservation contract is incomplete")
    with database.engine.connect() as connection:
        if "pipeline_contract_version" not in jobs:
            return
        for row in connection.execute(
            text(
                "SELECT cost_reservation_json, cost_reservation_sha256 "
                "FROM anki_provider_attempt_events"
            )
        ).mappings():
            payload, digest = row["cost_reservation_json"], row["cost_reservation_sha256"]
            if (payload is None) != (digest is None):
                raise RuntimeError("schema v29 reservation fields must be paired")
            if payload is not None and hashlib.sha256(payload.encode()).hexdigest() != digest:
                raise RuntimeError("schema v29 reservation hash is invalid")
        for row in connection.execute(
            text(
                "SELECT pipeline_contract_version, offline_replay_only, v3_rate_table_json, "
                "v3_rate_table_sha256 FROM anki_curation_jobs"
            )
        ).mappings():
            if row["pipeline_contract_version"] != "card_centric_v3":
                continue
            payload, digest = row["v3_rate_table_json"], row["v3_rate_table_sha256"]
            if payload is None or digest is None:
                raise RuntimeError("schema v29 v3 job rate-table pin is incomplete")
            try:
                document = json.loads(payload)
                table = FrozenRateTable.from_document(document)
            except (TypeError, ValueError, json.JSONDecodeError) as exc:
                raise RuntimeError("schema v29 v3 job rate-table pin is invalid") from exc
            canonical = json.dumps(table.document(), sort_keys=True, separators=(",", ":"))
            if payload != canonical or digest != table.rate_table_sha256:
                raise RuntimeError("schema v29 v3 job rate-table pin changed")


def _validate_course_curation_policy_v28(database: "Database") -> None:
    """Fail closed when a claimed v28 database lacks its new immutable contracts."""
    inspector = inspect(database.engine)
    table = "course_curation_policy"
    if not inspector.has_table(table):
        raise RuntimeError("schema v28 is missing course curation policy revisions")
    columns = {column["name"]: column for column in inspector.get_columns(table)}
    required = {
        "id",
        "policy_id",
        "revision",
        "payload_json",
        "policy_sha256",
        "created_at",
        "updated_at",
    }
    if missing := required - set(columns):
        raise RuntimeError(
            "schema v28 policy columns are incomplete: " + ", ".join(sorted(missing))
        )
    if any(columns[name]["nullable"] for name in required - {"id"}):
        raise RuntimeError("schema v28 policy columns must be non-null")
    unique_sets = {tuple(item["column_names"]) for item in inspector.get_unique_constraints(table)}
    if ("policy_id", "revision") not in unique_sets:
        raise RuntimeError("schema v28 policy revision identity is not unique")
    jobs = {column["name"]: column for column in inspector.get_columns("anki_curation_jobs")}
    if "policy_sha256" not in jobs or not jobs["policy_sha256"]["nullable"]:
        raise RuntimeError("schema v28 job policy pin must be nullable")
    with database.engine.connect() as connection:
        policies = list(
            connection.execute(
                text(
                    "SELECT policy_id, revision, payload_json, policy_sha256 "
                    "FROM course_curation_policy"
                )
            ).mappings()
        )
        policy_hashes: set[str] = set()
        for row in policies:
            try:
                payload = json.loads(row["payload_json"])
                canonical = json.dumps(
                    payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
                )
                if canonical != row["payload_json"]:
                    raise ValueError("payload is not canonical")
                policy = CourseCurationPolicy.model_validate(
                    {**payload, "policy_sha256": row["policy_sha256"]}
                )
                canonical_policy_payload = json.dumps(
                    policy.canonical_payload(),
                    sort_keys=True,
                    separators=(",", ":"),
                    ensure_ascii=False,
                )
                if canonical_policy_payload != row["payload_json"]:
                    raise ValueError("payload does not match canonical policy")
            except (TypeError, ValueError, json.JSONDecodeError) as error:
                raise RuntimeError("schema v28 policy row is invalid") from error
            if policy.policy_id != row["policy_id"] or policy.revision != row["revision"]:
                raise RuntimeError("schema v28 policy row identity is invalid")
            policy_hashes.add(policy.policy_sha256)
        if "pipeline_contract_version" not in jobs:
            return
        pinned_jobs = connection.execute(
            text(
                "SELECT pipeline_contract_version, policy_sha256 FROM anki_curation_jobs "
                "WHERE policy_sha256 IS NOT NULL"
            )
        ).mappings()
        for row in pinned_jobs:
            if (
                row["pipeline_contract_version"] != "card_centric_v3"
                or row["policy_sha256"] not in policy_hashes
            ):
                raise RuntimeError("schema v28 job policy pin is invalid")


def _validate_card_ledger_attempt_lifecycles_v25(
    rows: list[Any],
    stage_transports: dict[tuple[str, str], tuple[str | None, str | None]],
) -> None:
    """Require the bounded primary-then-repair order for every S2 execution."""
    attempts_by_execution: dict[tuple[str, int], list[Any]] = {}
    for row in rows:
        key = (row["job_id"], row["stage_attempt"])
        attempts_by_execution.setdefault(key, []).append(row)
    for attempts in attempts_by_execution.values():
        call_indexes = [row["call_index"] for row in attempts]
        if call_indexes not in ([1], [1, 2]):
            raise RuntimeError("schema v25 card-ledger attempt lifecycle is invalid")
        if len(attempts) == 2 and attempts[0]["outcome"] != "validation_failed":
            raise RuntimeError("schema v25 card-ledger attempt lifecycle is invalid")
        if len(attempts) == 2 and any(
            attempts[0][field] != attempts[1][field]
            for field in (
                "provider",
                "model",
                "generation_parameters_json",
                "generation_parameters_sha256",
            )
        ):
            raise RuntimeError("schema v25 card-ledger attempt transport identity is invalid")
        expected_transport = stage_transports.get((attempts[0]["job_id"], "card_ledger"))
        if (
            expected_transport is not None
            and all(expected_transport)
            and any((row["provider"], row["model"]) != expected_transport for row in attempts)
        ):
            raise RuntimeError("schema v25 card-ledger attempt stage transport is invalid")


def _validate_card_ledger_attempt_row(row: Any) -> None:
    required_hashes = ("instruction_sha256", "generation_parameters_sha256")
    if (
        row["stage"] != "card_ledger"
        or not isinstance(row["stage_attempt"], int)
        or row["stage_attempt"] < 1
        or row["call_index"] not in {1, 2}
        or row["kind"] not in {"primary", "repair"}
        or (row["call_index"] == 1) != (row["kind"] == "primary")
        or row["outcome"] not in {"accepted", "validation_failed", "transport_failed"}
        or row["provider"] not in {item.value for item in ProviderName}
        or not isinstance(row["model"], str)
        or not row["model"].strip()
        or len(row["model"]) > 200
        or (
            row["request_id"] is not None
            and (
                not isinstance(row["request_id"], str)
                or not row["request_id"].strip()
                or len(row["request_id"]) > 200
            )
        )
        or any(not re.fullmatch(r"[0-9a-f]{64}", str(row[name])) for name in required_hashes)
        or any(
            not isinstance(row[name], int) or isinstance(row[name], bool) or row[name] < 0
            for name in ("input_tokens", "output_tokens", "cost_microusd")
        )
    ):
        raise RuntimeError("schema v25 card-ledger attempt row is invalid")
    try:
        parameters = json.loads(row["generation_parameters_json"])
    except (TypeError, json.JSONDecodeError) as error:
        raise RuntimeError("schema v25 card-ledger attempt parameters are invalid") from error
    canonical = json.dumps(parameters, sort_keys=True, separators=(",", ":"))
    if (
        not isinstance(parameters, dict)
        or canonical != row["generation_parameters_json"]
        or hashlib.sha256(canonical.encode()).hexdigest() != row["generation_parameters_sha256"]
    ):
        raise RuntimeError("schema v25 card-ledger attempt parameters are invalid")
    try:
        validate_persisted_s2_generation_parameters(
            ProviderName(row["provider"]),
            row["model"],
            parameters,
        )
    except (TypeError, ValueError) as error:
        raise RuntimeError("schema v25 card-ledger attempt parameters are invalid") from error
    invalid_response = row["invalid_response"]
    invalid_hash = row["invalid_response_sha256"]
    validation_error = row["validation_error"]
    diagnostic_source = row["diagnostic_source"]
    http_status = row["http_status"]
    valid_diagnostic = (
        diagnostic_source is None
        or diagnostic_source in {source.value for source in DiagnosticSource}
    ) and (
        http_status is None
        or (
            isinstance(http_status, int)
            and not isinstance(http_status, bool)
            and 100 <= http_status <= 599
        )
    )
    if row["outcome"] == "accepted":
        valid_payload = (
            validation_error is None
            and invalid_response is None
            and invalid_hash is None
            and diagnostic_source is None
            and http_status is None
        )
    elif row["outcome"] == "validation_failed":
        valid_payload = (
            isinstance(validation_error, str)
            and bool(validation_error.strip())
            and len(validation_error) <= 2_000
            and isinstance(invalid_response, str)
            and len(invalid_response) <= 12_000
            and isinstance(invalid_hash, str)
            and bool(re.fullmatch(r"[0-9a-f]{64}", invalid_hash))
            and invalid_response == _redacted_invalid_response(invalid_response)
            and hashlib.sha256(invalid_response.encode()).hexdigest() == invalid_hash
            and diagnostic_source is None
            and http_status is None
        )
    else:
        valid_payload = (
            isinstance(validation_error, str)
            and bool(validation_error.strip())
            and len(validation_error) <= 2_000
            and invalid_response is None
            and invalid_hash is None
            and valid_diagnostic
        )
    if not valid_payload:
        raise RuntimeError("schema v25 card-ledger attempt outcome payload is invalid")


def _upgrade_card_ledger_attempt_diagnostics_v25(database: "Database") -> None:
    """Add safe S2 failure routing metadata without changing prior evidence."""
    _ensure_column(
        database,
        "anki_card_ledger_attempts",
        "diagnostic_source",
        "VARCHAR(40)",
    )
    _ensure_column(database, "anki_card_ledger_attempts", "http_status", "INTEGER")


def _rebuild_legacy_outline_outputs_v19(database: "Database") -> None:
    """Rebuild v19 outlines before metadata creates audit FKs.

    SQLite rewrites inbound foreign-key targets when a referenced table is
    renamed.  This must happen before ``create_schema`` can create
    ``existing_artifact_imports.outline_id``.
    """
    inspector = inspect(database.engine)
    if not inspector.has_table("outline_outputs"):
        return
    columns: dict[str, Any] = {
        str(item["name"]): item for item in inspector.get_columns("outline_outputs")
    }
    if not bool(columns.get("job_id", {}).get("nullable") is False):
        return
    with database.engine.begin() as connection:
        # A current-schema database deliberately downgraded for this legacy
        # migration can still have the v22 review triggers.  SQLite validates
        # trigger bodies while renaming this table, so remove only the derived
        # v22 guards; the v22 step recreates their exact definitions later.
        if database.engine.dialect.name == "sqlite":
            connection.execute(text("DROP TRIGGER IF EXISTS trg_outline_replacement_review_insert"))
            connection.execute(text("DROP TRIGGER IF EXISTS trg_outline_replacement_review_update"))
        # A real v19 database has no audit table.  Tests and interrupted local
        # upgrades can leave an empty later table behind; dropping that empty
        # shell avoids SQLite retargeting its outline FK during the rename.
        if inspector.has_table("existing_artifact_imports"):
            count = connection.execute(
                text("SELECT COUNT(*) FROM existing_artifact_imports")
            ).scalar_one()
            if count == 0:
                connection.execute(text("DROP TABLE existing_artifact_imports"))
        connection.execute(text("ALTER TABLE outline_outputs RENAME TO outline_outputs_v19"))
    # With the legacy table out of the way, metadata can create both the new
    # outline table and the audit table against its final name.  Creating the
    # audit table before this rename is what retargeted its FK in SQLite.
    database.create_schema()
    with database.engine.begin() as connection:
        connection.execute(
            text("""
            INSERT INTO outline_outputs
            (id, lecture_id, job_id, path, sha256, current, created_at)
            SELECT id, lecture_id, job_id, path, sha256, current, created_at
            FROM outline_outputs_v19
        """)
        )
        connection.execute(text("DROP TABLE outline_outputs_v19"))


def _upgrade_existing_artifact_import_v20(database: "Database") -> None:
    """Add provenance/audit data without inventing a generation history.

    SQLite cannot relax the old ``outline_outputs.job_id NOT NULL`` constraint
    in place, so the table is rebuilt only when that legacy constraint exists.
    """
    for name, definition in {
        "provenance_kind": "VARCHAR(40) NOT NULL DEFAULT 'llm_cleaned'",
        "import_id": "VARCHAR(36)",
    }.items():
        _ensure_column(database, "study_revisions", name, definition)
    for name, definition in {
        "subject": "VARCHAR(200) NOT NULL DEFAULT ''",
        "exam_number": "INTEGER NOT NULL DEFAULT 0",
        "lecture_number": "INTEGER NOT NULL DEFAULT 0",
        "topic": "VARCHAR(500) NOT NULL DEFAULT ''",
        "canonical_transcript_path": "TEXT",
        "canonical_outline_path": "TEXT",
        "immutable_transcript_path": "TEXT",
        "immutable_outline_path": "TEXT",
        "transcript_filename": "VARCHAR(500)",
        "outline_filename": "VARCHAR(500)",
    }.items():
        _ensure_column(database, "existing_artifact_imports", name, definition)
    inspector = inspect(database.engine)
    if not inspector.has_table("outline_outputs"):
        return
    columns: dict[str, Any] = {
        str(item["name"]): item for item in inspector.get_columns("outline_outputs")
    }
    if bool(columns.get("job_id", {}).get("nullable") is False):
        raise RuntimeError("legacy outline rebuild must run before schema creation")
    else:
        for name, definition in {
            "provenance_kind": "VARCHAR(40) NOT NULL DEFAULT 'notebooklm_generated'",
            "original_filename": "VARCHAR(500)",
            "immutable_path": "TEXT",
            "slide_revision_id": "INTEGER",
            "slide_sha256": "VARCHAR(64)",
            "transcript_revision_id": "INTEGER",
            "transcript_sha256": "VARCHAR(64)",
            "import_id": "VARCHAR(36)",
        }.items():
            _ensure_column(database, "outline_outputs", name, definition)
    with database.engine.begin() as connection:
        duplicate_revision = connection.execute(
            text("""
            SELECT lecture_id, kind FROM study_revisions WHERE current = 1
            GROUP BY lecture_id, kind HAVING COUNT(*) > 1 LIMIT 1
        """)
        ).first()
        duplicate_outline = connection.execute(
            text("""
            SELECT lecture_id FROM outline_outputs WHERE current = 1
            GROUP BY lecture_id HAVING COUNT(*) > 1 LIMIT 1
        """)
        ).first()
        if duplicate_revision or duplicate_outline:
            raise RuntimeError(
                "schema v20 cannot add current-artifact uniqueness: duplicate "
                "current rows exist; resolve them explicitly before migration"
            )
        connection.execute(
            text(
                "CREATE UNIQUE INDEX IF NOT EXISTS "
                "uq_study_revisions_current_lecture_kind "
                "ON study_revisions(lecture_id, kind) WHERE current = 1"
            )
        )
        connection.execute(
            text(
                "CREATE UNIQUE INDEX IF NOT EXISTS "
                "uq_outline_outputs_current_lecture "
                "ON outline_outputs(lecture_id) WHERE current = 1"
            )
        )


def _upgrade_existing_artifact_slide_identity_v21(database: "Database") -> None:
    """Make imported slide source/PDF identities explicit without losing v20 rows."""
    _validate_v20_legacy_slide_identity(database)
    _ensure_column(
        database,
        "existing_artifact_imports",
        "slide_source_sha256",
        "VARCHAR(64)",
    )
    _ensure_column(
        database,
        "existing_artifact_imports",
        "slide_pdf_sha256",
        "VARCHAR(64)",
    )
    _ensure_column(database, "outline_outputs", "slide_source_sha256", "VARCHAR(64)")
    inspector = inspect(database.engine)
    if inspector.has_table("existing_artifact_imports"):
        columns = {item["name"] for item in inspector.get_columns("existing_artifact_imports")}
        with database.engine.begin() as connection:
            if "slide_sha256" in columns:
                connection.execute(
                    text(
                        "UPDATE existing_artifact_imports SET slide_pdf_sha256=slide_sha256 "
                        "WHERE slide_pdf_sha256 IS NULL"
                    )
                )
            connection.execute(
                text(
                    "UPDATE existing_artifact_imports SET "
                    "slide_source_sha256=(SELECT source_sha256 FROM study_revisions "
                    "WHERE id=existing_artifact_imports.slide_revision_id), "
                    "slide_pdf_sha256=(SELECT derived_sha256 FROM study_revisions "
                    "WHERE id=existing_artifact_imports.slide_revision_id) "
                    "WHERE status='complete'"
                )
            )
            connection.execute(
                text(
                    "UPDATE outline_outputs SET slide_source_sha256=(SELECT source_sha256 "
                    "FROM study_revisions WHERE id=outline_outputs.slide_revision_id), "
                    "slide_sha256=(SELECT derived_sha256 FROM study_revisions "
                    "WHERE id=outline_outputs.slide_revision_id) "
                    "WHERE provenance_kind='imported_notebooklm'"
                )
            )
            invalid = connection.execute(
                text(
                    "SELECT id FROM existing_artifact_imports WHERE status='complete' "
                    "AND (slide_source_sha256 IS NULL OR slide_pdf_sha256 IS NULL) LIMIT 1"
                )
            ).first()
            if invalid:
                raise RuntimeError("schema v21 cannot backfill imported slide identity")


def _ensure_study_revision_import_fk(database: "Database") -> None:
    """SQLite must rebuild this legacy table to add the imported-audit FK."""
    if database.engine.dialect.name != "sqlite":
        return
    with database.engine.connect() as connection:
        fks = {
            row[3]: (row[2], row[6])
            for row in connection.execute(text("PRAGMA foreign_key_list(study_revisions)"))
        }
    if fks.get("import_id") == ("existing_artifact_imports", "RESTRICT"):
        return
    columns = (
        "id, upload_item_id, lecture_id, kind, source_sha256, immutable_source_path, "
        "derived_sha256, immutable_derived_path, canonical_source_path, "
        "canonical_derived_path, icloud_path, prompt_sha256, provenance_kind, import_id, "
        "state, current, created_at, promoted_at"
    )
    raw = database.engine.raw_connection()
    try:
        cursor = raw.cursor()
        cursor.execute("PRAGMA foreign_keys=OFF")
        cursor.execute("DROP TRIGGER IF EXISTS trg_outline_replacement_review_insert")
        cursor.execute("DROP TRIGGER IF EXISTS trg_outline_replacement_review_update")
        cursor.execute("DROP TABLE IF EXISTS study_revisions_import_fk")
        cursor.execute("""
            CREATE TABLE study_revisions_import_fk (
                id INTEGER PRIMARY KEY,
                upload_item_id VARCHAR(36) NOT NULL UNIQUE REFERENCES upload_items(id),
                lecture_id INTEGER NOT NULL REFERENCES lectures(id),
                kind VARCHAR(20) NOT NULL,
                source_sha256 VARCHAR(64) NOT NULL,
                immutable_source_path TEXT NOT NULL,
                derived_sha256 VARCHAR(64), immutable_derived_path TEXT,
                canonical_source_path TEXT, canonical_derived_path TEXT, icloud_path TEXT,
                prompt_sha256 VARCHAR(64),
                provenance_kind VARCHAR(40) NOT NULL DEFAULT 'llm_cleaned',
                import_id VARCHAR(36) REFERENCES existing_artifact_imports(id) ON DELETE RESTRICT,
                state VARCHAR(30) NOT NULL DEFAULT 'proposed', current BOOLEAN NOT NULL DEFAULT 0,
                created_at VARCHAR(40) NOT NULL, promoted_at VARCHAR(40),
                UNIQUE(lecture_id, kind, source_sha256)
            )
        """)
        cursor.execute(
            f"INSERT INTO study_revisions_import_fk ({columns}) "
            f"SELECT {columns} FROM study_revisions"
        )
        cursor.execute("DROP TABLE study_revisions")
        cursor.execute("ALTER TABLE study_revisions_import_fk RENAME TO study_revisions")
        cursor.execute(
            "CREATE UNIQUE INDEX uq_study_revisions_current_lecture_kind "
            "ON study_revisions(lecture_id, kind) WHERE current = 1"
        )
        raw.commit()
    finally:
        raw.execute("PRAGMA foreign_keys=ON")
        raw.close()
    _upgrade_outline_replacement_reviews_v22(database)


def _validate_v20_legacy_slide_identity(database: "Database") -> None:
    """Reject contradictory v20 identities before v21 can normalize them."""
    inspector = inspect(database.engine)
    if not inspector.has_table("existing_artifact_imports"):
        return
    audit_columns = {item["name"] for item in inspector.get_columns("existing_artifact_imports")}
    outline_columns = {item["name"] for item in inspector.get_columns("outline_outputs")}
    if "slide_sha256" not in audit_columns or "slide_sha256" not in outline_columns:
        return
    with database.engine.connect() as connection:
        invalid = connection.execute(
            text("""
            SELECT a.id
            FROM existing_artifact_imports a
            LEFT JOIN study_revisions s ON s.id=a.slide_revision_id
            LEFT JOIN outline_outputs o ON o.id=a.outline_id
            WHERE a.status='complete' AND (
                s.id IS NULL
                OR (a.slide_sha256 IS NOT NULL AND a.slide_sha256 != s.derived_sha256)
                OR (o.id IS NOT NULL AND o.provenance_kind='imported_notebooklm'
                    AND o.slide_sha256 IS NOT NULL AND o.slide_sha256 != s.derived_sha256)
            ) LIMIT 1
        """)
        ).first()
    if invalid is not None:
        raise RuntimeError("schema v21 legacy imported slide identity is invalid")


def _validate_complete_v20_import_graph(database: "Database") -> None:
    """Read-only v20 gate: reject bad import graphs before any upgrade DDL."""
    _validate_existing_artifact_graph(database, version=20)


def _validate_complete_v21_import_graph(database: "Database") -> None:
    """Read-only v21 gate before the v22 review table/trigger DDL exists."""
    _validate_existing_artifact_graph(database, version=21)


def _validate_complete_existing_artifact_graph(database: "Database") -> None:
    """Fail closed unless every completed offline import is one coherent graph."""
    _validate_existing_artifact_graph(database, version=LATEST_SCHEMA_VERSION)


def _upgrade_imported_derived_slide_v23(database: "Database") -> None:
    """Add the all-or-none audit identity for explicit derived-PDF adoption."""
    for name, definition in {
        "expected_current_pdf_sha256": "VARCHAR(64)",
        "previous_pdf_sha256": "VARCHAR(64)",
        "previous_immutable_pdf_path": "TEXT",
        "imported_pdf_sha256": "VARCHAR(64)",
        "imported_immutable_pdf_path": "TEXT",
        "derived_provenance": "VARCHAR(40)",
        "adoption_operator": "VARCHAR(200)",
        "adoption_reason": "TEXT",
        "adoption_confirmed_at": "VARCHAR(40)",
        "recovery_phase": "VARCHAR(30)",
    }.items():
        _ensure_column(database, "existing_artifact_imports", name, definition)


def _validate_imported_derived_adoptions_v23(database: "Database") -> None:
    """Validate without repairing: v23 data is evidence, not input to normalize."""
    inspector = inspect(database.engine)
    if not inspector.has_table("existing_artifact_imports"):
        return
    fields = (
        "expected_current_pdf_sha256",
        "previous_pdf_sha256",
        "previous_immutable_pdf_path",
        "imported_pdf_sha256",
        "imported_immutable_pdf_path",
        "derived_provenance",
        "adoption_operator",
        "adoption_reason",
        "adoption_confirmed_at",
        "recovery_phase",
    )
    with database.engine.connect() as connection:
        rows = connection.execute(
            text("""
            SELECT a.*, s.source_sha256 AS slide_source,
                s.derived_sha256 AS slide_pdf, s.immutable_source_path AS slide_immutable_source,
                s.canonical_source_path AS slide_canonical_source,
                s.immutable_derived_path AS slide_path,
                s.provenance_kind AS slide_provenance, s.import_id AS slide_import,
                s.lecture_id AS slide_lecture, s.current AS slide_current, s.kind AS slide_kind,
                s.canonical_derived_path AS slide_canonical, s.icloud_path AS slide_icloud,
                o.job_id AS current_outline_job, o.path AS current_outline_path,
                o.sha256 AS current_outline_sha, o.provenance_kind AS current_outline_provenance,
                g.lecture_id AS replacement_lecture, g.kind AS replacement_kind,
                g.state AS replacement_state, g.stage AS replacement_stage,
                g.pdf_revision_id AS replacement_slide,
                g.transcript_revision_id AS replacement_transcript,
                r.lecture_id AS review_lecture, r.import_id AS review_import,
                r.operator AS review_operator, r.reason AS review_reason
            FROM existing_artifact_imports a
            LEFT JOIN study_revisions s ON s.id=a.slide_revision_id
            LEFT JOIN outline_outputs o ON o.lecture_id=a.lecture_id AND o.current=1
            LEFT JOIN generation_jobs g ON g.id=o.job_id
            LEFT JOIN outline_replacement_reviews r ON r.generation_job_id=o.job_id
        """)
        ).mappings()
        for row in rows:
            values = [row[field] for field in fields]
            has_any = any(value is not None for value in values)
            if not has_any:
                continue
            if has_any and not all(value is not None for value in values):
                raise RuntimeError("schema v23 imported-derived adoption fields are partial")
            if (
                any(
                    value is None or (isinstance(value, str) and not value.strip())
                    for value in values
                )
                or row["derived_provenance"] != "imported_derived"
                or row["recovery_phase"]
                not in {
                    "preparing",
                    "archive_copying",
                    "archived",
                    "canonical_promoted",
                    "icloud_promoted",
                    "precommit",
                    "recovery_required",
                    "committed",
                }
                or row["previous_pdf_sha256"] != row["expected_current_pdf_sha256"]
                or row["status"] not in {"preparing", "failed", "complete"}
            ):
                raise RuntimeError("schema v23 imported-derived adoption fields are invalid")
            for field in (
                "expected_current_pdf_sha256",
                "previous_pdf_sha256",
                "imported_pdf_sha256",
            ):
                if not re.fullmatch(r"[0-9a-f]{64}", str(row[field])):
                    raise RuntimeError("schema v23 imported-derived adoption hash is invalid")
            try:
                if str(UUID(str(row["id"]))) != row["id"]:
                    raise ValueError
            except ValueError as error:
                raise RuntimeError("schema v23 imported-derived adoption ID is invalid") from error
            complete = row["status"] == "complete"
            incomplete = row["status"] in {"preparing", "failed"}
            approved_outline_replacement = (
                complete
                and row["current_outline_job"] is not None
                and row["current_outline_path"] == row["canonical_outline_path"]
                and row["current_outline_provenance"] == "notebooklm_generated"
                and row["replacement_lecture"] == row["lecture_id"]
                and row["replacement_kind"] == "outline"
                and row["replacement_state"] == "complete"
                and row["replacement_stage"] == "complete"
                and row["replacement_slide"] == row["slide_revision_id"]
                and row["replacement_transcript"] == row["transcript_revision_id"]
                and row["review_lecture"] == row["lecture_id"]
                and row["review_import"] == row["id"]
                and bool(str(row["review_operator"] or "").strip())
                and bool(str(row["review_reason"] or "").strip())
            )
            if (complete and row["recovery_phase"] != "committed") or (
                incomplete and row["recovery_phase"] == "committed"
            ):
                raise RuntimeError("schema v23 imported-derived adoption phase is invalid")
            if complete and (
                row["slide_kind"] != "slides"
                or not row["slide_current"]
                or row["slide_pdf"] != row["imported_pdf_sha256"]
                or row["slide_path"] != row["imported_immutable_pdf_path"]
                or row["slide_provenance"] != "imported_derived"
                or row["slide_import"] != row["id"]
            ):
                raise RuntimeError("schema v23 imported-derived adoption graph is invalid")
            if incomplete and (
                row["slide_kind"] != "slides"
                or not row["slide_current"]
                or row["slide_source"] != row["slide_source_sha256"]
                or row["slide_pdf_sha256"] != row["imported_pdf_sha256"]
                or row["slide_lecture"] != row["lecture_id"]
                or row["slide_pdf"] != row["previous_pdf_sha256"]
                or row["slide_path"] != row["previous_immutable_pdf_path"]
                or row["slide_provenance"] == "imported_derived"
                or row["slide_import"] is not None
            ):
                raise RuntimeError("schema v23 incomplete adoption graph is invalid")
            transcript_path = Path(str(row["immutable_transcript_path"]))
            imported = Path(str(row["imported_immutable_pdf_path"]))
            previous = Path(str(row["previous_immutable_pdf_path"]))
            slide_immutable_source = Path(str(row["slide_immutable_source"]))
            slide_canonical_source = Path(str(row["slide_canonical_source"]))
            canonical = Path(str(row["slide_canonical"]))
            icloud = Path(str(row["slide_icloud"]))
            transcript_canonical = Path(str(row["canonical_transcript_path"]))
            outline_immutable = Path(str(row["immutable_outline_path"]))
            outline_canonical = Path(str(row["canonical_outline_path"]))
            audit_root = transcript_path.parent

            def immutable_root(path: Path) -> Path | None:
                for candidate in path.parents:
                    if candidate.name == "v2" and candidate.parent.name == "artifacts":
                        return candidate
                return None

            v2_root = immutable_root(slide_immutable_source)
            import_root = v2_root.parent / "existing-imports" if v2_root is not None else None
            generated_outline = (
                v2_root.parent
                / "generation"
                / str(row["current_outline_job"])
                / "outline.pdf"
                if approved_outline_replacement and v2_root is not None
                else None
            )

            def safe_path(path: Path, *, require_regular_file: bool) -> bool:
                try:
                    if not path.is_absolute() or any(
                        is_indirection(component) for component in (path, *path.parents)
                    ):
                        return False
                    if not path.parent.is_dir():
                        return False
                    return not require_regular_file or path.is_file()
                except OSError:
                    return False

            def safe_future_path(path: Path) -> bool:
                """Validate a not-yet-created output through every extant ancestor."""
                try:
                    if not path.is_absolute() or any(
                        part in {"", ".", ".."} for part in path.parts
                    ):
                        return False
                    for component in (path, *path.parents):
                        if is_indirection(component):
                            return False
                        if component != path and component.exists() and not component.is_dir():
                            return False
                    return not path.exists() or path.is_file()
                except OSError:
                    return False

            all_paths = (
                slide_immutable_source,
                slide_canonical_source,
                previous,
                imported,
                canonical,
                icloud,
                transcript_path,
                transcript_canonical,
                outline_immutable,
                outline_canonical,
            )
            mandatory_complete_paths = all_paths
            mandatory_incomplete_paths = (
                slide_immutable_source,
                slide_canonical_source,
                previous,
                canonical,
                icloud,
            )
            preparing_precursor = (
                incomplete
                and row["status"] in {"preparing", "failed"}
                and row["recovery_phase"] == "preparing"
            )
            archive_copying = incomplete and row["recovery_phase"] == "archive_copying"
            # The audit transaction precedes four immutable/canonical writes.
            # A process death or fenced-claim loss can therefore leave any
            # exact prefix of those writes behind.  Their persisted spellings
            # still need component-by-component validation, even when their
            # parent directory has not yet been created.
            safe_incomplete_paths = mandatory_incomplete_paths if preparing_precursor else all_paths
            future_paths = (
                transcript_path,
                outline_immutable,
                imported,
                transcript_canonical,
                outline_canonical,
            )
            if (
                transcript_path.name != "cleaned.txt"
                or audit_root.name != row["id"]
                or transcript_path != audit_root / "cleaned.txt"
                or outline_immutable != audit_root / "outline.pdf"
                or imported != audit_root / "derived-slide.pdf"
                or previous == imported
                or is_indirection(audit_root)
                or v2_root is None
                or import_root is None
                or is_indirection(v2_root)
                or is_indirection(import_root)
                or not trusted_existing_directory(import_root)
                or audit_root != import_root / row["id"]
                or not trusted_managed_path(
                    slide_immutable_source,
                    v2_root,
                    require_regular_file=complete,
                )
                or not trusted_managed_path(
                    previous,
                    v2_root,
                    require_regular_file=True,
                )
                or not all(
                    trusted_managed_path(path, import_root, require_regular_file=complete)
                    for path in (imported, transcript_path, outline_immutable)
                )
                or (
                    generated_outline is not None
                    and not safe_path(generated_outline, require_regular_file=True)
                )
                or not all(
                    safe_path(path, require_regular_file=False)
                    for path in (all_paths if complete else safe_incomplete_paths)
                )
                or (
                    preparing_precursor and not all(safe_future_path(path) for path in future_paths)
                )
                or (
                    complete
                    and not all(
                        safe_path(path, require_regular_file=True)
                        for path in mandatory_complete_paths
                    )
                )
                or (
                    incomplete
                    and not all(
                        safe_path(path, require_regular_file=True)
                        for path in mandatory_incomplete_paths
                    )
                )
            ):
                raise RuntimeError("schema v23 imported-derived adoption path is invalid")

            def digest(path: Path) -> str:
                return hashlib.sha256(path.read_bytes()).hexdigest()

            try:
                if preparing_precursor:
                    precursor_files = (
                        (transcript_path, row["transcript_sha256"]),
                        (outline_immutable, row["outline_sha256"]),
                        (transcript_canonical, row["transcript_sha256"]),
                        (outline_canonical, row["outline_sha256"]),
                    )
                    present = tuple(path.exists() for path, _digest in precursor_files)
                    # The importer's order is immutable transcript, immutable
                    # outline, canonical transcript, canonical outline.  A
                    # later persisted output without every predecessor is not
                    # evidence this retry may normalize.
                    if any(present[index] and not all(present[:index]) for index in range(1, 4)):
                        raise RuntimeError(
                            "schema v23 imported-derived adoption precursor state is invalid"
                        )
                    if imported.exists() or any(
                        path.exists() and digest(path) != expected_digest
                        for path, expected_digest in precursor_files
                    ):
                        raise RuntimeError(
                            "schema v23 imported-derived adoption precursor files are invalid"
                        )
                elif archive_copying:
                    precursor_files = (
                        (transcript_path, row["transcript_sha256"]),
                        (outline_immutable, row["outline_sha256"]),
                        (transcript_canonical, row["transcript_sha256"]),
                        (outline_canonical, row["outline_sha256"]),
                    )
                    if any(
                        not path.is_file() or digest(path) != expected_digest
                        for path, expected_digest in precursor_files
                    ):
                        raise RuntimeError(
                            "schema v23 imported-derived adoption "
                            "archive-copying precursors are invalid"
                        )
                if digest(previous) != row["previous_pdf_sha256"] or (
                    imported.exists() and digest(imported) != row["imported_pdf_sha256"]
                ):
                    raise RuntimeError("schema v23 imported-derived adoption files are invalid")
                if complete and any(
                    digest(path) != expected_digest
                    for path, expected_digest in (
                        (slide_immutable_source, row["slide_source_sha256"]),
                        (slide_canonical_source, row["slide_source_sha256"]),
                        (previous, row["previous_pdf_sha256"]),
                        (imported, row["imported_pdf_sha256"]),
                        (canonical, row["imported_pdf_sha256"]),
                        (icloud, row["imported_pdf_sha256"]),
                        (transcript_path, row["transcript_sha256"]),
                        (transcript_canonical, row["transcript_sha256"]),
                        (outline_immutable, row["outline_sha256"]),
                        (
                            outline_canonical,
                            row["current_outline_sha"]
                            if approved_outline_replacement
                            else row["outline_sha256"],
                        ),
                    )
                ):
                    raise RuntimeError("schema v23 imported-derived adoption files are invalid")
                if (
                    generated_outline is not None
                    and digest(generated_outline) != row["current_outline_sha"]
                ):
                    raise RuntimeError("schema v23 imported-derived adoption files are invalid")
                expected_states: tuple[tuple[object, object], ...]
                if complete:
                    expected_states = ((row["imported_pdf_sha256"], row["imported_pdf_sha256"]),)
                elif row["recovery_phase"] == "recovery_required":
                    expected_states = (
                        (row["previous_pdf_sha256"], row["previous_pdf_sha256"]),
                        (row["imported_pdf_sha256"], row["previous_pdf_sha256"]),
                        (row["previous_pdf_sha256"], row["imported_pdf_sha256"]),
                        (row["imported_pdf_sha256"], row["imported_pdf_sha256"]),
                    )
                elif row["recovery_phase"] == "archived":
                    expected_states = (
                        (row["previous_pdf_sha256"], row["previous_pdf_sha256"]),
                        (row["imported_pdf_sha256"], row["previous_pdf_sha256"]),
                    )
                elif row["recovery_phase"] == "canonical_promoted":
                    expected_states = (
                        (row["imported_pdf_sha256"], row["previous_pdf_sha256"]),
                        (row["imported_pdf_sha256"], row["imported_pdf_sha256"]),
                    )
                elif row["recovery_phase"] in {"icloud_promoted", "precommit"}:
                    expected_states = ((row["imported_pdf_sha256"], row["imported_pdf_sha256"]),)
                else:
                    expected_states = ((row["previous_pdf_sha256"], row["previous_pdf_sha256"]),)
                if (digest(canonical), digest(icloud)) not in expected_states:
                    raise RuntimeError("schema v23 imported-derived adoption files are invalid")
                if (
                    row["recovery_phase"] not in {"preparing", "archive_copying"}
                    and not imported.is_file()
                ):
                    raise RuntimeError("schema v23 imported-derived archive is unavailable")
            except OSError as error:
                raise RuntimeError(
                    "schema v23 imported-derived adoption files are unavailable"
                ) from error


def _validate_existing_artifact_graph(database: "Database", *, version: int) -> None:
    """Validate every persisted import edge without normalizing it.

    The v20 form has one legacy slide digest; v21 separates source/PDF
    identities.  Both must describe the same complete audit, upload,
    transcript, outline, slide, and lecture graph before any mutation occurs.
    """
    inspector = inspect(database.engine)
    _validate_required_import_tables(database, version=version)
    audit_columns = {
        item["name"] for item in inspector.get_columns("existing_artifact_imports")
    }
    modern_slide_identity = {
        "slide_source_sha256",
        "slide_pdf_sha256",
    } <= audit_columns
    required_tables = {
        "existing_artifact_imports",
        "lectures",
        "upload_batches",
        "upload_items",
        "study_revisions",
        "outline_outputs",
    }
    assert required_tables <= set(inspector.get_table_names())
    audit_slide_columns = (
        "a.slide_source_sha256 AS audit_source, a.slide_pdf_sha256 AS audit_pdf,"
        if modern_slide_identity
        else "NULL AS audit_source, a.slide_sha256 AS audit_pdf,"
    )
    outline_slide_columns = (
        "o.slide_source_sha256 AS outline_source," if modern_slide_identity else "NULL AS outline_source,"
    )
    with database.engine.connect() as connection:
        foreign_key_errors = connection.execute(text("PRAGMA foreign_key_check")).first()
        if foreign_key_errors is not None:
            raise RuntimeError(f"schema v{version} imported artifact foreign-key check failed")
        rows = connection.execute(
            text(f"""
            SELECT
                a.id AS audit_id, a.lecture_id AS audit_lecture, a.slide_revision_id,
                {audit_slide_columns}
                a.transcript_sha256 AS audit_transcript, a.outline_sha256 AS audit_outline,
                a.transcript_revision_id AS audit_transcript_id, a.outline_id AS audit_outline_id,
                a.subject AS audit_subject, a.exam_number AS audit_exam,
                a.lecture_number AS audit_number, a.topic AS audit_topic,
                a.canonical_transcript_path, a.canonical_outline_path,
                a.immutable_transcript_path, a.immutable_outline_path,
                a.transcript_filename, a.outline_filename,
                l.subject AS lecture_subject, l.exam_number AS lecture_exam,
                l.lecture_number AS lecture_number, l.topic AS lecture_topic,
                s.lecture_id AS slide_lecture, s.kind AS slide_kind, s.current AS slide_current,
                s.state AS slide_state,
                s.source_sha256 AS slide_source, s.derived_sha256 AS slide_pdf,
                t.lecture_id AS transcript_lecture, t.kind AS transcript_kind,
                t.current AS transcript_current, t.provenance_kind AS transcript_provenance,
                t.import_id AS transcript_import, t.source_sha256 AS transcript_source,
                t.derived_sha256 AS transcript_derived, t.state AS transcript_state,
                t.immutable_source_path AS transcript_immutable_source,
                t.immutable_derived_path AS transcript_immutable_derived,
                t.canonical_source_path AS transcript_canonical_source,
                t.canonical_derived_path AS transcript_canonical_derived,
                i.lecture_id AS item_lecture, i.kind AS item_kind,
                i.original_filename AS item_filename, i.staged_path AS item_staged,
                i.sha256 AS item_sha, i.state AS item_state,
                i.manual_assignment AS item_manual, b.kind AS batch_kind,
                b.state AS batch_state,
                o.lecture_id AS outline_lecture, o.current AS outline_current,
                o.provenance_kind AS outline_provenance, o.import_id AS outline_import,
                o.job_id AS outline_job, o.path AS outline_path,
                o.original_filename AS outline_filename_row,
                o.immutable_path AS outline_immutable,
                o.slide_revision_id AS outline_slide_id, {outline_slide_columns}
                o.slide_sha256 AS outline_pdf, o.transcript_revision_id AS outline_transcript_id,
                o.transcript_sha256 AS outline_transcript, o.sha256 AS outline_sha,
                o.id AS imported_outline_id
            FROM existing_artifact_imports a
            LEFT JOIN lectures l ON l.id=a.lecture_id
            LEFT JOIN study_revisions s ON s.id=a.slide_revision_id
            LEFT JOIN study_revisions t ON t.id=a.transcript_revision_id
            LEFT JOIN upload_items i ON i.id=t.upload_item_id
            LEFT JOIN upload_batches b ON b.id=i.batch_id
            LEFT JOIN outline_outputs o ON o.id=a.outline_id
            WHERE a.status='complete'
        """)
        ).mappings()
        for row in rows:
            audit_id = row["audit_id"]
            required = [
                "slide_revision_id",
                "audit_pdf",
                "audit_transcript",
                "audit_outline",
                "audit_transcript_id",
                "audit_outline_id",
                "audit_subject",
                "audit_topic",
                "canonical_transcript_path",
                "canonical_outline_path",
                "immutable_transcript_path",
                "immutable_outline_path",
                "transcript_filename",
                "outline_filename",
            ]
            if modern_slide_identity:
                required.append("audit_source")
            if (
                any(row[key] is None or row[key] == "" for key in required)
                or row["lecture_subject"] != row["audit_subject"]
                or row["lecture_exam"] != row["audit_exam"]
                or row["lecture_number"] != row["audit_number"]
                or row["lecture_topic"] != row["audit_topic"]
                or row["slide_lecture"] != row["audit_lecture"]
                or row["slide_kind"] != "slides"
                or row["slide_pdf"] != row["audit_pdf"]
                or (modern_slide_identity and row["slide_source"] != row["audit_source"])
                or row["transcript_lecture"] != row["audit_lecture"]
                or row["transcript_kind"] != "transcripts"
                or row["transcript_provenance"] != "imported_cleaned"
                or row["transcript_import"] != audit_id
                or row["transcript_source"] != row["audit_transcript"]
                or row["transcript_derived"] != row["audit_transcript"]
                or row["transcript_source"] != row["transcript_derived"]
                or row["transcript_immutable_source"] != row["immutable_transcript_path"]
                or row["transcript_immutable_derived"] != row["immutable_transcript_path"]
                or row["transcript_canonical_source"] != row["canonical_transcript_path"]
                or row["transcript_canonical_derived"] != row["canonical_transcript_path"]
                or row["item_lecture"] != row["audit_lecture"]
                or row["item_kind"] != "transcripts"
                or row["item_filename"] != row["transcript_filename"]
                or row["item_staged"] != row["immutable_transcript_path"]
                or row["item_sha"] != row["audit_transcript"]
                or row["item_state"] != "complete"
                or not row["item_manual"]
                or row["batch_kind"] != "transcripts"
                or row["batch_state"] != "complete"
                or row["outline_lecture"] != row["audit_lecture"]
                or row["outline_provenance"] != "imported_notebooklm"
                or row["outline_import"] != audit_id
                or row["outline_job"] is not None
                or row["outline_path"] != row["canonical_outline_path"]
                or row["outline_filename_row"] != row["outline_filename"]
                or row["outline_immutable"] != row["immutable_outline_path"]
                or row["outline_slide_id"] != row["slide_revision_id"]
                or row["outline_pdf"] != row["audit_pdf"]
                or (modern_slide_identity and row["outline_source"] != row["audit_source"])
                or row["outline_transcript_id"] != row["audit_transcript_id"]
                or row["outline_transcript"] != row["audit_transcript"]
                or row["outline_sha"] != row["audit_outline"]
            ):
                raise RuntimeError(
                    f"schema v{version} imported artifact graph is invalid: {audit_id}"
                )


def _validate_required_import_tables(database: "Database", *, version: int) -> None:
    """A versioned import schema is evidence, never a request to recreate it."""
    required_tables = {
        "existing_artifact_imports",
        "lectures",
        "upload_batches",
        "upload_items",
        "study_revisions",
        "outline_outputs",
    }
    if version >= 22:
        required_tables.add("outline_replacement_reviews")
    present = set(inspect(database.engine).get_table_names())
    if missing := sorted(required_tables - present):
        raise RuntimeError(
            f"schema v{version} imported artifact required table is missing: {missing[0]}"
        )


def _validate_import_schema_structure(database: "Database", *, version: int) -> None:
    """Reject a reconstructed import schema even when its rows happen to cohere."""
    if database.engine.dialect.name != "sqlite":
        return
    inspector = inspect(database.engine)
    expected_columns = {
        "study_revisions": {"provenance_kind", "import_id"},
        "outline_outputs": {
            "job_id",
            "provenance_kind",
            "slide_revision_id",
            "slide_source_sha256",
            "transcript_revision_id",
            "import_id",
        },
        "existing_artifact_imports": {
            "id",
            "bundle_sha256",
            "lecture_id",
            "slide_revision_id",
            "slide_source_sha256",
            "slide_pdf_sha256",
            "transcript_revision_id",
            "outline_id",
        },
    }
    if version >= 22:
        expected_columns["outline_replacement_reviews"] = {
            "generation_job_id",
            "lecture_id",
            "import_id",
            "operator",
            "reason",
        }
    if version >= 23:
        expected_columns["existing_artifact_imports"] |= {
            "expected_current_pdf_sha256",
            "previous_pdf_sha256",
            "previous_immutable_pdf_path",
            "imported_pdf_sha256",
            "imported_immutable_pdf_path",
            "derived_provenance",
            "adoption_operator",
            "adoption_reason",
            "adoption_confirmed_at",
            "recovery_phase",
        }
    for table, columns in expected_columns.items():
        actual = {column["name"] for column in inspector.get_columns(table)}
        if missing := sorted(columns - actual):
            raise RuntimeError(
                f"schema v{version} import structural column is missing: {table}.{missing[0]}"
            )

    def foreign_keys(table: str) -> dict[str, tuple[str, str]]:
        with database.engine.connect() as connection:
            return {
                row[3]: (row[2], row[6])
                for row in connection.execute(text(f"PRAGMA foreign_key_list({table})"))
            }

    expected_fks = {
        "outline_outputs": {
            "job_id": ("generation_jobs", "NO ACTION"),
            "slide_revision_id": ("study_revisions", "RESTRICT"),
            "transcript_revision_id": ("study_revisions", "RESTRICT"),
            "import_id": ("existing_artifact_imports", "RESTRICT"),
        },
        "existing_artifact_imports": {
            "lecture_id": ("lectures", "NO ACTION"),
            "slide_revision_id": ("study_revisions", "RESTRICT"),
            "transcript_revision_id": ("study_revisions", "RESTRICT"),
            "outline_id": ("outline_outputs", "RESTRICT"),
        },
    }
    if version >= 22:
        expected_fks["study_revisions"] = {"import_id": ("existing_artifact_imports", "RESTRICT")}
        expected_fks["outline_replacement_reviews"] = {
            "generation_job_id": ("generation_jobs", "RESTRICT"),
            "lecture_id": ("lectures", "RESTRICT"),
            "import_id": ("existing_artifact_imports", "RESTRICT"),
        }
    for table, expected_fk in expected_fks.items():
        actual_fks = foreign_keys(table)
        if any(actual_fks.get(column) != target for column, target in expected_fk.items()):
            raise RuntimeError(f"schema v{version} import foreign-key contract is invalid: {table}")

    def has_unique(table: str, columns: tuple[str, ...]) -> bool:
        with database.engine.connect() as connection:
            indexes = connection.execute(text(f"PRAGMA index_list({table})")).all()
            for index in indexes:
                if not index[2]:
                    continue
                found = tuple(
                    row[2] for row in connection.execute(text(f"PRAGMA index_info({index[1]})"))
                )
                if found == columns:
                    return True
        return False

    if not has_unique("existing_artifact_imports", ("bundle_sha256",)):
        raise RuntimeError(f"schema v{version} import unique contract is invalid: bundle_sha256")
    if not has_unique("outline_outputs", ("job_id",)):
        raise RuntimeError(f"schema v{version} import unique contract is invalid: job_id")
    if version >= 22:
        with database.engine.connect() as connection:
            review_columns = connection.execute(
                text("PRAGMA table_info(outline_replacement_reviews)")
            ).all()
        pk = tuple(row[1] for row in review_columns if row[5])
        if pk != ("generation_job_id",):
            raise RuntimeError("schema v22 outline replacement review primary-key is invalid")
        confirmed_at = next((row for row in review_columns if row[1] == "confirmed_at"), None)
        if confirmed_at is None or not confirmed_at[3]:
            raise RuntimeError("schema v22 outline replacement review confirmed-at is invalid")


def _validate_current_artifact_indexes(database: "Database") -> None:
    """A v21 database must retain the exact partial-current uniqueness contract."""
    expected = {
        "uq_study_revisions_current_lecture_kind": (
            "study_revisions",
            "CREATE UNIQUE INDEX uq_study_revisions_current_lecture_kind "
            "ON study_revisions (lecture_id, kind) WHERE current = 1",
        ),
        "uq_outline_outputs_current_lecture": (
            "outline_outputs",
            "CREATE UNIQUE INDEX uq_outline_outputs_current_lecture "
            "ON outline_outputs (lecture_id) WHERE current = 1",
        ),
    }
    if database.engine.dialect.name != "sqlite":
        for name, (table, _) in expected.items():
            index = next(
                (
                    item
                    for item in inspect(database.engine).get_indexes(table)
                    if item["name"] == name
                ),
                None,
            )
            if index is None or not index.get("unique"):
                raise RuntimeError(
                    f"schema v21 current-artifact index is missing or invalid: {name}"
                )
        return
    with database.engine.connect() as connection:
        for name, (_, definition) in expected.items():
            actual = connection.execute(
                text("SELECT sql FROM sqlite_master WHERE type='index' AND name=:name"),
                {"name": name},
            ).scalar_one_or_none()

            def normalize(sql: str) -> str:
                return " ".join(sql.split()).replace(" (", "(").casefold()

            if actual is None or normalize(actual) != normalize(definition):
                raise RuntimeError(
                    f"schema v21 current-artifact index is missing or invalid: {name}"
                )


_REVIEW_IDENTITY_PREDICATE = """NOT EXISTS (
    SELECT 1
    FROM generation_jobs j
    JOIN existing_artifact_imports a ON a.id=NEW.import_id
    JOIN lectures l ON l.id=NEW.lecture_id
    JOIN outline_outputs io ON io.id=a.outline_id
    JOIN study_revisions s ON s.id=a.slide_revision_id
    JOIN study_revisions t ON t.id=a.transcript_revision_id
    JOIN upload_items i ON i.id=t.upload_item_id
    JOIN upload_batches b ON b.id=i.batch_id
    JOIN notebook_mappings n ON n.remote_notebook_id=j.notebook_id
        AND n.subject=l.subject AND n.exam_number=l.exam_number
    JOIN notebook_source_mappings ps ON ps.notebook_mapping_id=n.id
        AND ps.lecture_id=NEW.lecture_id AND ps.study_revision_id=s.id
        AND ps.source_kind='lecture_pdf' AND ps.source_sha256=s.derived_sha256
        AND ps.remote_source_id=j.pdf_source_id AND ps.state='ready'
    JOIN notebook_source_mappings ts ON ts.notebook_mapping_id=n.id
        AND ts.lecture_id=NEW.lecture_id AND ts.study_revision_id=t.id
        AND ts.source_kind='cleaned_transcript' AND ts.source_sha256=t.derived_sha256
        AND ts.remote_source_id=j.transcript_source_id AND ts.state='ready'
    WHERE j.id=NEW.generation_job_id AND j.lecture_id=NEW.lecture_id
        AND j.kind='outline' AND j.state='failed' AND j.stage='pdf'
        AND trim(COALESCE(j.notebook_answer, '')) != ''
        AND trim(COALESCE(j.notebook_id, '')) != ''
        AND trim(COALESCE(j.pdf_source_id, '')) != ''
        AND trim(COALESCE(j.transcript_source_id, '')) != ''
        AND j.pdf_revision_id=a.slide_revision_id
        AND j.transcript_revision_id=a.transcript_revision_id
        AND a.lecture_id=NEW.lecture_id AND a.status='complete'
        AND a.subject=l.subject AND a.exam_number=l.exam_number
        AND a.lecture_number=l.lecture_number AND a.topic=l.topic
        AND s.lecture_id=NEW.lecture_id AND s.kind='slides'
        AND s.source_sha256=a.slide_source_sha256 AND s.derived_sha256=a.slide_pdf_sha256
        AND t.lecture_id=NEW.lecture_id AND t.kind='transcripts'
        AND t.source_sha256=a.transcript_sha256 AND t.derived_sha256=a.transcript_sha256
        AND t.provenance_kind='imported_cleaned' AND t.import_id=a.id
        AND t.current=1 AND t.state='current'
        AND t.immutable_source_path=a.immutable_transcript_path
        AND t.immutable_derived_path=a.immutable_transcript_path
        AND t.canonical_source_path=a.canonical_transcript_path
        AND t.canonical_derived_path=a.canonical_transcript_path
        AND i.lecture_id=NEW.lecture_id AND i.kind='transcripts'
        AND i.original_filename=a.transcript_filename
        AND i.staged_path=a.immutable_transcript_path AND i.sha256=a.transcript_sha256
        AND i.state='complete' AND i.manual_assignment=1
        AND b.kind='transcripts' AND b.state='complete'
        AND io.lecture_id=NEW.lecture_id AND io.current=1
        AND io.provenance_kind='imported_notebooklm' AND io.import_id=a.id
        AND io.job_id IS NULL AND io.slide_revision_id=a.slide_revision_id
        AND io.slide_source_sha256=a.slide_source_sha256 AND io.slide_sha256=a.slide_pdf_sha256
        AND io.transcript_revision_id=a.transcript_revision_id
        AND io.transcript_sha256=a.transcript_sha256 AND io.sha256=a.outline_sha256
        AND io.path=a.canonical_outline_path AND io.original_filename=a.outline_filename
        AND io.immutable_path=a.immutable_outline_path
        AND s.current=1 AND t.current=1
        AND trim(NEW.operator) != '' AND trim(NEW.reason) != ''
)"""
_REVIEW_INSERT_TRIGGER = f"""CREATE TRIGGER trg_outline_replacement_review_insert
BEFORE INSERT ON outline_replacement_reviews
FOR EACH ROW BEGIN
  SELECT CASE WHEN {_REVIEW_IDENTITY_PREDICATE}
  THEN RAISE(ABORT, 'outline replacement review identity is invalid') END;
END"""
_REVIEW_UPDATE_TRIGGER = f"""CREATE TRIGGER trg_outline_replacement_review_update
BEFORE UPDATE OF generation_job_id, lecture_id, import_id, operator, reason
ON outline_replacement_reviews
FOR EACH ROW BEGIN
  SELECT CASE WHEN {_REVIEW_IDENTITY_PREDICATE}
  THEN RAISE(ABORT, 'outline replacement review identity is invalid') END;
END"""


def _upgrade_outline_replacement_reviews_v22(database: "Database") -> None:
    if database.engine.dialect.name != "sqlite":
        return
    with database.engine.begin() as connection:
        existing = set(
            connection.execute(
                text("SELECT name FROM sqlite_master WHERE type='trigger'")
            ).scalars()
        )
        if "trg_outline_replacement_review_insert" not in existing:
            connection.execute(text(_REVIEW_INSERT_TRIGGER))
        if "trg_outline_replacement_review_update" not in existing:
            connection.execute(text(_REVIEW_UPDATE_TRIGGER))


def _validate_outline_replacement_reviews(database: "Database") -> None:
    if not inspect(database.engine).has_table("outline_replacement_reviews"):
        return
    if database.engine.dialect.name == "sqlite":
        with database.engine.connect() as connection:
            for name, expected in {
                "trg_outline_replacement_review_insert": _REVIEW_INSERT_TRIGGER,
                "trg_outline_replacement_review_update": _REVIEW_UPDATE_TRIGGER,
            }.items():
                actual = connection.execute(
                    text("SELECT sql FROM sqlite_master WHERE type='trigger' AND name=:name"),
                    {"name": name},
                ).scalar_one_or_none()

                def normalize(value: str) -> str:
                    return " ".join(value.split()).casefold()

                if actual is None or normalize(actual) != normalize(expected):
                    raise RuntimeError(
                        f"schema v22 outline replacement review trigger is invalid: {name}"
                    )
    with database.engine.connect() as connection:
        rows = connection.execute(
            text("""
            SELECT r.generation_job_id, r.lecture_id AS review_lecture, r.import_id,
                r.operator, r.reason, j.lecture_id AS job_lecture, j.kind AS job_kind,
                j.stage AS job_stage, j.notebook_answer, j.pdf_revision_id,
                j.transcript_revision_id, j.notebook_id, j.pdf_source_id, j.transcript_source_id,
                j.state AS job_state,
                a.lecture_id AS audit_lecture,
                a.status AS audit_status, a.slide_revision_id,
                a.transcript_revision_id AS audit_transcript,
                a.slide_source_sha256, a.slide_pdf_sha256, a.transcript_sha256,
                s.source_sha256 AS slide_source, s.derived_sha256 AS slide_pdf,
                t.source_sha256 AS transcript_source, t.derived_sha256 AS transcript_derived,
                o.id AS consumed_outline, o.lecture_id AS consumed_lecture,
                o.job_id AS consumed_job, o.provenance_kind AS consumed_provenance,
                o.import_id AS consumed_import, o.current AS consumed_current,
                io.current AS imported_current, cs.id AS current_slide, ct.id AS current_transcript,
                n.id AS mapping_id, ps.id AS pdf_binding_id, ts.id AS transcript_binding_id,
                co.id AS current_generated_id, co.provenance_kind AS current_generated_provenance,
                co.import_id AS current_generated_import
            FROM outline_replacement_reviews r
            LEFT JOIN generation_jobs j ON j.id=r.generation_job_id
            LEFT JOIN existing_artifact_imports a ON a.id=r.import_id
            LEFT JOIN study_revisions s ON s.id=a.slide_revision_id
            LEFT JOIN study_revisions t ON t.id=a.transcript_revision_id
            LEFT JOIN outline_outputs o ON o.job_id=r.generation_job_id
            LEFT JOIN outline_outputs io ON io.id=a.outline_id
            LEFT JOIN lectures l ON l.id=r.lecture_id
            LEFT JOIN notebook_mappings n ON n.remote_notebook_id=j.notebook_id
                AND n.subject=l.subject AND n.exam_number=l.exam_number
            LEFT JOIN notebook_source_mappings ps ON ps.notebook_mapping_id=n.id
                AND ps.lecture_id=r.lecture_id AND ps.study_revision_id=a.slide_revision_id
                AND ps.source_kind='lecture_pdf' AND ps.source_sha256=s.derived_sha256
                AND ps.remote_source_id=j.pdf_source_id AND ps.state='ready'
            LEFT JOIN notebook_source_mappings ts ON ts.notebook_mapping_id=n.id
                AND ts.lecture_id=r.lecture_id AND ts.study_revision_id=a.transcript_revision_id
                AND ts.source_kind='cleaned_transcript' AND ts.source_sha256=t.derived_sha256
                AND ts.remote_source_id=j.transcript_source_id AND ts.state='ready'
            LEFT JOIN study_revisions cs
                ON cs.lecture_id=r.lecture_id AND cs.kind='slides' AND cs.current=1
            LEFT JOIN study_revisions ct
                ON ct.lecture_id=r.lecture_id AND ct.kind='transcripts' AND ct.current=1
            LEFT JOIN outline_outputs co
                ON co.lecture_id=r.lecture_id AND co.current=1
        """)
        ).mappings()
        for row in rows:
            invalid = (
                row["job_lecture"] != row["review_lecture"]
                or row["job_kind"] != "outline"
                or not row["notebook_answer"]
                or not row["notebook_id"]
                or not row["pdf_source_id"]
                or not row["transcript_source_id"]
                or row["audit_lecture"] != row["review_lecture"]
                or row["audit_status"] != "complete"
                or not row["operator"]
                or not row["reason"]
                or row["pdf_revision_id"] != row["slide_revision_id"]
                or row["transcript_revision_id"] != row["audit_transcript"]
                or row["slide_source"] != row["slide_source_sha256"]
                or row["slide_pdf"] != row["slide_pdf_sha256"]
                or row["transcript_source"] != row["transcript_sha256"]
                or row["transcript_derived"] != row["transcript_sha256"]
                or (
                    row["consumed_outline"] is None
                    and (
                        row["job_stage"] != "pdf"
                        or row["job_state"] not in {"failed", "queued", "running"}
                        or not row["imported_current"]
                        or row["current_slide"] != row["slide_revision_id"]
                        or row["current_transcript"] != row["audit_transcript"]
                        or row["mapping_id"] is None
                        or row["pdf_binding_id"] is None
                        or row["transcript_binding_id"] is None
                    )
                )
                or (
                    row["consumed_outline"] is not None
                    and (
                        row["job_state"] != "complete"
                        or row["job_stage"] != "complete"
                        or row["consumed_lecture"] != row["review_lecture"]
                        or row["consumed_job"] != row["generation_job_id"]
                        or row["consumed_provenance"] != "notebooklm_generated"
                        or row["consumed_import"] is not None
                        or row["mapping_id"] is None
                        or row["pdf_binding_id"] is None
                        or row["transcript_binding_id"] is None
                        or row["imported_current"]
                        or (
                            not row["consumed_current"]
                            and (
                                row["current_generated_id"] is None
                                or row["current_generated_provenance"] != "notebooklm_generated"
                                or row["current_generated_import"] is not None
                            )
                        )
                    )
                )
            )
            if invalid:
                raise RuntimeError(
                    "schema v22 outline replacement review row is invalid: "
                    f"{row['generation_job_id']}"
                )


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
        publication_conflicts = connection.execute(
            text(
                "SELECT destination_subject_key, destination_exam_number, label_key "
                "FROM published_quizzes WHERE active = 1 AND studio_run_id IS NOT NULL "
                "GROUP BY destination_subject_key, destination_exam_number, label_key "
                "HAVING COUNT(*) > 1"
            )
        ).mappings().all()
        if publication_conflicts:
            conflict = publication_conflicts[0]
            raise StudioPublicationMigrationConflict(
                "migration recovery conflict: multiple active Studio publications exist "
                f"for {conflict['destination_subject_key']} exam "
                f"{conflict['destination_exam_number']} label {conflict['label_key']}"
            )
        # Older Studio databases can contain multiple active rows from before
        # the partial index existed.  Repair them deterministically before
        # creating the guard, otherwise the upgrade itself prevents startup.
        duplicates = connection.execute(
            text(
                "SELECT destination_subject_key, destination_exam_number, label_key "
                "FROM studio_runs WHERE state IN ('queued', 'running', 'retrying') "
                "GROUP BY destination_subject_key, destination_exam_number, label_key "
                "HAVING COUNT(*) > 1"
            )
        ).mappings().all()
        for group in duplicates:
            rows = connection.execute(
                text(
                    "SELECT id FROM studio_runs WHERE destination_subject_key=:subject "
                    "AND destination_exam_number=:exam AND label_key=:label "
                    "AND state IN ('queued', 'running', 'retrying') "
                    "ORDER BY created_at, id"
                ),
                {
                    "subject": group["destination_subject_key"],
                    "exam": group["destination_exam_number"],
                    "label": group["label_key"],
                },
            ).mappings().all()
            owner_ids = connection.execute(
                text(
                    "SELECT DISTINCT studio_run_id FROM published_quizzes "
                    "WHERE destination_subject_key=:subject "
                    "AND destination_exam_number=:exam AND label_key=:label "
                    "AND active = 1 AND studio_run_id IS NOT NULL"
                ),
                {
                    "subject": group["destination_subject_key"],
                    "exam": group["destination_exam_number"],
                    "label": group["label_key"],
                },
            ).scalars().all()
            retained_id = owner_ids[0] if owner_ids else rows[0]["id"]
            active_ids = {row["id"] for row in rows}
            retain_active_owner = retained_id in active_ids
            for row in rows:
                if retain_active_owner and row["id"] == retained_id:
                    continue
                connection.execute(
                    text(
                        "UPDATE studio_runs SET state='failed', next_attempt_at=NULL, "
                        "diagnostic_source='migration', error=:error WHERE id=:id"
                    ),
                    {
                        "id": row["id"],
                        "error": (
                            "migration active-label conflict; retained active publication "
                            f"owner {retained_id}"
                            if owner_ids
                            else f"migration active-label conflict; retained run {retained_id}"
                        ),
                    },
                )
        connection.execute(
            text(
                "CREATE UNIQUE INDEX IF NOT EXISTS ix_studio_runs_active_label "
                "ON studio_runs "
                "(destination_subject_key, destination_exam_number, label_key) "
                "WHERE state IN ('queued', 'running', 'retrying')"
            )
        )


def _upgrade_studio_durability_v19(database: "Database") -> None:
    """Add additive local-import defaults and external-source operation journal."""
    _ensure_column(database, "studio_sources", "import_role", "VARCHAR(40)")
    _ensure_column(
        database,
        "studio_sources",
        "import_attach_to_notebook",
        "BOOLEAN NOT NULL DEFAULT 0",
    )


def _upgrade_transcript_cleaning_reservation_v20(database: "Database") -> None:
    """Fence one paid first-transcript cleaning owner per lecture."""
    if database.engine.dialect.name != "sqlite":
        return
    if not inspect(database.engine).has_table("study_revisions"):
        return
    with database.engine.begin() as connection:
        connection.execute(
            text(
                "CREATE UNIQUE INDEX IF NOT EXISTS "
                "uq_study_revisions_transcript_cleaning_lecture "
                "ON study_revisions(lecture_id) "
                "WHERE kind='transcripts' AND state='cleaning'"
            )
        )


def _upgrade_studio_source_operation_claims_v20(database: "Database") -> None:
    """Add recoverable compare-and-swap leases for external source mutations."""
    _ensure_column(
        database,
        "studio_source_operations",
        "lease_owner",
        "VARCHAR(100)",
    )
    _ensure_column(
        database,
        "studio_source_operations",
        "lease_expires_at",
        "VARCHAR(40)",
    )


def _upgrade_studio_source_scope_fence_v21(database: "Database") -> None:
    """Reserve a logical notebook before any remote source mutation begins."""
    _ensure_column(
        database,
        "studio_source_operations",
        "subject_key",
        "VARCHAR(100)",
    )
    _ensure_column(
        database,
        "studio_source_operations",
        "exam_number",
        "INTEGER",
    )
    if database.engine.dialect.name != "sqlite":
        return
    if not inspect(database.engine).has_table("studio_source_operations"):
        return
    active_states = "('queued', 'executing', 'reconciling', 'deleting')"
    with database.engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE studio_source_operations SET "
                "subject_key=(SELECT subject_key FROM studio_sources "
                "WHERE studio_sources.id=studio_source_operations.source_id), "
                "exam_number=(SELECT exam_number FROM studio_sources "
                "WHERE studio_sources.id=studio_source_operations.source_id) "
                "WHERE subject_key IS NULL OR exam_number IS NULL"
            )
        )
        active = connection.execute(
            text(
                "SELECT id, source_id, subject_key, exam_number "
                "FROM studio_source_operations "
                f"WHERE state IN {active_states} "
                "AND subject_key IS NOT NULL AND exam_number IS NOT NULL "
                "ORDER BY created_at, id"
            )
        ).mappings()
        retained_by_scope: dict[tuple[str, int], str] = {}
        for operation in active:
            scope = (operation["subject_key"], operation["exam_number"])
            retained_id = retained_by_scope.setdefault(scope, operation["id"])
            if retained_id == operation["id"]:
                continue
            message = (
                "migration notebook-scope conflict; manual review is required; "
                f"retained operation {retained_id}"
            )
            connection.execute(
                text(
                    "UPDATE studio_source_operations SET state='needs_review', "
                    "lease_owner=NULL, lease_expires_at=NULL, "
                    "diagnostic_source='migration', error=:error WHERE id=:id"
                ),
                {"id": operation["id"], "error": message},
            )
            connection.execute(
                text(
                    "UPDATE studio_sources SET state='needs_review', "
                    "next_attempt_at=NULL, diagnostic_source='migration', "
                    "error=:error WHERE id=:id"
                ),
                {"id": operation["source_id"], "error": message},
            )
        connection.execute(
            text(
                "CREATE UNIQUE INDEX IF NOT EXISTS "
                "ix_studio_source_operations_scope_active "
                "ON studio_source_operations(subject_key, exam_number) "
                "WHERE subject_key IS NOT NULL AND exam_number IS NOT NULL "
                f"AND state IN {active_states}"
            )
        )


def _upgrade_notebook_scope_leases_v22(database: "Database") -> None:
    """Create the cross-worker NotebookLM scope reservation table."""
    if not inspect(database.engine).has_table("notebook_scope_leases"):
        database.create_schema()


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


def _upgrade_studio_history_v16(database: "Database") -> None:
    """Keep run-history removal separate from the published quiz record."""
    _ensure_column(database, "studio_runs", "history_hidden_at", "VARCHAR(40)")


def _upgrade_runtime_settings_v17(database: "Database") -> None:
    """Ensure the remote-safe setting tables exist on an upgraded install.

    ``create_schema`` creates the additive tables.  Keep this named migration
    so schema version 17 documents the recovery-boundary setting surface.
    """
    inspector = inspect(database.engine)
    if not inspector.has_table(RuntimeSettingModel.__tablename__):
        database.create_schema()
    if not inspector.has_table(RuntimeSettingAuditModel.__tablename__):
        database.create_schema()


def _upgrade_published_quiz_display_order_v18(database: "Database") -> None:
    """Add durable, additive ordering for the public quiz libraries.

    Older rows deliberately start tied at zero.  The repository resolves those
    ties deterministically and assigns a contiguous order before the first
    manual move, which keeps upgrades idempotent and makes every old row
    reorderable.
    """
    _ensure_column(
        database,
        "published_quizzes",
        "display_order",
        "INTEGER NOT NULL DEFAULT 0",
    )


def _upgrade_anki_replay_inputs_v19(database: "Database") -> None:
    """Add durable v2 lecture pins and immutable per-stage replay documents."""
    for name, definition in {
        "lecture_title_snapshot": "TEXT",
        "lecture_metadata_json": "TEXT",
        "lecture_metadata_sha256": "VARCHAR(64)",
    }.items():
        _ensure_column(database, "anki_curation_jobs", name, definition)
    # ``create_schema`` creates the new table on both clean installs and upgrades.
    # Keep this explicit for installations whose metadata creation is customized.
    if not inspect(database.engine).has_table("anki_stage_replay_inputs"):
        database.create_schema()


def _upgrade_published_quiz_flags_v23(database: "Database") -> None:
    """Additive aggregate public-question flags; fresh schemas already have it."""
    if not inspect(database.engine).has_table("published_quiz_flags"):
        database.create_schema()


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


def _preflight_current_artifact_uniqueness(database: "Database") -> None:
    """Run before metadata creates v20 partial indexes on an old database."""
    inspector = inspect(database.engine)
    if not inspector.has_table("study_revisions"):
        return
    with database.engine.connect() as connection:
        duplicate_revision = connection.execute(
            text(
                "SELECT lecture_id, kind FROM study_revisions WHERE current = 1 "
                "GROUP BY lecture_id, kind HAVING COUNT(*) > 1 LIMIT 1"
            )
        ).first()
        duplicate_outline = None
        if inspector.has_table("outline_outputs"):
            duplicate_outline = connection.execute(
                text(
                    "SELECT lecture_id FROM outline_outputs WHERE current = 1 "
                    "GROUP BY lecture_id HAVING COUNT(*) > 1 LIMIT 1"
                )
            ).first()
    if duplicate_revision or duplicate_outline:
        raise RuntimeError(
            "schema v20 cannot add current-artifact uniqueness: duplicate "
            "current rows exist; resolve them explicitly before migration"
        )


def _has_reconciled_v29_schema(database: "Database") -> bool:
    """Detect the two historical schema-29 variants before taking the read-only path."""
    inspector = inspect(database.engine)
    required_tables = {
        "notebook_scope_leases",
        "published_quiz_flags",
        "studio_source_operations",
    }
    if not required_tables <= set(inspector.get_table_names()):
        return False
    studio_source_columns = {
        column["name"] for column in inspector.get_columns("studio_sources")
    }
    return {"import_attach_to_notebook", "import_role"} <= studio_source_columns


def _is_deployed_study_hub_v23_schema(database: "Database", version: int | None) -> bool:
    """Identify the deployed non-Anki schema that independently used version 23."""
    if version != 23:
        return False
    inspector = inspect(database.engine)
    tables = set(inspector.get_table_names())
    nuc_tables = {
        "notebook_scope_leases",
        "published_quiz_flags",
        "studio_source_operations",
    }
    import_tables = {"existing_artifact_imports", "outline_replacement_reviews"}
    if not nuc_tables <= tables or import_tables & tables:
        return False
    studio_source_columns = {
        column["name"] for column in inspector.get_columns("studio_sources")
    }
    return {"import_attach_to_notebook", "import_role"} <= studio_source_columns


def migrate_database(database: "Database") -> None:
    # A populated current schema is an integrity check, not an opportunity to
    # rewrite persisted identities.  Keep this branch read-only.
    inspector = inspect(database.engine)
    if inspector.has_table("schema_version"):
        with database.engine.connect() as connection:
            version = connection.execute(
                text("SELECT version FROM schema_version WHERE id=1")
            ).scalar_one_or_none()
        deployed_study_hub_v23 = _is_deployed_study_hub_v23_schema(
            database, version
        )
        if version is not None and version >= 20 and not deployed_study_hub_v23:
            _validate_required_import_tables(database, version=version)
        if (
            version is not None
            and version >= LATEST_SCHEMA_VERSION
            and _has_reconciled_v29_schema(database)
        ):
            _validate_import_schema_structure(database, version=version)
            _validate_complete_existing_artifact_graph(database)
            _validate_current_artifact_indexes(database)
            _validate_outline_replacement_reviews(database)
            _validate_imported_derived_adoptions_v23(database)
            _validate_card_ledger_attempts_v25(database)
            _validate_provider_attempt_events_v26(database)
            _validate_course_curation_policy_v28(database)
            _validate_v3_durable_reservations_v29(database)
            return
        if version == 20:
            _validate_complete_v20_import_graph(database)
        if version == 21:
            _validate_import_schema_structure(database, version=version)
            _validate_complete_v21_import_graph(database)
            _validate_current_artifact_indexes(database)
        if version == 22:
            _validate_import_schema_structure(database, version=version)
            _validate_existing_artifact_graph(database, version=version)
            _validate_current_artifact_indexes(database)
            _validate_outline_replacement_reviews(database)
        if version == 23 and not deployed_study_hub_v23:
            # A v23 database is already a complete, immutable import graph.
            # Validate it before create_schema can materialize any v24 table.
            _validate_import_schema_structure(database, version=version)
            _validate_existing_artifact_graph(database, version=version)
            _validate_current_artifact_indexes(database)
            _validate_outline_replacement_reviews(database)
            _validate_imported_derived_adoptions_v23(database)
    _preflight_current_artifact_uniqueness(database)
    _rebuild_legacy_outline_outputs_v19(database)
    database.create_schema()
    _upgrade_generation_job_columns(database)
    _upgrade_studio_columns(database)
    _upgrade_studio_run_active_label_index(database)
    _upgrade_quiz_import_v15(database)
    _upgrade_studio_history_v16(database)
    _upgrade_runtime_settings_v17(database)
    _upgrade_published_quiz_display_order_v18(database)
    _upgrade_studio_durability_v19(database)
    _upgrade_transcript_cleaning_reservation_v20(database)
    _upgrade_studio_source_operation_claims_v20(database)
    _upgrade_studio_source_scope_fence_v21(database)
    _upgrade_notebook_scope_leases_v22(database)
    _upgrade_published_quiz_flags_v23(database)
    _upgrade_anki_replay_inputs_v19(database)
    _upgrade_existing_artifact_import_v20(database)
    _upgrade_existing_artifact_slide_identity_v21(database)
    _ensure_study_revision_import_fk(database)
    _upgrade_outline_replacement_reviews_v22(database)
    _upgrade_imported_derived_slide_v23(database)
    _upgrade_card_ledger_attempt_diagnostics_v25(database)
    _upgrade_provider_attempt_subcall_ordinal_v27(database)
    _upgrade_course_curation_policy_v28(database)
    _upgrade_v3_durable_reservations_v29(database)
    _validate_import_schema_structure(database, version=LATEST_SCHEMA_VERSION)
    _validate_complete_existing_artifact_graph(database)
    _validate_current_artifact_indexes(database)
    _validate_outline_replacement_reviews(database)
    _validate_imported_derived_adoptions_v23(database)
    _validate_card_ledger_attempts_v25(database)
    _validate_provider_attempt_events_v26(database)
    _validate_course_curation_policy_v28(database)
    _validate_v3_durable_reservations_v29(database)
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
