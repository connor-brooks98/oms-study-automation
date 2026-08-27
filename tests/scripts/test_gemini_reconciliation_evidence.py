from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
EVIDENCE = ROOT / "scripts" / "gemini-reconciliation-evidence.ps1"
WRAPPER = ROOT / "scripts" / "run-gemini-reconciliation.ps1"
HARNESS = ROOT / "tests" / "scripts" / "gemini_reconciliation_evidence_harness.ps1"


def test_reconciliation_wrapper_commits_distinct_evidence_stages_and_retention() -> None:
    evidence = EVIDENCE.read_text(encoding="utf-8")
    wrapper = WRAPPER.read_text(encoding="utf-8")

    assert "RECONCILIATION_EVIDENCE_HARNESS_VERIFIED" in HARNESS.read_text(encoding="utf-8")
    for stage in ("parse", "validation", "safe_result_write"):
        assert stage in evidence
    for exit_code in (41, 42, 43):
        assert str(exit_code) in evidence
    assert "unexpected_stdout_prefix" in evidence
    assert "RetainRaw" in evidence
    assert "evidence_usable" in evidence
    assert "operator_artifacts_deleted" in wrapper
    assert "provider_cleanup_complete" in evidence
    assert "wrapper_failed" not in evidence
    assert "wrapper_failed" not in wrapper
    assert "RUN_GEMINI_RECONCILIATION" in wrapper
    assert "RUN_PRIVATE_GEMINI_SHADOW" not in wrapper


def test_python_json_to_powershell_evidence_path_runs_in_committed_harness() -> None:
    powershell = next(
        (
            executable
            for name in ("powershell.exe", "powershell")
            if (executable := shutil.which(name)) is not None
        ),
        None,
    )
    if powershell is None:
        # macOS cannot execute the Windows evidence harness. Source-contract
        # assertions above remain mandatory; Windows reruns this same harness.
        return
    result = subprocess.run(
        [
            powershell,
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(HARNESS),
            "-EvidenceScript",
            str(EVIDENCE),
            "-PythonExecutable",
            sys.executable,
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    assert "RECONCILIATION_EVIDENCE_HARNESS_VERIFIED" in result.stdout
