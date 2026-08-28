[CmdletBinding()]
param(
  [Parameter(Mandatory = $true)][string]$EvidenceScript,
  [Parameter(Mandatory = $true)][string]$WrapperScript,
  [Parameter(Mandatory = $true)][string]$PythonExecutable
)

$ErrorActionPreference = "Stop"
$Sandbox = Join-Path ([System.IO.Path]::GetTempPath()) (
  "oms-private-shadow-evidence-{0}" -f [Guid]::NewGuid().ToString("N")
)
$Utf8 = [System.Text.UTF8Encoding]::new($false, $true)
New-Item -ItemType Directory -Path $Sandbox | Out-Null
$OperatorRoot = Join-Path $Sandbox "operator"
$CasesRoot = Join-Path $Sandbox "cases"
New-Item -ItemType Directory -Path $OperatorRoot,$CasesRoot | Out-Null
$Emitter = Join-Path $OperatorRoot "emit_private_shadow_json.py"
[System.IO.File]::WriteAllText(
  $Emitter,
  @'
import json
import os
from pathlib import Path

mode = os.environ.get("PRIVATE_SHADOW_FIXTURE_MODE", "success")
launch_sentinel = os.environ.get("PRIVATE_SHADOW_LAUNCH_SENTINEL")
if launch_sentinel is not None:
    Path(launch_sentinel).write_text("launched", encoding="utf-8")
base = {
    "source_revision_hash": "a" * 64,
    "document_types": ["markdown", "pdf"],
    "page_count": 1,
    "slide_count": 1,
    "byte_usage": {"index_inputs": 64},
}
success_states = [
    "prior_operator_state_empty",
    "store_created",
    "inputs_uploaded:2",
    "inputs_imported:2",
    "positive_query_complete",
    "wrong_scope_query_complete",
    "documents_delete_attempted:2",
    "files_delete_attempted:2",
    "file_reconciliation_empty",
    "stores_delete_attempted:1",
    "store_reconciliation_empty",
]
if mode.startswith("success"):
    record = {
        "status": "passed",
        **base,
        "provider_operation_states": success_states,
        "citation_resolution_rate": 1.0,
        "duration_ms": 25,
        "token_usage": {"input": 3, "output": 2},
        "warnings": [],
    }
    exit_code = 0
    if mode == "success_missing_state":
        record["provider_operation_states"].remove("positive_query_complete")
    elif mode == "success_out_of_order":
        states = record["provider_operation_states"]
        states[2], states[3] = states[3], states[2]
    elif mode == "success_failure_marker":
        record["provider_operation_states"].insert(2, "private_shadow_failed")
else:
    stage = mode.removeprefix("stage_")
    cleanup = "complete"
    reconciliation = "empty"
    warnings = ["private_shadow_failed"]
    states = ["prior_operator_state_empty"]
    if stage != "prior_state_check":
        states.append("store_created")
    if stage not in {"prior_state_check", "create_store", "upload_input"}:
        states.append("inputs_uploaded:2")
    if stage not in {
        "prior_state_check", "create_store", "upload_input", "import_input",
        "wait_for_import",
    }:
        states.append("inputs_imported:2")
    if stage in {
        "negative_query", "negative_validation", "cleanup", "unknown",
    }:
        states.append("positive_query_complete")
    if stage in {"cleanup", "unknown"}:
        states.append("wrong_scope_query_complete")
    states.extend(
        [
            "documents_delete_attempted:2",
            "files_delete_attempted:2",
            "file_reconciliation_empty",
            "stores_delete_attempted:1",
            "store_reconciliation_empty",
            "private_shadow_failed",
        ]
    )
    if mode == "primary_over_cleanup":
        stage = "positive_query"
        cleanup = "failed"
        reconciliation = "unknown"
        warnings = ["private_citation_unresolved", "private_cleanup_failed"]
    elif mode == "cleanup_failure":
        stage = "cleanup"
        cleanup = "failed"
        reconciliation = "unknown"
        warnings = ["private_cleanup_failed"]
    elif mode == "reconciliation_residue":
        stage = "cleanup"
        cleanup = "failed"
        reconciliation = "not_empty"
        warnings = ["private_cleanup_failed"]
        states = [state for state in states if not state.endswith("reconciliation_empty")]
    elif mode == "reconciliation_unknown":
        stage = "cleanup"
        cleanup = "unknown"
        reconciliation = "unknown"
        warnings = ["private_cleanup_failed", "private_cleanup_unknown"]
        states = [state for state in states if not state.endswith("reconciliation_empty")]
    elif mode == "preflight_failure":
        stage = "prior_state_check"
        cleanup = "unknown"
        reconciliation = "unknown"
        warnings = ["private_shadow_failed", "private_cleanup_unknown"]
    elif mode == "reversed_warnings":
        stage = "positive_query"
        cleanup = "failed"
        reconciliation = "unknown"
        warnings = ["private_cleanup_failed", "private_citation_unresolved"]
    elif mode == "stage_progress_contradiction":
        stage = "create_store"
        states = [*success_states, "private_shadow_failed"]
    elif mode == "impossible_outcomes":
        stage = "positive_query"
        cleanup = "complete"
        reconciliation = "unknown"
        warnings = ["private_citation_unresolved"]
    record = {
        "status": "blocked",
        **base,
        "provider_operation_states": states,
        "failure_stage": stage,
        "provider_cleanup_outcome": cleanup,
        "provider_reconciliation_outcome": reconciliation,
        "warnings": warnings,
    }
    exit_code = 1
if mode == "malformed":
    print("{")
elif mode == "raw_content":
    print("PRIVATE SOURCE CONTENT MUST BE REJECTED")
    print(json.dumps(record, sort_keys=True, separators=(",", ":")))
elif mode == "extra_key":
    record["raw_body"] = "PRIVATE SOURCE CONTENT MUST BE REJECTED"
    print(json.dumps(record, sort_keys=True, separators=(",", ":")))
else:
    print(json.dumps(record, sort_keys=True, separators=(",", ":")))
raise SystemExit(exit_code)
'@,
  $Utf8
)

