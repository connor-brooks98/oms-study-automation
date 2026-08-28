from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

from tests.scripts.private_shadow_entrypoint_fixture import corrected_blocked_record

ROOT = Path(__file__).resolve().parents[2]
ENTRYPOINT = ROOT / "scripts" / "private-shadow-operator-entry.py"
HARNESS = ROOT / "tests" / "scripts" / "private_shadow_entrypoint_harness.ps1"

FAILURE_KEYS = {
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


def _load_entrypoint() -> ModuleType:
    assert ENTRYPOINT.is_file(), "composition entrypoint must be committed"
    spec = importlib.util.spec_from_file_location("task_2_8_composition_entrypoint", ENTRYPOINT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_entrypoint_preserves_corrected_blocked_schema_and_has_full_fallback() -> None:
    module = _load_entrypoint()
    corrected = corrected_blocked_record()

    assert module._FAILURE_KEYS == FAILURE_KEYS
    assert module._resolve_failure_record(corrected) == corrected

    fallback = module._resolve_failure_record({"status": "blocked"})
    assert set(fallback) == FAILURE_KEYS
    assert fallback["status"] == "blocked"
    assert fallback["failure_stage"] == "prior_state_check"
    assert fallback["failure_input_identity"] == "none"
    assert fallback["provider_error_category"] == "none"
    assert fallback["provider_status_code"] is None
    assert fallback["provider_reason"] == "none"
    assert fallback["provider_cleanup_outcome"] == "unknown"
    assert fallback["provider_reconciliation_outcome"] == "unknown"
    assert fallback["warnings"] == ["private_shadow_failed", "private_cleanup_unknown"]


def test_entrypoint_harness_binds_actual_entrypoint_to_real_validator() -> None:
    harness = HARNESS.read_text(encoding="utf-8")
    for required in (
        "PRIVATE_SHADOW_ENTRYPOINT_HARNESS_VERIFIED",
        "Convert-PrivateShadowEvidence",
        'foreach ($Mode in @("corrected", "fallback"))',
        "$Process.ExitCode -ne 1",
        "Count -ne 15",
    ):
        assert required in harness
