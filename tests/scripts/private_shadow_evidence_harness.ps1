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
$ValidatorScratch = Join-Path $Sandbox "validator-scratch"
$HostTempRoot = Join-Path $Sandbox "host-temp"
$DirectConverterSiteCustomize = $null
$DirectConverterProbePath = $null
$HostTempProbePath = Join-Path $HostTempRoot "direct-converter-environment.json"
$HadCompositionVerify = Test-Path Env:OMS_TASK28_COMPOSITION_VERIFY
$PriorCompositionVerify = $env:OMS_TASK28_COMPOSITION_VERIFY
$HadPrivateProject = Test-Path Env:OMS_TASK28_PRIVATE_PROJECT
$PriorPrivateProject = $env:OMS_TASK28_PRIVATE_PROJECT
New-Item -ItemType Directory -Path $OperatorRoot,$CasesRoot,$ValidatorScratch,$HostTempRoot | Out-Null
Copy-Item -LiteralPath $SourceRoot -Destination $PackageRoot -Recurse
$Emitter = Join-Path $OperatorRoot "emit_private_shadow_json.py"
. (Join-Path (Split-Path -Parent $WrapperScript) "task28/private-shadow-common.ps1")

function New-EvidenceState {
  $State = Get-Task28StatePaths -RunId ([Guid]::NewGuid().ToString("N"))
  New-Task28ProtectedState -State $State
  return $State
}

