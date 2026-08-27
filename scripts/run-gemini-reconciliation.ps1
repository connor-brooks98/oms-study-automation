[CmdletBinding()]
param(
  [Parameter(Mandatory = $true)][string]$PythonExecutable,
  [Parameter(Mandatory = $true)][string]$ProjectRoot,
  [Parameter(Mandatory = $true)][string]$DiagnosticRoot,
  [Parameter(Mandatory = $true)][string]$SafeResultPath,
  [Parameter(Mandatory = $true)][string]$SafeStatusPath,
  [string]$HubHealthUri = "http://127.0.0.1:8888/health"
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
$Utf8 = [System.Text.UTF8Encoding]::new($false, $true)
$ProjectRoot = [System.IO.Path]::GetFullPath($ProjectRoot)
$DiagnosticRoot = [System.IO.Path]::GetFullPath($DiagnosticRoot)
$SafeResultPath = [System.IO.Path]::GetFullPath($SafeResultPath)
$SafeStatusPath = [System.IO.Path]::GetFullPath($SafeStatusPath)
$Operator = Join-Path $ProjectRoot "scripts/run-gemini-reconciliation.py"
$EvidenceModule = Join-Path $ProjectRoot "scripts/gemini-reconciliation-evidence.ps1"
$RawStdout = Join-Path $DiagnosticRoot "operator.stdout"
$RawStderr = Join-Path $DiagnosticRoot "operator.stderr"
$StageMarker = Join-Path $DiagnosticRoot "evidence-stage.json"
$EvidenceUsable = $false
$RetainRaw = $true
$OperatorArtifactsDeleted = $false
$ProviderCleanupComplete = $false
$WrapperStage = "bootstrap"
$ExitCode = 43
$HealthBefore = "unavailable"
$HealthAfter = "unavailable"

function Protect-ReconciliationDirectory {
  param([Parameter(Mandatory = $true)][string]$Path)
  $Identity = & whoami.exe /user /fo csv /nh | ConvertFrom-Csv -Header Name,Sid
  if ($LASTEXITCODE -ne 0 -or $Identity.Sid -notmatch '^S-1(?:-\d+)+$') {
    throw "Current Windows SID was unavailable."
  }
  & icacls.exe $Path /inheritance:r /grant:r "*$($Identity.Sid):(OI)(CI)F" | Out-Null
  if ($LASTEXITCODE -ne 0) {
    throw "Diagnostic DACL initialization failed."
  }
  & icacls.exe $Path /verify | Out-Null
  if ($LASTEXITCODE -ne 0) {
    throw "Diagnostic DACL verification failed."
  }
  $Acl = Get-Acl -LiteralPath $Path
  $Rules = @($Acl.GetAccessRules(
      $true, $false, [System.Security.Principal.SecurityIdentifier]
  ))
  if (-not $Acl.AreAccessRulesProtected -or $Rules.Count -ne 1 -or
      $Rules[0].IdentityReference.Value -cne $Identity.Sid -or
      $Rules[0].AccessControlType -ne
        [System.Security.AccessControl.AccessControlType]::Allow -or
      $Rules[0].IsInherited) {
    throw "Diagnostic DACL was not current-user-only."
  }
}

function Get-HubHealthState {
  try {
    $Health = Invoke-RestMethod -Uri $HubHealthUri -TimeoutSec 10 -Method Get
    if ($Health.status -ceq "ok") { return "ok" }
  } catch {}
  return "unavailable"
}

function Write-SafeStatus {
  param([Parameter(Mandatory = $true)][object]$Record)
  if (Test-Path -LiteralPath $SafeStatusPath) {
    throw "Safe status destination must not already exist."
  }
  $Parent = Split-Path -Parent $SafeStatusPath
  $Temporary = Join-Path $Parent (".{0}.tmp" -f [Guid]::NewGuid().ToString("N"))
  try {
    $Payload = $Record | ConvertTo-Json -Compress -Depth 5
    [System.IO.File]::WriteAllText($Temporary, $Payload + "`n", $Utf8)
    [System.IO.File]::Move($Temporary, $SafeStatusPath)
  } finally {
    Remove-Item -LiteralPath $Temporary -Force -ErrorAction SilentlyContinue
  }
}

try {
  foreach ($Required in @($PythonExecutable, $Operator, $EvidenceModule)) {
    if (-not (Test-Path -LiteralPath $Required -PathType Leaf)) {
      throw "Required reconciliation input was unavailable."
    }
  }
  if ($DiagnosticRoot.StartsWith(
      $ProjectRoot + [System.IO.Path]::DirectorySeparatorChar,
      [System.StringComparison]::OrdinalIgnoreCase
  )) {
    throw "Diagnostic root must be outside Git."
  }
  if (Test-Path -LiteralPath $DiagnosticRoot) {
    throw "Diagnostic root must be newly created."
  }
  $DiagnosticParent = Split-Path -Parent $DiagnosticRoot
  if ((Get-Item -LiteralPath $DiagnosticParent).Attributes -band
      [System.IO.FileAttributes]::ReparsePoint) {
    throw "Diagnostic parent must not be a reparse point."
  }
  New-Item -ItemType Directory -Path $DiagnosticRoot | Out-Null
  Protect-ReconciliationDirectory -Path $DiagnosticRoot
  . $EvidenceModule

  $HealthBefore = Get-HubHealthState
  if ($HealthBefore -cne "ok") {
    throw "Hub health precondition failed."
  }

  $WrapperStage = "operator"
  $ProcessInfo = [System.Diagnostics.ProcessStartInfo]::new()
  $ProcessInfo.FileName = $PythonExecutable
  $ProcessInfo.Arguments = "scripts/run-gemini-reconciliation.py"
  $ProcessInfo.WorkingDirectory = $ProjectRoot
  $ProcessInfo.UseShellExecute = $false
  $ProcessInfo.CreateNoWindow = $true
  $ProcessInfo.RedirectStandardOutput = $true
  $ProcessInfo.RedirectStandardError = $true
  $ProcessInfo.StandardOutputEncoding = $Utf8
  $ProcessInfo.StandardErrorEncoding = $Utf8
  $ProcessInfo.EnvironmentVariables["RUN_GEMINI_RECONCILIATION"] = "1"
  $Process = [System.Diagnostics.Process]::new()
  $Process.StartInfo = $ProcessInfo
  if (-not $Process.Start()) {
    throw "Reconciliation operator did not start."
  }
  $StdoutTask = $Process.StandardOutput.ReadToEndAsync()
  $StderrTask = $Process.StandardError.ReadToEndAsync()
  $Process.WaitForExit()
  $OperatorExit = $Process.ExitCode
  $Stdout = $StdoutTask.GetAwaiter().GetResult()
  $Stderr = $StderrTask.GetAwaiter().GetResult()
  [System.IO.File]::WriteAllText($RawStdout, $Stdout, $Utf8)
  [System.IO.File]::WriteAllText($RawStderr, $Stderr, $Utf8)

  $WrapperStage = "evidence"
  $Evidence = Convert-GeminiReconciliationEvidence `
    -RawStdoutPath $RawStdout -SafeResultPath $SafeResultPath `
    -StageMarkerPath $StageMarker
  $EvidenceUsable = $Evidence.EvidenceUsable
  $RetainRaw = $Evidence.RetainRaw
  $WrapperStage = $Evidence.Stage
  $ExitCode = if ($Evidence.ExitCode -ne 0) {$Evidence.ExitCode} else {$OperatorExit}
  if ($EvidenceUsable) {
    $SafeRecord = Get-Content -LiteralPath $SafeResultPath -Raw | ConvertFrom-Json
    $ProviderCleanupComplete =
      [bool]$SafeRecord.operator_result.provider_cleanup_complete
  }
} catch {
  $RetainRaw = $true
} finally {
  $HealthAfter = Get-HubHealthState
  if ($EvidenceUsable -and -not $RetainRaw) {
    Remove-Item -LiteralPath $RawStdout,$RawStderr,$StageMarker `
      -Force -ErrorAction SilentlyContinue
    if (@($RawStdout,$RawStderr,$StageMarker | Where-Object {
          Test-Path -LiteralPath $_
        }).Count -eq 0) {
      Remove-Item -LiteralPath $DiagnosticRoot -Force -ErrorAction SilentlyContinue
      $OperatorArtifactsDeleted = -not (Test-Path -LiteralPath $DiagnosticRoot)
    }
  }
  $SafeStatus = [ordered]@{
    schema_version = 1
    wrapper_stage = $WrapperStage
    exit_code = $ExitCode
    evidence_usable = $EvidenceUsable
    provider_cleanup_complete = $ProviderCleanupComplete
    operator_artifacts_deleted = $OperatorArtifactsDeleted
    raw_diagnostic_retained = $RetainRaw
    hub_health_before = $HealthBefore
    hub_health_after = $HealthAfter
  }
  Write-SafeStatus -Record $SafeStatus
}

exit $ExitCode
