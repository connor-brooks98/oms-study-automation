from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CLEANUP = ROOT / "scripts" / "cleanup-task28-reconciliation-e71eace.ps1"
HARNESS = ROOT / "tests" / "scripts" / "cleanup_task28_reconciliation_e71eace_harness.ps1"


def test_cleanup_disposition_binds_exact_legacy_result_and_targets() -> None:
    source = CLEANUP.read_text(encoding="utf-8")

    for required in (
        "bd26cc4b7b3eeb34e875caaa043b4118bea375bc4473a3dab34dd65dffdf5a7d",
        "e5ec9fcf1fb790b62a18939d78d350ea1572a903e02fae6e4e916ad5f4661c3f",
        "OMS Sol0 Task28 Reconciliation e71eace",
        "sol0-task28-reconciliation-e71eace",
        "sol0-task28-private-shadow-9097851",
        "sol0-task28-private-shadow-06848e2",
        "System.Management.Automation.PSCustomObject",
        "Unregister-ScheduledTask",
        "TASK28_RECONCILIATION_CLEANUP_VALIDATED",
        "d36c6a64ef342ff0d4e88c370c794a2add46ef2f98fbdfb9dcabd6bd86f702b0",
        "ad8e00b852d32c3b1216452e25e62160a68fb07745f3589321b20fec3ccfc5a7",
        "5a955d65feb3adf03759bd62c8e2f842b2e81a27abfc5c9e10b8912c72796587",
        "0795af225426707b9a49454b19538b6b0eb420a9f05ab74280d1d541fd87fffa",
        "96c77c083d665fe945cde5a31265d83276fe07778a1bb732bccee1b28f1acad2",
        "cleanup_tombstone_state_mismatch",
        "cleanup_path_overlap",
        "cleanup_path_escape",
    ):
        assert required in source
    for forbidden in (
        "gemini-api-key",
        "RUN_GEMINI_RECONCILIATION",
        "run-gemini-reconciliation.py",
        "Start-ScheduledTask",
        "Register-ScheduledTask",
        "RUN_PRIVATE_GEMINI_SHADOW",
    ):
        assert forbidden not in source


def test_cleanup_disposition_runs_in_committed_powershell_harness() -> None:
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
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(HARNESS),
            "-CleanupScript",
            str(CLEANUP),
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    assert "TASK28_RECONCILIATION_CLEANUP_HARNESS_VERIFIED" in result.stdout