[IO.File]::WriteAllText(
  $Emitter,
  @'
import json
import os
import tempfile
from pathlib import Path

scratch = Path(os.environ["TEMP"]).resolve()
if Path(os.environ["TMP"]).resolve() != scratch:
    raise RuntimeError("temp_mismatch")
if os.environ.get("PYTHONDONTWRITEBYTECODE") != "1":
    raise RuntimeError("bytecode_not_disabled")
with tempfile.NamedTemporaryFile(delete=True) as handle:
    if Path(handle.name).resolve().parent != scratch:
        raise RuntimeError("tempfile_escaped")
(scratch / "environment-probe.json").write_text(
    json.dumps({
        "temp": str(scratch),
        "tmp": os.environ["TMP"],
        "bytecode": os.environ.get("PYTHONDONTWRITEBYTECODE"),
        "composition_verify": os.environ.get("OMS_TASK28_COMPOSITION_VERIFY"),
        "private_project": os.environ.get("OMS_TASK28_PRIVATE_PROJECT"),
        "private_diagnostic": os.environ.get("OMS_TASK28_PRIVATE_DIAGNOSTIC_PATH"),
    }),
    encoding="utf-8",
)
project = Path(__file__).resolve().parents[1]
if any(project.rglob("__pycache__")):
    raise RuntimeError("immutable_bytecode_written")

if os.getenv("PRIVATE_SHADOW_FIXTURE_MODE", "valid") == "valid":
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
  param([Parameter(Mandatory = $true)][string]$Raw, [string]$TempRoot)
  $Start = [Diagnostics.ProcessStartInfo]::new()
  $Start.FileName = $PythonExecutable
  $Start.Arguments = "-m oms_hub.providers.gemini.evidence --process-exit-code 1"
  $Start.WorkingDirectory = $ProjectRoot
  $Start.UseShellExecute = $false
  $Start.CreateNoWindow = $true
  $Start.RedirectStandardInput = $true
  $Start.RedirectStandardOutput = $true
  $Start.RedirectStandardError = $true
  $Start.StandardOutputEncoding = $Utf8
  $Start.StandardErrorEncoding = $Utf8
  $Start.EnvironmentVariables["PYTHONPATH"] = $PackageRoot
  $Start.EnvironmentVariables["PYTHONDONTWRITEBYTECODE"] = "1"
  if ($TempRoot) {
    $Start.EnvironmentVariables["TEMP"] = $TempRoot
    $Start.EnvironmentVariables["TMP"] = $TempRoot
  }
  $Process = [Diagnostics.Process]::new()
  $Process.StartInfo = $Start
  if (-not $Process.Start()) { throw "Evidence validator did not start." }
  $InputBytes = $Utf8.GetBytes($Raw)
  $InputStream = $Process.StandardInput.BaseStream
  $InputStream.Write($InputBytes, 0, $InputBytes.Length)
  $InputStream.Flush()
  $InputStream.Close()
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
  param([Parameter(Mandatory = $true)][string]$Mode, [switch]$CompositionVerify)
  $CaseRoot = Join-Path $CasesRoot ([Guid]::NewGuid().ToString("N"))
  $State = New-EvidenceState
  $EvidenceRoot = $State.Evidence
  $DiagnosticRoot = $State.Diagnostic
  $DiagnosticPath = Join-Path $DiagnosticRoot "provider-diagnostic.json"
  $SafeResult = Join-Path $EvidenceRoot "result.json"
  $SafeStatus = Join-Path $EvidenceRoot "status.json"
  $SafeResultContent = ""
  $EnvironmentProbe = ""
  $env:PRIVATE_SHADOW_FIXTURE_MODE = $Mode
  try {
    if ($CompositionVerify) {
      & powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass `
        -File $WrapperScript -PythonExecutable $PythonExecutable -StateRoot $State.Root -ProjectRoot $ProjectRoot `
        -OperatorScript $Emitter -DiagnosticRoot $DiagnosticRoot `
        -SafeResultPath $SafeResult -SafeStatusPath $SafeStatus -CompositionVerify
    } else {
      & powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass `
        -File $WrapperScript -PythonExecutable $PythonExecutable -StateRoot $State.Root -ProjectRoot $ProjectRoot `
        -OperatorScript $Emitter -DiagnosticRoot $DiagnosticRoot -DiagnosticPath $DiagnosticPath `
        -SafeResultPath $SafeResult -SafeStatusPath $SafeStatus
    }
    $ExitCode = $LASTEXITCODE
    if (Test-Path -LiteralPath $SafeResult) {
      $SafeResultContent = [IO.File]::ReadAllText($SafeResult, $Utf8)
    }
    $ProbePath = Join-Path $State.Scratch "environment-probe.json"
    if (Test-Path -LiteralPath $ProbePath) {
      $EnvironmentProbe = [IO.File]::ReadAllText($ProbePath, $Utf8)
    }
  } finally {
    Remove-Item Env:PRIVATE_SHADOW_FIXTURE_MODE -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath $State.Root -Recurse -Force -ErrorAction SilentlyContinue
  }
  [pscustomobject]@{
    ExitCode = $ExitCode
    SafeResult = $SafeResultContent
    EnvironmentProbe = $EnvironmentProbe
    ScratchRoot = $State.Scratch
    DiagnosticPath = $DiagnosticPath
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
  $State = $null
  $ExternalRoot = Join-Path $CaseRoot "external"
  try {
    New-Item -ItemType Directory -Path $CaseRoot | Out-Null
    $State = New-EvidenceState
    New-Item -ItemType Directory -Path $ExternalRoot | Out-Null
    Remove-Item -LiteralPath $State.Diagnostic -Recurse -Force
    New-Item -ItemType Junction -Path $State.Diagnostic -Target $ExternalRoot | Out-Null
    $SafeResult = Join-Path $State.Evidence "result.json"
    $SafeStatus = Join-Path $State.Evidence "status.json"
    & powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass `
      -File $WrapperScript -PythonExecutable $PythonExecutable -StateRoot $State.Root -ProjectRoot $ProjectRoot `
      -OperatorScript $Emitter -DiagnosticRoot $State.Diagnostic `
      -DiagnosticPath (Join-Path $State.Diagnostic "provider-diagnostic.json") `
      -SafeResultPath $SafeResult -SafeStatusPath $SafeStatus
    [pscustomobject]@{
      ExitCode = $LASTEXITCODE
      SafeResultExists = Test-Path -LiteralPath $SafeResult
    }
  } finally {
    if ($State) { Remove-Item -LiteralPath $State.Root -Recurse -Force -ErrorAction SilentlyContinue }
    Remove-Item -LiteralPath $ExternalRoot -Recurse -Force -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath $CaseRoot -Recurse -Force -ErrorAction SilentlyContinue
  }
}

try {
  . $EvidenceScript
  $ValidRaw = '{"status":"blocked","source_revision_hash":"' + ("a" * 64 -join "") + '","document_types":["markdown"],"page_count":1,"slide_count":1,"provider_operation_states":["private_shadow_failed"],"byte_usage":{"index_inputs":1},"transient_attempts":0,"failure_class":"unclassified","failure_stage":"prior_state_check","failure_input_identity":"none","provider_error_category":"provider","provider_status_code":400,"provider_reason":"provider_bad_request","provider_cleanup_outcome":"unknown","provider_reconciliation_outcome":"unknown","warnings":["private_shadow_failed","private_cleanup_unknown"]}' + "`n"
  $DirectValid = Invoke-DirectEvidence -Raw $ValidRaw
  $env:OMS_TASK28_COMPOSITION_VERIFY = "stale-composition"
  $env:OMS_TASK28_PRIVATE_PROJECT = "stale-private-project"
  $env:OMS_TASK28_PRIVATE_DIAGNOSTIC_PATH = "stale-private-diagnostic"
  $WrappedValid = Invoke-WrapperEvidence -Mode "valid"
  $DirectCanonical = $DirectValid.Stdout.TrimEnd("`r", "`n") + "`n"
  if ($DirectValid.ExitCode -ne 0 -or -not [string]::IsNullOrEmpty($DirectValid.Stderr) -or
      $WrappedValid.ExitCode -ne 1 -or $WrappedValid.SafeResult -cne $DirectCanonical) {
    $SafeStatus = [ordered]@{
      direct_exit = $DirectValid.ExitCode
      direct_stderr_empty = [string]::IsNullOrEmpty($DirectValid.Stderr)
      wrapped_exit = $WrappedValid.ExitCode
      safe_equal = $WrappedValid.SafeResult -ceq $DirectCanonical
      direct_length = $DirectCanonical.Length
      safe_length = $WrappedValid.SafeResult.Length
    } | ConvertTo-Json -Compress
    throw "Valid private-shadow evidence did not match the shared Python contract; safe_status=$SafeStatus"
  }
  $WrappedComposition = Invoke-WrapperEvidence -Mode "valid" -CompositionVerify
  foreach ($ProbeResult in @($WrappedValid, $WrappedComposition)) {
    $Probe = $ProbeResult.EnvironmentProbe | ConvertFrom-Json
    $ExpectedScratch = [IO.Path]::GetFullPath($ProbeResult.ScratchRoot)
    if (-not [string]::Equals([IO.Path]::GetFullPath($Probe.temp), $ExpectedScratch, [StringComparison]::OrdinalIgnoreCase) -or
        -not [string]::Equals([IO.Path]::GetFullPath($Probe.tmp), $ExpectedScratch, [StringComparison]::OrdinalIgnoreCase) -or
        $Probe.bytecode -cne "1") {
      throw "Child environment did not bind TEMP/TMP and bytecode policy to protected scratch."
    }
  }
  $NormalProbe = $WrappedValid.EnvironmentProbe | ConvertFrom-Json
  if ($null -ne $NormalProbe.composition_verify -or $null -ne $NormalProbe.private_project -or
      -not [string]::Equals([IO.Path]::GetFullPath($NormalProbe.private_diagnostic),
        [IO.Path]::GetFullPath($WrappedValid.DiagnosticPath), [StringComparison]::OrdinalIgnoreCase)) {
    throw "Normal child inherited stale composition environment."
  }
  $CompositionProbe = $WrappedComposition.EnvironmentProbe | ConvertFrom-Json
  if ($CompositionProbe.composition_verify -cne "1" -or $null -ne $CompositionProbe.private_diagnostic -or -not [string]::Equals(
      [IO.Path]::GetFullPath($CompositionProbe.private_project), [IO.Path]::GetFullPath($ProjectRoot),
      [StringComparison]::OrdinalIgnoreCase
    )) {
    throw "Composition child did not receive exact composition environment."
  }
  Write-Output "PRIVATE_SHADOW_COMPOSITION_ENVIRONMENT_VERIFIED"
  if (@(Get-ChildItem -LiteralPath $ProjectRoot -Force -Recurse -Directory -Filter "__pycache__").Count -ne 0) {
    throw "Child execution wrote Python bytecode into immutable project content."
  }
  Write-Output "PRIVATE_SHADOW_ENVIRONMENT_VERIFIED"

  $DirectConverterSiteCustomize = Join-Path $PackageRoot "sitecustomize.py"
  [IO.File]::WriteAllText(
    $DirectConverterSiteCustomize,
    @'
import json
import os
import tempfile
from pathlib import Path

scratch = Path(os.environ["TEMP"]).resolve()
if Path(os.environ["TMP"]).resolve() != scratch:
    raise RuntimeError("temp_mismatch")
if os.environ.get("PYTHONDONTWRITEBYTECODE") != "1":
    raise RuntimeError("bytecode_not_disabled")
with tempfile.NamedTemporaryFile(delete=True) as handle:
    if Path(handle.name).resolve().parent != scratch:
        raise RuntimeError("tempfile_escaped")
(scratch / "direct-converter-environment.json").write_text(
    json.dumps({"temp": str(scratch), "tmp": os.environ["TMP"], "bytecode": os.environ.get("PYTHONDONTWRITEBYTECODE")}),
    encoding="utf-8",
)
'@,
    $Utf8
  )
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
    -SourceRoot $PackageRoot -EvidenceModule $EvidenceModule -ScratchRoot $ValidatorScratch
  if ($Entrypoint.ExitCode -ne 1 -or -not [string]::IsNullOrEmpty($Entrypoint.Stderr) -or
      $EntrypointEvidence.ExitCode -ne 0 -or -not (Test-Path -LiteralPath $EntrypointSafeResult)) {
    throw "Real entrypoint evidence did not pass through the real converter."
  }
  $DirectConverterProbePath = Join-Path $ValidatorScratch "direct-converter-environment.json"
  if (-not (Test-Path -LiteralPath $DirectConverterProbePath)) {
    throw "Direct converter did not use the protected validator scratch root."
  }
  $DirectConverterProbe = [IO.File]::ReadAllText($DirectConverterProbePath, $Utf8) | ConvertFrom-Json
  $ExpectedValidatorScratch = [IO.Path]::GetFullPath($ValidatorScratch)
  if (-not [string]::Equals(
      [IO.Path]::GetFullPath($DirectConverterProbe.temp), $ExpectedValidatorScratch,
      [StringComparison]::OrdinalIgnoreCase
    ) -or -not [string]::Equals(
      [IO.Path]::GetFullPath($DirectConverterProbe.tmp), $ExpectedValidatorScratch,
      [StringComparison]::OrdinalIgnoreCase
    ) -or $DirectConverterProbe.bytecode -cne "1") {
    throw "Direct converter child environment escaped protected validator scratch."
  }
  if (@(Get-ChildItem -LiteralPath $ProjectRoot -Force -Recurse -Directory -Filter "__pycache__").Count -ne 0) {
    throw "Direct converter wrote Python bytecode into immutable project content."
  }
  Remove-Item -LiteralPath $DirectConverterSiteCustomize -Force
  if (Test-Path -LiteralPath $DirectConverterSiteCustomize) {
    throw "Direct converter sitecustomize remained after its dedicated call."
  }
  Write-Output "DIRECT_CONVERTER_SITECUSTOMIZE_REMOVED"
  Write-Output "PRIVATE_SHADOW_ENTRYPOINT_CONVERTER_VERIFIED"
  Write-Output "DIRECT_CONVERTER_ENVIRONMENT_VERIFIED"

  $InvalidRaw = '{"status":"blocked"}' + "`n"
  $DirectInvalid = Invoke-DirectEvidence -Raw $InvalidRaw -TempRoot $HostTempRoot
  $WrappedInvalid = Invoke-WrapperEvidence -Mode "invalid"
  if ($DirectInvalid.ExitCode -ne 52 -or -not [string]::IsNullOrEmpty($DirectInvalid.Stdout) -or
      -not [string]::IsNullOrEmpty($DirectInvalid.Stderr) -or $WrappedInvalid.ExitCode -ne 52 -or
      -not [string]::IsNullOrEmpty($WrappedInvalid.SafeResult)) {
    throw "Invalid private-shadow evidence did not fail closed through both paths."
  }
  if (Test-Path -LiteralPath $HostTempProbePath) {
    throw "Direct converter probe escaped the protected validator scratch root."
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
    -EvidenceModule $EvidenceModule -ScratchRoot $ValidatorScratch
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
  if ($DirectConverterSiteCustomize) {
    Remove-Item -LiteralPath $DirectConverterSiteCustomize -Force -ErrorAction SilentlyContinue
  }
  if ($DirectConverterProbePath) {
    Remove-Item -LiteralPath $DirectConverterProbePath -Force -ErrorAction SilentlyContinue
  }
  Remove-Item -LiteralPath $HostTempProbePath -Force -ErrorAction SilentlyContinue
  if ($HadCompositionVerify) {
    $env:OMS_TASK28_COMPOSITION_VERIFY = $PriorCompositionVerify
  } else {
    Remove-Item Env:OMS_TASK28_COMPOSITION_VERIFY -ErrorAction SilentlyContinue
  }
  if ($HadPrivateProject) {
    $env:OMS_TASK28_PRIVATE_PROJECT = $PriorPrivateProject
  } else {
    Remove-Item Env:OMS_TASK28_PRIVATE_PROJECT -ErrorAction SilentlyContinue
  }
  if (Test-Path -LiteralPath $Sandbox) {
    Remove-Item -LiteralPath $Sandbox -Recurse -Force
  }
}
