[CmdletBinding()]
param(
  [Parameter(Mandatory = $true)][string]$EvidenceScript,
  [Parameter(Mandatory = $true)][string]$WrapperScript,
  [Parameter(Mandatory = $true)][string]$PythonExecutable,
  [Parameter(Mandatory = $true)][string]$SourceRoot,
  [Parameter(Mandatory = $true)][string]$EntryPoint,
  [Parameter(Mandatory = $true)][string]$EntryFixture
)

$ErrorActionPreference = "Stop"
$Sandbox = Join-Path ([IO.Path]::GetTempPath()) (
  "oms-private-shadow-evidence-{0}" -f [Guid]::NewGuid().ToString("N")
)
$Utf8 = [Text.UTF8Encoding]::new($false, $true)
New-Item -ItemType Directory -Path $Sandbox | Out-Null
$ProjectRoot = Join-Path $Sandbox "project"
$OperatorRoot = Join-Path $ProjectRoot "scripts"
$PackageRoot = Join-Path $ProjectRoot "src"
$EvidenceModule = Join-Path $PackageRoot "oms_hub/providers/gemini/evidence.py"
$CasesRoot = Join-Path $Sandbox "cases"
New-Item -ItemType Directory -Path $OperatorRoot,$CasesRoot | Out-Null
Copy-Item -LiteralPath $SourceRoot -Destination $PackageRoot -Recurse
$Emitter = Join-Path $OperatorRoot "emit_private_shadow_json.py"

[IO.File]::WriteAllText(
  $Emitter,
  @'
import json
import os

if os.environ["PRIVATE_SHADOW_FIXTURE_MODE"] == "valid":
    record = {
        "status": "blocked",
        "source_revision_hash": "a" * 64,
        "document_types": ["markdown"],
        "page_count": 1,
        "slide_count": 1,
        "provider_operation_states": ["private_shadow_failed"],
        "byte_usage": {"index_inputs": 1},
        "transient_attempts": 0,
        "failure_class": "unclassified",
        "failure_stage": "prior_state_check",
        "failure_input_identity": "none",
        "provider_error_category": "provider",
        "provider_status_code": 400,
        "provider_reason": "provider_bad_request",
        "provider_cleanup_outcome": "unknown",
        "provider_reconciliation_outcome": "unknown",
        "warnings": ["private_shadow_failed", "private_cleanup_unknown"],
    }
else:
    record = {"status": "blocked"}
print(json.dumps(record, sort_keys=True, separators=(",", ":")))
raise SystemExit(1)
'@,
  $Utf8
)

function Invoke-DirectEvidence {
  param([Parameter(Mandatory = $true)][string]$Raw)
  $Start = [Diagnostics.ProcessStartInfo]::new()
  $Start.FileName = $PythonExecutable
  $Start.Arguments = "-m oms_hub.providers.gemini.evidence --process-exit-code 1"
  $Start.WorkingDirectory = $ProjectRoot
  $Start.UseShellExecute = $false
  $Start.CreateNoWindow = $true
  $Start.RedirectStandardInput = $true
  $Start.RedirectStandardOutput = $true
  $Start.RedirectStandardError = $true
  $Start.StandardInputEncoding = $Utf8
  $Start.StandardOutputEncoding = $Utf8
  $Start.StandardErrorEncoding = $Utf8
  $Start.EnvironmentVariables["PYTHONPATH"] = $PackageRoot
  $Process = [Diagnostics.Process]::new()
  $Process.StartInfo = $Start
  if (-not $Process.Start()) { throw "Evidence validator did not start." }
  $Process.StandardInput.Write($Raw)
  $Process.StandardInput.Close()
  $Stdout = $Process.StandardOutput.ReadToEndAsync()
  $Stderr = $Process.StandardError.ReadToEndAsync()
  $Process.WaitForExit()
  [pscustomobject]@{
    ExitCode = $Process.ExitCode
    Stdout = $Stdout.GetAwaiter().GetResult()
    Stderr = $Stderr.GetAwaiter().GetResult()
  }
}

