from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
EVIDENCE = ROOT / "scripts" / "private-shadow-evidence.ps1"
WRAPPER = ROOT / "scripts" / "run-private-shadow-evidence.ps1"
HARNESS = ROOT / "tests" / "scripts" / "private_shadow_evidence_harness.ps1"


def test_private_shadow_evidence_contract_is_durable_and_provider_agnostic() -> None:
    evidence = EVIDENCE.read_text(encoding="utf-8")
    wrapper = WRAPPER.read_text(encoding="utf-8")
    harness = HARNESS.read_text(encoding="utf-8")

    assert "-m oms_hub.providers.gemini.evidence" in evidence
    assert "ReadAllText" in evidence
    assert "WriteAllText" in evidence
    assert "File]::Move" in evidence
    assert "oms_hub/providers/gemini/evidence.py" in wrapper
    assert "$EvidenceModule" in wrapper
    assert "Assert-PrivateShadowRecord" not in evidence
    assert "ProviderReasons" not in evidence
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
    assert "provider_bad_request" in harness
    assert "PRIVATE_SHADOW_ENTRYPOINT_CONVERTER_VERIFIED" in harness
    assert "safe_result_write" in harness
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
        pytest.skip("PowerShell is unavailable")
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
            "-SourceRoot",
            str(ROOT / "src"),
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stderr
    assert "PRIVATE_SHADOW_EVIDENCE_HARNESS_VERIFIED" in result.stdout