function Invoke-PrivateShadowCase {
  param(
    [Parameter(Mandatory = $true)][string]$Mode,
    [Parameter(Mandatory = $true)][bool]$EvidenceUsable,
    [Parameter(Mandatory = $true)][int]$ExpectedExit
  )
  $CaseRoot = Join-Path $CasesRoot ([Guid]::NewGuid().ToString("N"))
  $EvidenceRoot = Join-Path $CaseRoot "evidence"
  $DiagnosticRoot = Join-Path $CaseRoot "diagnostic"
  New-Item -ItemType Directory -Path $EvidenceRoot -Force | Out-Null
  $SafeResult = Join-Path $EvidenceRoot "result.json"
  $SafeStatus = Join-Path $EvidenceRoot "status.json"
  $env:PRIVATE_SHADOW_FIXTURE_MODE = $Mode
  $env:PRIVATE_SHADOW_FAKE_SECRET = "must-not-be-read-or-retained"
  try {
    & powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass `
      -File $WrapperScript -PythonExecutable $PythonExecutable `
      -OperatorScript $Emitter -DiagnosticRoot $DiagnosticRoot `
      -SafeResultPath $SafeResult -SafeStatusPath $SafeStatus
    $ActualExit = $LASTEXITCODE
  } finally {
    Remove-Item Env:PRIVATE_SHADOW_FIXTURE_MODE -ErrorAction SilentlyContinue
    Remove-Item Env:PRIVATE_SHADOW_FAKE_SECRET -ErrorAction SilentlyContinue
  }
  if ($ActualExit -ne $ExpectedExit) {
    throw "Private-shadow wrapper exit was invalid for $Mode`: $ActualExit."
  }
  $Status = Get-Content -LiteralPath $SafeStatus -Raw | ConvertFrom-Json
  if ($Status.evidence_usable -ne $EvidenceUsable -or
      -not $Status.operator_artifacts_deleted -or
      $Status.raw_content_retained -or
      (Test-Path -LiteralPath $DiagnosticRoot)) {
    throw "Private-shadow wrapper status was invalid for $Mode."
  }
  if ($EvidenceUsable -ne (Test-Path -LiteralPath $SafeResult -PathType Leaf)) {
    throw "Private-shadow safe-result presence was invalid for $Mode."
  }
  $StatusText = Get-Content -LiteralPath $SafeStatus -Raw
  if ($StatusText -match "must-not-be-read-or-retained|PRIVATE SOURCE CONTENT") {
    throw "Private-shadow status retained forbidden content."
  }
  if (-not $EvidenceUsable) {
    return ""
  }
  $ResultText = Get-Content -LiteralPath $SafeResult -Raw
  if ($ResultText -match "must-not-be-read-or-retained|PRIVATE SOURCE CONTENT") {
    throw "Private-shadow result retained forbidden content."
  }
  return $ResultText
}

