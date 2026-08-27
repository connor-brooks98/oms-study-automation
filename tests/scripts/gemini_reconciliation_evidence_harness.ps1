[CmdletBinding()]
param(
  [Parameter(Mandatory = $true)]
  [string]$EvidenceScript,

  [Parameter(Mandatory = $true)]
  [string]$PythonExecutable
)

$ErrorActionPreference = "Stop"
. $EvidenceScript

$Sandbox = Join-Path ([System.IO.Path]::GetTempPath()) (
  "oms-gemini-reconciliation-evidence-{0}" -f [Guid]::NewGuid().ToString("N")
)
New-Item -ItemType Directory -Path $Sandbox | Out-Null
$Utf8 = [System.Text.UTF8Encoding]::new($false, $true)
$Emitter = Join-Path $Sandbox "emit_reconciliation_json.py"
[System.IO.File]::WriteAllText(
  $Emitter,
  @'
import json
import os
from pathlib import Path

record = {
    "schema_version": 1,
    "status": "passed",
    "provider_operation_states": [
        "inventory_complete",
        "deletes_attempted",
        "reconciliation_empty",
    ],
    "inspected_counts": {"stores": 1, "files": 2, "documents": 1},
    "matched_counts": {"stores": 1, "files": 1, "documents": 1},
    "delete_attempt_counts": {"stores": 1, "files": 1, "documents": 1},
    "remaining_counts": {
        "stores": 0,
        "files": 0,
        "documents": 0,
        "stores_inspected": 0,
        "files_inspected": 1,
        "documents_inspected": 0,
    },
    "provider_cleanup_complete": True,
    "warnings": [],
}
Path(os.environ["OMS_EMITTER_OUTPUT"]).write_text(
    os.environ.get("OMS_EMITTER_PREFIX", "")
    + json.dumps(record, sort_keys=True, separators=(",", ":"))
    + "\n",
    encoding="utf-8",
)
'@,
  $Utf8
)

function Write-PythonJson([string]$Path, [string]$Prefix = "") {
  $env:OMS_EMITTER_OUTPUT = $Path
  $env:OMS_EMITTER_PREFIX = $Prefix
  try {
    & $PythonExecutable $Emitter
    if ($LASTEXITCODE -ne 0) {
      throw "Python JSON emitter failed with exit code $LASTEXITCODE."
    }
  } finally {
    Remove-Item Env:OMS_EMITTER_OUTPUT -ErrorAction SilentlyContinue
    Remove-Item Env:OMS_EMITTER_PREFIX -ErrorAction SilentlyContinue
  }
}

try {
  $Raw = Join-Path $Sandbox "operator.stdout"
  $Safe = Join-Path $Sandbox "safe.json"
  $Stage = Join-Path $Sandbox "stage.json"
  Write-PythonJson -Path $Raw
  $Result = Convert-GeminiReconciliationEvidence `
    -RawStdoutPath $Raw -SafeResultPath $Safe -StageMarkerPath $Stage
  if ($Result.ExitCode -ne 0 -or -not $Result.EvidenceUsable -or $Result.RetainRaw) {
    throw "Valid Python JSON was not accepted."
  }

  $PrefixRaw = Join-Path $Sandbox "prefix.stdout"
  $PrefixSafe = Join-Path $Sandbox "prefix-safe.json"
  $PrefixStage = Join-Path $Sandbox "prefix-stage.json"
  Write-PythonJson -Path $PrefixRaw -Prefix "sdk-prefix-line`n"
  $PrefixResult = Convert-GeminiReconciliationEvidence `
    -RawStdoutPath $PrefixRaw -SafeResultPath $PrefixSafe -StageMarkerPath $PrefixStage
  $PrefixRecord = Get-Content -LiteralPath $PrefixSafe -Raw | ConvertFrom-Json
  if ($PrefixResult.ExitCode -ne 0 -or -not $PrefixResult.RetainRaw -or
      $PrefixRecord.wrapper_warnings -notcontains "unexpected_stdout_prefix") {
    throw "Unexpected stdout prefix was not retained and reported."
  }

  $Malformed = Join-Path $Sandbox "malformed.stdout"
  [System.IO.File]::WriteAllText($Malformed, "{`n", $Utf8)
  $ParseResult = Convert-GeminiReconciliationEvidence `
    -RawStdoutPath $Malformed -SafeResultPath (Join-Path $Sandbox "malformed-safe.json") `
    -StageMarkerPath (Join-Path $Sandbox "malformed-stage.json")
  if ($ParseResult.ExitCode -ne 41 -or $ParseResult.Stage -cne "parse" -or
      -not $ParseResult.RetainRaw) {
    throw "Malformed JSON did not produce the parse contract."
  }

  $Invalid = Join-Path $Sandbox "invalid.stdout"
  [System.IO.File]::WriteAllText($Invalid, '{"schema_version":1,"status":"passed"}' + "`n", $Utf8)
  $ValidationResult = Convert-GeminiReconciliationEvidence `
    -RawStdoutPath $Invalid -SafeResultPath (Join-Path $Sandbox "invalid-safe.json") `
    -StageMarkerPath (Join-Path $Sandbox "invalid-stage.json")
  if ($ValidationResult.ExitCode -ne 42 -or $ValidationResult.Stage -cne "validation" -or
      -not $ValidationResult.RetainRaw) {
    throw "Invalid JSON shape did not produce the validation contract."
  }

  $WriteRaw = Join-Path $Sandbox "write.stdout"
  $WriteStage = Join-Path $Sandbox "write-stage.json"
  $DirectoryAsDestination = Join-Path $Sandbox "directory-destination"
  New-Item -ItemType Directory -Path $DirectoryAsDestination | Out-Null
  Write-PythonJson -Path $WriteRaw
  $WriteResult = Convert-GeminiReconciliationEvidence `
    -RawStdoutPath $WriteRaw -SafeResultPath $DirectoryAsDestination `
    -StageMarkerPath $WriteStage
  $WriteMarker = Get-Content -LiteralPath $WriteStage -Raw | ConvertFrom-Json
  if ($WriteResult.ExitCode -ne 43 -or $WriteResult.Stage -cne "safe_result_write" -or
      -not $WriteResult.RetainRaw -or $WriteMarker.stage -cne "safe_result_write") {
    throw "Safe-result write failure did not retain its stage and raw input."
  }

  Write-Output "RECONCILIATION_EVIDENCE_HARNESS_VERIFIED"
} finally {
  Remove-Item -LiteralPath $Sandbox -Recurse -Force -ErrorAction SilentlyContinue
}