function Invoke-WrapperEvidence {
  param([Parameter(Mandatory = $true)][string]$Mode)
  $CaseRoot = Join-Path $CasesRoot ([Guid]::NewGuid().ToString("N"))
  $EvidenceRoot = Join-Path $CaseRoot "evidence"
  $DiagnosticRoot = Join-Path $CaseRoot "diagnostic"
  New-Item -ItemType Directory -Path $EvidenceRoot | Out-Null
  $SafeResult = Join-Path $EvidenceRoot "result.json"
  $SafeStatus = Join-Path $EvidenceRoot "status.json"
  $env:PRIVATE_SHADOW_FIXTURE_MODE = $Mode
  try {
    & powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass `
      -File $WrapperScript -PythonExecutable $PythonExecutable -ProjectRoot $ProjectRoot `
      -OperatorScript $Emitter -DiagnosticRoot $DiagnosticRoot `
      -SafeResultPath $SafeResult -SafeStatusPath $SafeStatus
    $ExitCode = $LASTEXITCODE
  } finally {
    Remove-Item Env:PRIVATE_SHADOW_FIXTURE_MODE -ErrorAction SilentlyContinue
  }
  [pscustomobject]@{
    ExitCode = $ExitCode
    SafeResult = if (Test-Path -LiteralPath $SafeResult) {
      [IO.File]::ReadAllText($SafeResult, $Utf8)
    } else { "" }
  }
}

function Invoke-EntrypointEvidence {
  $Start = [Diagnostics.ProcessStartInfo]::new()
  $Start.FileName = $PythonExecutable
  $Start.Arguments = '"' + $EntryFixture.Replace('"', '\"') +
    '" --entrypoint "' + $EntryPoint.Replace('"', '\"') + '" --mode corrected'
  $Start.UseShellExecute = $false
  $Start.CreateNoWindow = $true
  $Start.RedirectStandardOutput = $true
  $Start.RedirectStandardError = $true
  $Start.StandardOutputEncoding = $Utf8
  $Start.StandardErrorEncoding = $Utf8
  $Process = [Diagnostics.Process]::new()
  $Process.StartInfo = $Start
  if (-not $Process.Start()) { throw "Entrypoint fixture did not start." }
  $Stdout = $Process.StandardOutput.ReadToEndAsync()
  $Stderr = $Process.StandardError.ReadToEndAsync()
  $Process.WaitForExit()
  [pscustomobject]@{
    ExitCode = $Process.ExitCode
    Stdout = $Stdout.GetAwaiter().GetResult()
    Stderr = $Stderr.GetAwaiter().GetResult()
  }
}

function Invoke-WrapperReparseCase {
  $CaseRoot = Join-Path $CasesRoot ([Guid]::NewGuid().ToString("N"))
  $EvidenceRoot = Join-Path $CaseRoot "evidence"
  $ExternalRoot = Join-Path $CaseRoot "external"
  $ReparseRoot = Join-Path $CaseRoot "reparse"
  New-Item -ItemType Directory -Path $EvidenceRoot,$ExternalRoot | Out-Null
  New-Item -ItemType Junction -Path $ReparseRoot -Target $ExternalRoot | Out-Null
  $SafeResult = Join-Path $EvidenceRoot "result.json"
  $SafeStatus = Join-Path $EvidenceRoot "status.json"
  & powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass `
    -File $WrapperScript -PythonExecutable $PythonExecutable -ProjectRoot $ProjectRoot `
    -OperatorScript $Emitter -DiagnosticRoot (Join-Path $ReparseRoot "diagnostic") `
    -SafeResultPath $SafeResult -SafeStatusPath $SafeStatus
  [pscustomobject]@{
    ExitCode = $LASTEXITCODE
    SafeResultExists = Test-Path -LiteralPath $SafeResult
  }
}

try {
  . $EvidenceScript
  $ValidRaw = '{"status":"blocked","source_revision_hash":"' + ("a" * 64) + '","document_types":["markdown"],"page_count":1,"slide_count":1,"provider_operation_states":["private_shadow_failed"],"byte_usage":{"index_inputs":1},"transient_attempts":0,"failure_class":"unclassified","failure_stage":"prior_state_check","failure_input_identity":"none","provider_error_category":"provider","provider_status_code":400,"provider_reason":"provider_bad_request","provider_cleanup_outcome":"unknown","provider_reconciliation_outcome":"unknown","warnings":["private_shadow_failed","private_cleanup_unknown"]}' + "`n"
  $DirectValid = Invoke-DirectEvidence -Raw $ValidRaw
  $WrappedValid = Invoke-WrapperEvidence -Mode "valid"
  if ($DirectValid.ExitCode -ne 0 -or -not [string]::IsNullOrEmpty($DirectValid.Stderr) -or
      $WrappedValid.ExitCode -ne 1 -or $WrappedValid.SafeResult -cne $DirectValid.Stdout) {
    throw "Valid private-shadow evidence did not match the shared Python contract."
  }

  $Entrypoint = Invoke-EntrypointEvidence
  $EntrypointRaw = $Entrypoint.Stdout.TrimEnd("`r", "`n")
  $EntrypointRoot = Join-Path $CasesRoot ([Guid]::NewGuid().ToString("N"))
  New-Item -ItemType Directory -Path $EntrypointRoot | Out-Null
  $EntrypointRawPath = Join-Path $EntrypointRoot "entrypoint.stdout"
  $EntrypointSafeResult = Join-Path $EntrypointRoot "entrypoint.result.json"
  [IO.File]::WriteAllText($EntrypointRawPath, $EntrypointRaw, $Utf8)
  $EntrypointEvidence = Convert-PrivateShadowEvidence `
    -RawStdoutPath $EntrypointRawPath -SafeResultPath $EntrypointSafeResult `
    -ProcessExitCode $Entrypoint.ExitCode -PythonExecutable $PythonExecutable `
    -SourceRoot $PackageRoot -EvidenceModule $EvidenceModule
  if ($Entrypoint.ExitCode -ne 1 -or -not [string]::IsNullOrEmpty($Entrypoint.Stderr) -or
      $EntrypointEvidence.ExitCode -ne 0 -or -not (Test-Path -LiteralPath $EntrypointSafeResult)) {
    throw "Real entrypoint evidence did not pass through the real converter."
  }
  Write-Output "PRIVATE_SHADOW_ENTRYPOINT_CONVERTER_VERIFIED"

  $InvalidRaw = '{"status":"blocked"}' + "`n"
  $DirectInvalid = Invoke-DirectEvidence -Raw $InvalidRaw
  $WrappedInvalid = Invoke-WrapperEvidence -Mode "invalid"
  if ($DirectInvalid.ExitCode -ne 52 -or -not [string]::IsNullOrEmpty($DirectInvalid.Stdout) -or
      -not [string]::IsNullOrEmpty($DirectInvalid.Stderr) -or $WrappedInvalid.ExitCode -ne 52 -or
      -not [string]::IsNullOrEmpty($WrappedInvalid.SafeResult)) {
    throw "Invalid private-shadow evidence did not fail closed through both paths."
  }

  $WriteRoot = Join-Path $CasesRoot ([Guid]::NewGuid().ToString("N"))
  New-Item -ItemType Directory -Path $WriteRoot | Out-Null
  $WriteRawPath = Join-Path $WriteRoot "raw.json"
  $WriteTarget = Join-Path $WriteRoot "already-a-directory"
  [IO.File]::WriteAllText($WriteRawPath, $ValidRaw, $Utf8)
  New-Item -ItemType Directory -Path $WriteTarget | Out-Null
  $WriteFailure = Convert-PrivateShadowEvidence `
    -RawStdoutPath $WriteRawPath -SafeResultPath $WriteTarget -ProcessExitCode 1 `
    -PythonExecutable $PythonExecutable -SourceRoot $PackageRoot `
    -EvidenceModule $EvidenceModule
  if ($WriteFailure.ExitCode -ne 53 -or $WriteFailure.Stage -cne "safe_result_write" -or
      $WriteFailure.EvidenceUsable) {
    throw "Safe evidence write failure did not remain distinct."
  }

  $ReparseFailure = Invoke-WrapperReparseCase
  if ($ReparseFailure.ExitCode -ne 54 -or $ReparseFailure.SafeResultExists) {
    throw "Reparse-point diagnostic containment did not fail closed."
  }
  Write-Output "PRIVATE_SHADOW_EVIDENCE_HARNESS_VERIFIED"
} finally {
  if (Test-Path -LiteralPath $Sandbox) {
    Remove-Item -LiteralPath $Sandbox -Recurse -Force
  }
}
