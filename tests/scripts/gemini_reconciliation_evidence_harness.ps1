[CmdletBinding()]
param(
  [Parameter(Mandatory = $true)]
  [string]$EvidenceScript,

  [Parameter(Mandatory = $true)]
  [string]$WrapperScript,

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
    "schema_version": 2,
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
    "inventory_failure_stage": "not_applicable",
    "provider_error_category": "none",
    "warnings": [],
}
mode = os.environ.get("OMS_EMITTER_MODE", "passed")
if mode == "blocked":
    record.update(
        status="blocked",
        provider_operation_states=["inventory_failed"],
        provider_cleanup_complete=False,
        inventory_failure_stage="store_request",
        provider_error_category="transient",
        warnings=["provider_reconciliation_incomplete"],
    )
elif mode == "not_authorized":
    record.update(
        status="blocked",
        provider_operation_states=["reconciliation_failed"],
        inspected_counts={"stores": 0, "files": 0, "documents": 0},
        matched_counts={"stores": 0, "files": 0, "documents": 0},
        delete_attempt_counts={"stores": 0, "files": 0, "documents": 0},
        remaining_counts={
            "stores": 0,
            "files": 0,
            "documents": 0,
            "stores_inspected": 0,
            "files_inspected": 0,
            "documents_inspected": 0,
        },
        provider_cleanup_complete=False,
        inventory_failure_stage="not_applicable",
        provider_error_category="none",
        warnings=["provider_reconciliation_not_authorized"],
    )
payload = (
    os.environ.get("OMS_EMITTER_PREFIX", "")
    + json.dumps(record, sort_keys=True, separators=(",", ":"))
    + "\n"
)
output = os.environ.get("OMS_EMITTER_OUTPUT")
if output is None:
    print(payload, end="")
else:
    Path(output).write_text(payload, encoding="utf-8")
raise SystemExit(0 if record["status"] == "passed" else 1)
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

