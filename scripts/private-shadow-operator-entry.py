from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import sys
from pathlib import Path
from types import ModuleType


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
    project = Path(
        os.environ.get("OMS_TASK28_PRIVATE_PROJECT", Path(__file__).resolve().parents[1])
    ).resolve()
    evidence = _load_hash_bound_evidence(project)
    try:
        if os.getenv("OMS_TASK28_COMPOSITION_VERIFY") == "1":
            smoke = _load_source_bound_smoke(project)
            record = asyncio.run(smoke.run_private_shadow_composition_probe())
        else:
            record = evidence.failure_record(
                None,
                RuntimeError("private runtime manifest is not authorized"),
                failure_stage="prior_state_check",
                states=[],
                cleanup_outcome="unknown",
                reconciliation_outcome="unknown",
            ).model_dump(mode="json")
    except BaseException:
        record = evidence.failure_record(
            None,
            RuntimeError("private composition probe failed"),
            failure_stage="prior_state_check",
            states=[],
            cleanup_outcome="unknown",
            reconciliation_outcome="unknown",
        ).model_dump(mode="json")
    exit_code = 0 if record["status"] == "passed" else 1
    record = evidence.validate_private_shadow_record(record, exit_code)
    print(json.dumps(record, sort_keys=True))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
