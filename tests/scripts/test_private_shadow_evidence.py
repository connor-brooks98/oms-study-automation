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
COMMON = ROOT / "scripts" / "task28" / "private-shadow-common.ps1"


def test_private_shadow_evidence_contract_is_durable_and_provider_agnostic() -> None:
    evidence = EVIDENCE.read_text(encoding="utf-8")
    wrapper = WRAPPER.read_text(encoding="utf-8")
    harness = HARNESS.read_text(encoding="utf-8")
    common = COMMON.read_text(encoding="utf-8")

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
    assert '("a" * 64 -join "")' in harness
    assert "provider_bad_request" in harness
    assert "PRIVATE_SHADOW_ENTRYPOINT_CONVERTER_VERIFIED" in harness
    assert "safe_result_write" in harness
    assert "operator_artifacts_deleted" in wrapper
    assert "raw_content_retained" in wrapper
    assert '"PYTHONDONTWRITEBYTECODE"] = "1"' in evidence
    assert '"TEMP"] = $ScratchRoot' in evidence
    assert '"TMP"] = $ScratchRoot' in evidence
    assert "PRIVATE_SHADOW_ENVIRONMENT_VERIFIED" in harness
    assert "function Set-PrivateShadowChildEnvironment" in evidence
    assert "[string]$ScratchRoot" in evidence
    assert "Set-CompositionVerifyEnvironment" not in evidence
    assert "Set-CompositionVerifyEnvironment" not in wrapper
    assert "DIRECT_CONVERTER_ENVIRONMENT_VERIFIED" in harness
    assert 'EnvironmentVariables.Remove("OMS_TASK28_COMPOSITION_VERIFY")' in evidence
    assert 'EnvironmentVariables.Remove("OMS_TASK28_PRIVATE_PROJECT")' in evidence
    assert 'EnvironmentVariables.Remove("OMS_TASK28_PRIVATE_DIAGNOSTIC_PATH")' in evidence
    assert "StandardInputEncoding" not in evidence
    assert "StandardInput.BaseStream" not in evidence
    assert "StreamWriter" not in evidence
    assert "$Process.StandardInput.Write($Raw)" in evidence
    assert "$Process.StandardInput.Close()" in evidence
    assert "StandardInputEncoding" not in harness
    assert "StandardInput.BaseStream" not in harness
    assert "StreamWriter" not in harness
    assert "$Process.StandardInput.Write($Raw)" in harness
    assert "$Process.StandardInput.Close()" in harness
    assert "PRIVATE_SHADOW_COMPOSITION_ENVIRONMENT_VERIFIED" in harness
    assert "DIRECT_CONVERTER_SITECUSTOMIZE_REMOVED" in harness
    assert '$DirectValid.Stdout.TrimEnd("`r", "`n") + "`n"' in harness
    assert "SetAccessRuleProtection($true, $false)" in common
    assert "RemoveAccessRuleSpecific" in common
    assert 'icacls.exe $Path /inheritance:r /grant:r' not in common


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
            "-EntryPoint",
            str(ROOT / "scripts" / "private-shadow-operator-entry.py"),
            "-EntryFixture",
            str(ROOT / "tests" / "scripts" / "private_shadow_entrypoint_fixture.py"),
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stderr
    assert "PRIVATE_SHADOW_EVIDENCE_HARNESS_VERIFIED" in result.stdout