function Assert-PrivateShadowBoundaryRejected {
  param([Parameter(Mandatory = $true)][string]$DiagnosticRoot)
  $CaseRoot = Join-Path $CasesRoot ([Guid]::NewGuid().ToString("N"))
  $EvidenceRoot = Join-Path $CaseRoot "evidence"
  New-Item -ItemType Directory -Path $EvidenceRoot -Force | Out-Null
  $SafeResult = Join-Path $EvidenceRoot "result.json"
  $SafeStatus = Join-Path $EvidenceRoot "status.json"
  $LaunchSentinel = Join-Path $CaseRoot "operator-launched"
  $env:PRIVATE_SHADOW_FIXTURE_MODE = "success"
  $env:PRIVATE_SHADOW_LAUNCH_SENTINEL = $LaunchSentinel
  try {
    & powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass `
      -File $WrapperScript -PythonExecutable $PythonExecutable `
      -OperatorScript $Emitter -DiagnosticRoot $DiagnosticRoot `
      -SafeResultPath $SafeResult -SafeStatusPath $SafeStatus
    $ActualExit = $LASTEXITCODE
  } finally {
    Remove-Item Env:PRIVATE_SHADOW_FIXTURE_MODE -ErrorAction SilentlyContinue
    Remove-Item Env:PRIVATE_SHADOW_LAUNCH_SENTINEL -ErrorAction SilentlyContinue
  }
  $Status = Get-Content -LiteralPath $SafeStatus -Raw | ConvertFrom-Json
  if ($ActualExit -ne 54 -or $Status.evidence_usable -or
      (Test-Path -LiteralPath $SafeResult) -or
      (Test-Path -LiteralPath $LaunchSentinel) -or
      (Test-Path -LiteralPath $DiagnosticRoot)) {
    throw "Private-shadow unsafe diagnostic boundary was not rejected."
  }
}

try {
  . $EvidenceScript
  $Success = Invoke-PrivateShadowCase -Mode "success" `
    -EvidenceUsable $true -ExpectedExit 0
  $SuccessRecord = $Success | ConvertFrom-Json
  if ($SuccessRecord.status -cne "passed" -or
      $SuccessRecord.provider_cleanup_outcome) {
    throw "Private-shadow success evidence was invalid."
  }

  foreach ($Stage in @(
      "create_store", "upload_input", "import_input", "wait_for_import",
      "positive_query", "positive_validation", "negative_query",
      "negative_validation"
  )) {
    $Record = Invoke-PrivateShadowCase -Mode "stage_$Stage" `
      -EvidenceUsable $true -ExpectedExit 1 | ConvertFrom-Json
    if ($Record.failure_stage -cne $Stage -or
        $Record.provider_cleanup_outcome -cne "complete" -or
        $Record.provider_reconciliation_outcome -cne "empty") {
      throw "Private-shadow stage evidence was invalid for $Stage."
    }
  }

  $Preflight = Invoke-PrivateShadowCase -Mode "preflight_failure" `
    -EvidenceUsable $true -ExpectedExit 1 | ConvertFrom-Json
  if ($Preflight.failure_stage -cne "prior_state_check" -or
      $Preflight.provider_cleanup_outcome -cne "unknown") {
    throw "Private-shadow preflight failure evidence was invalid."
  }

  $PrimaryFirst = Invoke-PrivateShadowCase -Mode "primary_over_cleanup" `
    -EvidenceUsable $true -ExpectedExit 1
  $PrimarySecond = Invoke-PrivateShadowCase -Mode "primary_over_cleanup" `
    -EvidenceUsable $true -ExpectedExit 1
  if ($PrimaryFirst -cne $PrimarySecond) {
    throw "Private-shadow sanitized output was not deterministic."
  }
  $Primary = $PrimaryFirst | ConvertFrom-Json
  if (($Primary.warnings -join "`n") -cne
      ("private_citation_unresolved", "private_cleanup_failed" -join "`n") -or
      $Primary.failure_stage -cne "positive_query") {
    throw "Private-shadow primary failure precedence was not preserved."
  }

  $Cleanup = Invoke-PrivateShadowCase -Mode "cleanup_failure" `
    -EvidenceUsable $true -ExpectedExit 1 | ConvertFrom-Json
  $Residue = Invoke-PrivateShadowCase -Mode "reconciliation_residue" `
    -EvidenceUsable $true -ExpectedExit 1 | ConvertFrom-Json
  $Unknown = Invoke-PrivateShadowCase -Mode "reconciliation_unknown" `
    -EvidenceUsable $true -ExpectedExit 1 | ConvertFrom-Json
  if ($Cleanup.provider_cleanup_outcome -cne "failed" -or
      $Cleanup.provider_reconciliation_outcome -cne "unknown" -or
      $Residue.provider_reconciliation_outcome -cne "not_empty" -or
      $Unknown.provider_cleanup_outcome -cne "unknown" -or
      $Unknown.provider_reconciliation_outcome -cne "unknown") {
    throw "Private-shadow cleanup/reconciliation outcomes were invalid."
  }

  Invoke-PrivateShadowCase -Mode "malformed" `
    -EvidenceUsable $false -ExpectedExit 51 | Out-Null
  Invoke-PrivateShadowCase -Mode "extra_key" `
    -EvidenceUsable $false -ExpectedExit 52 | Out-Null
  Invoke-PrivateShadowCase -Mode "raw_content" `
    -EvidenceUsable $false -ExpectedExit 51 | Out-Null

  foreach ($Mode in @(
      "success_missing_state", "success_out_of_order", "success_failure_marker",
      "reversed_warnings", "stage_progress_contradiction", "impossible_outcomes"
  )) {
    Invoke-PrivateShadowCase -Mode $Mode `
      -EvidenceUsable $false -ExpectedExit 52 | Out-Null
  }

  Assert-PrivateShadowBoundaryRejected `
    -DiagnosticRoot (Join-Path $OperatorRoot "diagnostic")
  $JunctionTarget = Join-Path $Sandbox "junction-target"
  $JunctionParent = Join-Path $JunctionTarget "existing"
  $Junction = Join-Path $Sandbox "junction"
  New-Item -ItemType Directory -Path $JunctionParent | Out-Null
  New-Item -ItemType Junction -Path $Junction -Target $JunctionTarget | Out-Null
  Assert-PrivateShadowBoundaryRejected `
    -DiagnosticRoot (Join-Path $Junction "existing/diagnostic")
  Write-Output "PRIVATE_SHADOW_EVIDENCE_HARNESS_VERIFIED"
} finally {
  if (Test-Path -LiteralPath $Sandbox) {
    Remove-Item -LiteralPath $Sandbox -Recurse -Force
  }
}