$HealthProcess = $null
try {
  $Project = Join-Path $Sandbox "project"
  $Scripts = Join-Path $Project "scripts"
  New-Item -ItemType Directory -Path $Scripts | Out-Null
  Copy-Item -LiteralPath $EvidenceScript `
    -Destination (Join-Path $Scripts "gemini-reconciliation-evidence.ps1")
  Copy-Item -LiteralPath $Emitter `
    -Destination (Join-Path $Scripts "run-gemini-reconciliation.py")
  $HealthServer = Join-Path $Sandbox "health_server.py"
  $PortFile = Join-Path $Sandbox "health-port.txt"
  [System.IO.File]::WriteAllText(
    $HealthServer,
    @'
import http.server
import json
import sys
from pathlib import Path


class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        body = json.dumps({"status": "ok"}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        return


server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
Path(sys.argv[1]).write_text(str(server.server_port), encoding="ascii")
for _ in range(16):
    server.handle_request()
server.server_close()
'@,
    $Utf8
  )
  $HealthProcess = Start-Process -FilePath $PythonExecutable `
    -ArgumentList @($HealthServer, $PortFile) -WindowStyle Hidden -PassThru
  for ($Attempt = 0; $Attempt -lt 100 -and -not (Test-Path $PortFile); $Attempt++) {
    Start-Sleep -Milliseconds 50
  }
  if (-not (Test-Path -LiteralPath $PortFile)) {
    throw "Offline health fixture did not start."
  }
  $Port = [System.IO.File]::ReadAllText($PortFile, $Utf8)
  $PowerShellExecutable = (Get-Process -Id $PID).Path

  $JunctionTarget = Join-Path $Sandbox "junction-target"
  $JunctionParent = Join-Path $JunctionTarget "existing"
  $Junction = Join-Path $Sandbox "junction"
  New-Item -ItemType Directory -Path $JunctionParent | Out-Null
  New-Item -ItemType Junction -Path $Junction -Target $JunctionTarget | Out-Null
  & $PowerShellExecutable -NoProfile -NonInteractive -ExecutionPolicy Bypass `
    -File $WrapperScript -PythonExecutable $PythonExecutable -ProjectRoot $Project `
    -DiagnosticRoot (Join-Path $Junction "existing/diagnostic") `
    -SafeResultPath (Join-Path $Sandbox "junction-safe.json") `
    -SafeStatusPath (Join-Path $Sandbox "junction-status.json") `
    -HubHealthUri "http://127.0.0.1:$Port/health"
  if ($LASTEXITCODE -eq 0) {
    throw "Diagnostic reparse ancestor was not rejected before launch."
  }

  $ProjectJunction = Join-Path $Sandbox "project-junction"
  New-Item -ItemType Junction -Path $ProjectJunction -Target $Project | Out-Null
  & $PowerShellExecutable -NoProfile -NonInteractive -ExecutionPolicy Bypass `
    -File $WrapperScript -PythonExecutable $PythonExecutable -ProjectRoot $ProjectJunction `
    -DiagnosticRoot (Join-Path $Sandbox "project-junction-diagnostic") `
    -SafeResultPath (Join-Path $Sandbox "project-junction-safe.json") `
    -SafeStatusPath (Join-Path $Sandbox "project-junction-status.json") `
    -HubHealthUri "http://127.0.0.1:$Port/health"
  if ($LASTEXITCODE -eq 0) {
    throw "Project-root reparse ancestor was not rejected before launch."
  }

  $WrapperSafe = Join-Path $Sandbox "wrapper-safe.json"
  $WrapperStatus = Join-Path $Sandbox "wrapper-status.json"
  $WrapperDiagnostic = Join-Path $Sandbox "wrapper-diagnostic"
  & $PowerShellExecutable -NoProfile -NonInteractive -ExecutionPolicy Bypass `
    -File $WrapperScript -PythonExecutable $PythonExecutable -ProjectRoot $Project `
    -DiagnosticRoot $WrapperDiagnostic -SafeResultPath $WrapperSafe `
    -SafeStatusPath $WrapperStatus -HubHealthUri "http://127.0.0.1:$Port/health"
  if ($LASTEXITCODE -ne 0) {
    $SafeStatus = if (Test-Path -LiteralPath $WrapperStatus) {
      [IO.File]::ReadAllText($WrapperStatus, $Utf8)
    } else {
      "missing"
    }
    throw "Committed wrapper rejected valid Python JSON; safe_status=$SafeStatus"
  }
  $WrapperStatusRecord = Get-Content -LiteralPath $WrapperStatus -Raw | ConvertFrom-Json
  if (-not $WrapperStatusRecord.evidence_usable -or
      -not $WrapperStatusRecord.provider_cleanup_complete -or
      -not $WrapperStatusRecord.operator_artifacts_deleted -or
      $WrapperStatusRecord.raw_diagnostic_retained) {
    throw "Committed wrapper did not separate evidence and cleanup state."
  }

  $BlockedWrapperSafe = Join-Path $Sandbox "blocked-wrapper-safe.json"
  $BlockedWrapperStatus = Join-Path $Sandbox "blocked-wrapper-status.json"
  $BlockedWrapperDiagnostic = Join-Path $Sandbox "blocked-wrapper-diagnostic"
  $env:OMS_EMITTER_MODE = "blocked"
  try {
    & $PowerShellExecutable -NoProfile -NonInteractive -ExecutionPolicy Bypass `
      -File $WrapperScript -PythonExecutable $PythonExecutable -ProjectRoot $Project `
      -DiagnosticRoot $BlockedWrapperDiagnostic -SafeResultPath $BlockedWrapperSafe `
      -SafeStatusPath $BlockedWrapperStatus -HubHealthUri "http://127.0.0.1:$Port/health"
  } finally {
    Remove-Item Env:OMS_EMITTER_MODE -ErrorAction SilentlyContinue
  }
  if ($LASTEXITCODE -ne 1) {
    throw "Blocked operator record did not preserve its failing exit."
  }
  $BlockedWrapperRecord = Get-Content -LiteralPath $BlockedWrapperStatus -Raw |
    ConvertFrom-Json
  if (-not $BlockedWrapperRecord.evidence_usable -or
      $BlockedWrapperRecord.provider_cleanup_complete -or
      $BlockedWrapperRecord.operator_artifacts_deleted -or
      -not $BlockedWrapperRecord.raw_diagnostic_retained -or
      $BlockedWrapperRecord.hub_health_before -cne "ok" -or
      $BlockedWrapperRecord.hub_health_after -cne "ok" -or
      -not (Test-Path -LiteralPath $BlockedWrapperDiagnostic -PathType Container)) {
    throw "Blocked usable evidence did not retain protected diagnostics."
  }

  $UnauthorizedWrapperSafe = Join-Path $Sandbox "unauthorized-wrapper-safe.json"
  $UnauthorizedWrapperStatus = Join-Path $Sandbox "unauthorized-wrapper-status.json"
  $UnauthorizedWrapperDiagnostic = Join-Path $Sandbox "unauthorized-wrapper-diagnostic"
  $env:OMS_EMITTER_MODE = "not_authorized"
  try {
    & $PowerShellExecutable -NoProfile -NonInteractive -ExecutionPolicy Bypass `
      -File $WrapperScript -PythonExecutable $PythonExecutable -ProjectRoot $Project `
      -DiagnosticRoot $UnauthorizedWrapperDiagnostic `
      -SafeResultPath $UnauthorizedWrapperSafe `
      -SafeStatusPath $UnauthorizedWrapperStatus -HubHealthUri "http://127.0.0.1:$Port/health"
  } finally {
    Remove-Item Env:OMS_EMITTER_MODE -ErrorAction SilentlyContinue
  }
  if ($LASTEXITCODE -ne 1) {
    throw "Not-authorized operator record did not preserve its failing exit."
  }
  $UnauthorizedWrapperRecord = Get-Content -LiteralPath $UnauthorizedWrapperStatus -Raw |
    ConvertFrom-Json
  if (-not $UnauthorizedWrapperRecord.evidence_usable -or
      -not $UnauthorizedWrapperRecord.raw_diagnostic_retained -or
      $UnauthorizedWrapperRecord.operator_artifacts_deleted) {
    throw "Not-authorized evidence was rejected or discarded."
  }

  $PrefixWrapperSafe = Join-Path $Sandbox "prefix-wrapper-safe.json"
  $PrefixWrapperStatus = Join-Path $Sandbox "prefix-wrapper-status.json"
  $PrefixWrapperDiagnostic = Join-Path $Sandbox "prefix-wrapper-diagnostic"
  $env:OMS_EMITTER_PREFIX = "sdk-prefix-line`n"
  try {
    & $PowerShellExecutable -NoProfile -NonInteractive -ExecutionPolicy Bypass `
      -File $WrapperScript -PythonExecutable $PythonExecutable -ProjectRoot $Project `
      -DiagnosticRoot $PrefixWrapperDiagnostic -SafeResultPath $PrefixWrapperSafe `
      -SafeStatusPath $PrefixWrapperStatus -HubHealthUri "http://127.0.0.1:$Port/health"
  } finally {
    Remove-Item Env:OMS_EMITTER_PREFIX -ErrorAction SilentlyContinue
  }
  if ($LASTEXITCODE -ne 0) {
    throw "Prefix diagnostic wrapper path did not complete."
  }
  $PrefixWrapperRecord = Get-Content -LiteralPath $PrefixWrapperStatus -Raw |
    ConvertFrom-Json
  $PrefixAcl = Get-Acl -LiteralPath $PrefixWrapperDiagnostic
  $PrefixRules = @($PrefixAcl.GetAccessRules(
      $true, $false, [System.Security.Principal.SecurityIdentifier]
  ))
  $RequiredInheritance =
    [System.Security.AccessControl.InheritanceFlags]::ContainerInherit -bor
    [System.Security.AccessControl.InheritanceFlags]::ObjectInherit
  if (-not $PrefixWrapperRecord.raw_diagnostic_retained -or
      $PrefixWrapperRecord.operator_artifacts_deleted -or
      -not $PrefixAcl.AreAccessRulesProtected -or $PrefixRules.Count -ne 1 -or
      (($PrefixRules[0].FileSystemRights -band
        [System.Security.AccessControl.FileSystemRights]::FullControl) -ne
        [System.Security.AccessControl.FileSystemRights]::FullControl) -or
      (($PrefixRules[0].InheritanceFlags -band $RequiredInheritance) -ne
        $RequiredInheritance)) {
    throw "Retained raw diagnostic DACL was not exact current-user FullControl."
  }

  $Raw = Join-Path $Sandbox "operator.stdout"
  $Safe = Join-Path $Sandbox "safe.json"
  $Stage = Join-Path $Sandbox "stage.json"
  Write-PythonJson -Path $Raw
  $Result = Convert-GeminiReconciliationEvidence `
    -RawStdoutPath $Raw -SafeResultPath $Safe -StageMarkerPath $Stage
  if ($Result.ExitCode -ne 0 -or -not $Result.EvidenceUsable -or $Result.RetainRaw) {
    throw "Valid Python JSON was not accepted."
  }

  foreach ($StoreStage in @("store_client", "store_request", "store_close")) {
    $StoreStagePath = Join-Path $Sandbox ("store-stage-{0}.stdout" -f $StoreStage)
    $StoreStageRecord = Get-Content -LiteralPath $Raw -Raw | ConvertFrom-Json
    $StoreStageRecord.status = "blocked"
    $StoreStageRecord.provider_operation_states = @("inventory_failed")
    $StoreStageRecord.provider_cleanup_complete = $false
    $StoreStageRecord.inventory_failure_stage = $StoreStage
    $StoreStageRecord.provider_error_category = "provider"
    $StoreStageRecord.warnings = @("provider_reconciliation_incomplete")
    $StoreStageRecord | ConvertTo-Json -Compress -Depth 5 |
      Set-Content -LiteralPath $StoreStagePath -Encoding UTF8
    $StoreStageResult = Convert-GeminiReconciliationEvidence `
      -RawStdoutPath $StoreStagePath `
      -SafeResultPath ($StoreStagePath + ".safe") `
      -StageMarkerPath ($StoreStagePath + ".stage")
    if ($StoreStageResult.ExitCode -ne 0 -or -not $StoreStageResult.EvidenceUsable) {
      throw "Fixed store lifecycle stage was rejected."
    }
  }

  foreach ($SafeProviderCategory in @("provider_bad_request", "provider_not_found")) {
    $SafeCategoryPath = Join-Path $Sandbox (
      "safe-provider-category-{0}.stdout" -f $SafeProviderCategory
    )
    $SafeCategoryRecord = Get-Content -LiteralPath $Raw -Raw | ConvertFrom-Json
    $SafeCategoryRecord.status = "blocked"
    $SafeCategoryRecord.provider_operation_states = @("inventory_failed")
    $SafeCategoryRecord.provider_cleanup_complete = $false
    $SafeCategoryRecord.inventory_failure_stage = "store_request"
    $SafeCategoryRecord.provider_error_category = $SafeProviderCategory
    $SafeCategoryRecord.warnings = @("provider_reconciliation_incomplete")
    $SafeCategoryRecord | ConvertTo-Json -Compress -Depth 5 |
      Set-Content -LiteralPath $SafeCategoryPath -Encoding UTF8
    $SafeCategoryResult = Convert-GeminiReconciliationEvidence `
      -RawStdoutPath $SafeCategoryPath `
      -SafeResultPath ($SafeCategoryPath + ".safe") `
      -StageMarkerPath ($SafeCategoryPath + ".stage")
    if ($SafeCategoryResult.ExitCode -ne 0 -or
        -not $SafeCategoryResult.EvidenceUsable) {
      throw "Safe provider request category was rejected."
    }
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

  $UnknownCode = Join-Path $Sandbox "unknown-code.stdout"
  $UnknownRecord = Get-Content -LiteralPath $Raw -Raw | ConvertFrom-Json
  $UnknownRecord.status = "blocked"
  $UnknownRecord.provider_operation_states = @("inventory_failed")
  $UnknownRecord.provider_cleanup_complete = $false
  $UnknownRecord.warnings = @("provider_payload_must_not_be_allowlisted")
  $UnknownRecord | ConvertTo-Json -Compress -Depth 5 |
    Set-Content -LiteralPath $UnknownCode -Encoding UTF8
  $UnknownResult = Convert-GeminiReconciliationEvidence `
    -RawStdoutPath $UnknownCode -SafeResultPath (Join-Path $Sandbox "unknown-safe.json") `
    -StageMarkerPath (Join-Path $Sandbox "unknown-stage.json")
  if ($UnknownResult.ExitCode -ne 42 -or -not $UnknownResult.RetainRaw) {
    throw "Unknown warning code was accepted into safe evidence."
  }

  foreach ($InvalidPair in @(
      @{Stage="unknown_stage";Category="provider"},
      @{Stage="store_list";Category="provider"},
      @{Stage="file_list";Category="unknown_category"},
      @{Stage="not_applicable";Category="provider_bad_request"}
  )) {
    $InvalidPairPath = Join-Path $Sandbox (
      "invalid-pair-{0}.stdout" -f [Guid]::NewGuid().ToString("N")
    )
    $InvalidPairRecord = Get-Content -LiteralPath $Raw -Raw | ConvertFrom-Json
    $InvalidPairRecord.status = "blocked"
    $InvalidPairRecord.provider_operation_states = @("inventory_failed")
    $InvalidPairRecord.provider_cleanup_complete = $false
    $InvalidPairRecord.inventory_failure_stage = $InvalidPair.Stage
    $InvalidPairRecord.provider_error_category = $InvalidPair.Category
    $InvalidPairRecord.warnings = @("provider_reconciliation_incomplete")
    $InvalidPairRecord | ConvertTo-Json -Compress -Depth 5 |
      Set-Content -LiteralPath $InvalidPairPath -Encoding UTF8
    $InvalidPairResult = Convert-GeminiReconciliationEvidence `
      -RawStdoutPath $InvalidPairPath `
      -SafeResultPath ($InvalidPairPath + ".safe") `
      -StageMarkerPath ($InvalidPairPath + ".stage")
    if ($InvalidPairResult.ExitCode -ne 42 -or -not $InvalidPairResult.RetainRaw) {
      throw "Unknown inventory failure diagnostic was accepted."
    }
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
  if ($null -ne $HealthProcess -and -not $HealthProcess.HasExited) {
    Stop-Process -Id $HealthProcess.Id -Force -ErrorAction SilentlyContinue
  }
  Remove-Item -LiteralPath $Sandbox -Recurse -Force -ErrorAction SilentlyContinue
}
