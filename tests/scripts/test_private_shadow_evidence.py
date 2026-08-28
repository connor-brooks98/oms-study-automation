from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
EVIDENCE = ROOT / "scripts" / "private-shadow-evidence.ps1"
WRAPPER = ROOT / "scripts" / "run-private-shadow-evidence.ps1"
HARNESS = ROOT / "tests" / "scripts" / "private_shadow_evidence_harness.ps1"


def test_private_shadow_evidence_contract_is_durable_and_provider_agnostic() -> None:
    evidence = EVIDENCE.read_text(encoding="utf-8")
    wrapper = WRAPPER.read_text(encoding="utf-8")
    harness = HARNESS.read_text(encoding="utf-8")

    for key in (
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
    ):
        assert f'"{key}"' in evidence
    for stage in (
        "prior_state_check",
        "create_store",
        "upload_input",
        "import_input",
        "wait_for_import",
        "positive_query",
        "positive_validation",
        "negative_query",
        "negative_validation",
        "cleanup",
        "unknown",
    ):
        assert f'"{stage}"' in evidence
    for outcome in ("complete", "failed", "empty", "not_empty", "unknown"):
        assert f'"{outcome}"' in evidence
    for forbidden in (
        "gemini-api-key",
        "Lecture 13",
        "fileSearchStores/",
        "Get-ScheduledTask",
        "Register-ScheduledTask",
        "Invoke-RestMethod",
        "RUN_PRIVATE_GEMINI_SHADOW",
        "C:\\Users\\",
    ):
        assert forbidden not in evidence
        assert forbidden not in wrapper
        assert forbidden not in harness
    assert "PRIVATE_SHADOW_EVIDENCE_HARNESS_VERIFIED" in harness
    assert "operator_artifacts_deleted" in wrapper
    assert "raw_content_retained" in wrapper


def test_private_shadow_wrapper_runs_committed_synthetic_fault_matrix() -> None:
    powershell = next(
        (
            executable
            for name in ("powershell.exe", "powershell")
            if (executable := shutil.which(name)) is not None
        ),
        None,
    )
    if powershell is None:
        return
    result = subprocess.run(
        [
            powershell,
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(HARNESS),
            "-EvidenceScript",
            str(EVIDENCE),
            "-WrapperScript",
            str(WRAPPER),
            "-PythonExecutable",
            sys.executable,
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stderr
    assert "PRIVATE_SHADOW_EVIDENCE_HARNESS_VERIFIED" in result.stdout
