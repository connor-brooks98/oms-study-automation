from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace


def _load_reviewed_operator() -> ModuleType:
    path = Path(__file__).with_name("private-shadow-operator-reviewed.py")
    spec = importlib.util.spec_from_file_location("task_2_8_reviewed_private_operator", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("private_shadow_operator_unavailable")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


_FAILURE_KEYS = {
    "status",
    "source_revision_hash",
    "document_types",
    "page_count",
    "slide_count",
    "provider_operation_states",
    "byte_usage",
    "failure_stage",
    "failure_input_identity",
    "provider_error_category",
    "provider_status_code",
    "provider_reason",
    "provider_cleanup_outcome",
    "provider_reconciliation_outcome",
    "warnings",
}


def _fail_closed_record() -> dict[str, object]:
    return {
        "status": "blocked",
        "source_revision_hash": "0" * 64,
        "document_types": ["markdown"],
        "page_count": 1,
        "slide_count": 1,
        "provider_operation_states": ["private_shadow_failed"],
        "byte_usage": {"index_inputs": 1},
        "failure_stage": "prior_state_check",
        "failure_input_identity": "none",
        "provider_error_category": "none",
        "provider_status_code": None,
        "provider_reason": "none",
        "provider_cleanup_outcome": "unknown",
        "provider_reconciliation_outcome": "unknown",
        "warnings": ["private_shadow_failed", "private_cleanup_unknown"],
    }


def _resolve_failure_record(failure_evidence: dict[str, object]) -> dict[str, object]:
    if set(failure_evidence) == _FAILURE_KEYS and failure_evidence.get("status") == "blocked":
        return dict(failure_evidence)
    return _fail_closed_record()


def main() -> int:
    reviewed = _load_reviewed_operator()
    scratch = Path(os.environ["OMS_TASK28_PRIVATE_SCRATCH"])
    project = Path(os.environ["OMS_TASK28_PRIVATE_PROJECT"]).resolve()
    database = None
    scratch_valid = False
    failure_evidence: dict[str, object] = {}
    record = _fail_closed_record()
    try:
        if os.getenv("RUN_PRIVATE_GEMINI_SHADOW") != "1":
            raise reviewed.OperatorFailure("private_shadow_opt_in_required")
        reviewed._validate_scratch(scratch)
        scratch_valid = True
        source_sha256, pdf_sha256 = reviewed._approved_hashes(project)
        database_url, study_root = reviewed._runtime_configuration()
        source_database = reviewed.make_url(database_url).database
        if not isinstance(source_database, str):
            raise reviewed.OperatorFailure("source_registry_configuration_failed")
        source_path = Path(source_database)
        source_info = source_path.lstat()
        source_attributes = getattr(source_info, "st_file_attributes", 0)
        source_reparse = getattr(reviewed.stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
        if (
            not reviewed.stat.S_ISREG(source_info.st_mode)
            or reviewed.stat.S_ISLNK(source_info.st_mode)
            or (source_reparse and source_attributes & source_reparse)
        ):
            raise reviewed.OperatorFailure("source_registry_configuration_failed")
        backup = scratch / "registry.db"
        reviewed.backup_sqlite_database(source_path, backup)
        database = reviewed.Database(f"sqlite:///{backup.as_posix()}")
        with database.engine.connect() as connection:
            schema_version = connection.execute(
                reviewed.text("SELECT version FROM schema_version WHERE id = 1")
            ).scalar_one_or_none()
        if schema_version != 29 or isinstance(schema_version, bool):
            raise reviewed.OperatorFailure("source_registry_schema_incompatible")
        artifacts = reviewed.ArtifactService(
            database,
            SimpleNamespace(
                data_dir=scratch,
                study_root=study_root,
                icloud_staging_root=None,
            ),
        )
        revision = reviewed._select_revision(artifacts, source_sha256, pdf_sha256)
        smoke = reviewed._load_smoke(project)
        preflight_root = scratch / "preflight"
        preflight_root.mkdir()
        approved = smoke.run_private_shadow_preflight(
            str(revision.id),
            schema_version=schema_version,
            artifacts=artifacts,
            materialization_root=preflight_root,
        )
        if set(approved) != reviewed._PREFLIGHT_KEYS or approved.get("status") != "ready":
            raise reviewed.OperatorFailure("private_preflight_evidence_invalid")
        reviewed._remove_tree(preflight_root)
        live_root = scratch / "live"
        live_root.mkdir()
        record = asyncio.run(
            smoke.run_authorized_private_shadow(
                str(revision.id),
                schema_version=schema_version,
                artifacts=artifacts,
                materialization_root=live_root,
                approved_preflight=approved,
                failure_evidence=failure_evidence,
            )
        )
        if set(record) != reviewed._LIVE_KEYS or record.get("status") != "passed":
            raise reviewed.OperatorFailure("private_shadow_evidence_invalid")
    except BaseException:
        record = _resolve_failure_record(failure_evidence)
    finally:
        if database is not None:
            database.close()
        if scratch_valid:
            try:
                reviewed._cleanup(scratch)
                scratch.rmdir()
            except BaseException:
                record = _fail_closed_record()
    print(json.dumps(record, sort_keys=True))
    return 0 if record["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
