from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace


def _load_source_bound_smoke(project: Path) -> ModuleType:
    try:
        canonical_project = project.resolve(strict=True)
        path = (canonical_project / "scripts" / "run-gemini-contract-smoke.py").resolve(
            strict=True
        )
    except (OSError, RuntimeError) as error:
        raise RuntimeError("private_shadow_smoke_binding_invalid") from error
    if not path.is_relative_to(canonical_project):
        raise RuntimeError("private_shadow_smoke_binding_invalid")
    spec = importlib.util.spec_from_file_location("task_2_8_source_bound_smoke", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("private_shadow_smoke_unavailable")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    if Path(str(module.__file__)).resolve() != path:
        raise RuntimeError("private_shadow_smoke_binding_invalid")
    return module


def _load_hash_bound_evidence(project: Path) -> ModuleType:
    try:
        canonical_project = project.resolve(strict=True)
        source_root = (canonical_project / "src").resolve(strict=True)
        path = (source_root / "oms_hub" / "providers" / "gemini" / "evidence.py").resolve(
            strict=True
        )
    except (OSError, RuntimeError) as error:
        raise RuntimeError("private_shadow_evidence_binding_invalid") from error
    if not source_root.is_relative_to(canonical_project) or not path.is_relative_to(source_root):
        raise RuntimeError("private_shadow_evidence_binding_invalid")
    spec = importlib.util.spec_from_file_location("task_2_8_hash_bound_evidence", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("private_shadow_evidence_unavailable")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    if Path(str(module.__file__)).resolve() != path:
        raise RuntimeError("private_shadow_evidence_binding_invalid")
    return module


def main() -> int:
    scratch = Path(os.environ["OMS_TASK28_PRIVATE_SCRATCH"])
    project = Path(os.environ["OMS_TASK28_PRIVATE_PROJECT"]).resolve()
    database = None
    scratch_valid = False
    failure_evidence: dict[str, object] = {}
    evidence: ModuleType | None = None
    record: dict[str, object] = {"status": "blocked"}
    try:
        reviewed = _load_source_bound_smoke(project)
        evidence = _load_hash_bound_evidence(project)
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
        if evidence is not None:
            try:
                record = evidence.validate_private_shadow_record(failure_evidence, 1)
            except ValueError:
                record = evidence.failure_record(
                    None,
                    RuntimeError("private shadow blocked"),
                    failure_stage="prior_state_check",
                    states=[],
                    cleanup_outcome="unknown",
                    reconciliation_outcome="unknown",
                ).model_dump(mode="json")
    finally:
        cleanup_failed = False
        if database is not None:
            try:
                database.close()
            except BaseException:
                cleanup_failed = True
        if scratch_valid:
            try:
                reviewed._cleanup(scratch)
                scratch.rmdir()
            except BaseException:
                cleanup_failed = True
        if cleanup_failed:
            if evidence is not None:
                record = evidence.failure_record(
                    None,
                    RuntimeError("private shadow cleanup failed"),
                    failure_stage="prior_state_check",
                    states=[],
                    cleanup_outcome="unknown",
                    reconciliation_outcome="unknown",
                ).model_dump(mode="json")
    print(json.dumps(record, sort_keys=True))
    return 0 if record["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
