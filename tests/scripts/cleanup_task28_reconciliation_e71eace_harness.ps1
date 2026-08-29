[CmdletBinding()]
param([Parameter(Mandatory = $true)][string]$CleanupScript)

$ErrorActionPreference = 'Stop'
if (-not (Test-Path -LiteralPath $CleanupScript -PathType Leaf)) {
  throw 'cleanup_disposition_missing'
}

$sandbox = Join-Path ([IO.Path]::GetTempPath()) (
  'oms-task28-reconciliation-cleanup-{0}' -f [Guid]::NewGuid().ToString('N')
)
$oldLocalAppData = $env:LOCALAPPDATA
$oldTemp = $env:TEMP
$env:LOCALAPPDATA = Join-Path $sandbox 'LocalAppData'
$env:TEMP = Join-Path $sandbox 'Temp'
$root = Join-Path $env:LOCALAPPDATA 'Temp\sol0-task28-reconciliation-e71eace'
$evidence = Join-Path $root 'evidence'
$privateRoot = Join-Path $env:LOCALAPPDATA 'Temp\sol0-task28-private-shadow-9097851'
$retainedRoot = Join-Path $env:LOCALAPPDATA 'Temp\sol0-task28-private-shadow-06848e2'
$privateEvidence = Join-Path $privateRoot 'evidence'
$retainedEvidence = Join-Path $retainedRoot 'evidence'
$taskName = 'OMS Sol0 Task28 Reconciliation e71eace'
$taskPath = '\'
$expectedExecutable = Join-Path $PSHOME 'powershell.exe'
$expectedArguments = '-NoProfile -NonInteractive -ExecutionPolicy Bypass -File "' +
  (Join-Path $root 'reconciliation-launcher.ps1') + '"'
$resultBase64 = 'eyJzY2hlbWFfdmVyc2lvbiI6MSwiZXZpZGVuY2VfdXNhYmxlIjp0cnVlLCJ3cmFwcGVyX3N0YWdlIjoiY29tcGxldGUiLCJzdGRvdXRfcHJlZml4X2xpbmVfY291bnQiOjAsIndyYXBwZXJfd2FybmluZ3MiOnt9LCJvcGVyYXRvcl9yZXN1bHQiOnsiZGVsZXRlX2F0dGVtcHRfY291bnRzIjp7ImRvY3VtZW50cyI6MCwiZmlsZXMiOjAsInN0b3JlcyI6MH0sImluc3BlY3RlZF9jb3VudHMiOnsiZG9jdW1lbnRzIjowLCJmaWxlcyI6MCwic3RvcmVzIjowfSwiaW52ZW50b3J5X2ZhaWx1cmVfc3RhZ2UiOiJub3RfYXBwbGljYWJsZSIsIm1hdGNoZWRfY291bnRzIjp7ImRvY3VtZW50cyI6MCwiZmlsZXMiOjAsInN0b3JlcyI6MH0sInByb3ZpZGVyX2NsZWFudXBfY29tcGxldGUiOnRydWUsInByb3ZpZGVyX2Vycm9yX2NhdGVnb3J5Ijoibm9uZSIsInByb3ZpZGVyX29wZXJhdGlvbl9zdGF0ZXMiOlsiaW52ZW50b3J5X2NvbXBsZXRlIiwiZGVsZXRlc19hdHRlbXB0ZWQiLCJyZWNvbmNpbGlhdGlvbl9lbXB0eSJdLCJyZW1haW5pbmdfY291bnRzIjp7ImRvY3VtZW50cyI6MCwiZG9jdW1lbnRzX2luc3BlY3RlZCI6MCwiZmlsZXMiOjAsImZpbGVzX2luc3BlY3RlZCI6MCwic3RvcmVzIjowLCJzdG9yZXNfaW5zcGVjdGVkIjowfSwic2NoZW1hX3ZlcnNpb24iOjIsInN0YXR1cyI6InBhc3NlZCIsIndhcm5pbmdzIjpbXX19Cg=='
$statusBase64 = 'eyJzY2hlbWFfdmVyc2lvbiI6MSwid3JhcHBlcl9zdGFnZSI6ImNvbXBsZXRlIiwiZXhpdF9jb2RlIjowLCJldmlkZW5jZV91c2FibGUiOnRydWUsInByb3ZpZGVyX2NsZWFudXBfY29tcGxldGUiOnRydWUsIm9wZXJhdG9yX2FydGlmYWN0c19kZWxldGVkIjp0cnVlLCJyYXdfZGlhZ25vc3RpY19yZXRhaW5lZCI6ZmFsc2UsImh1Yl9oZWFsdGhfYmVmb3JlIjoib2siLCJodWJfaGVhbHRoX2FmdGVyIjoib2sifQo='
$global:CleanupTaskRegistered = $true
$global:CleanupTaskState = 'Ready'
$global:CleanupHealthSchema = 29
$global:CleanupUnregisterCalls = @()

function Protect-FixtureDirectory([string]$Path) {
  $sid = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value
  $grant = '*' + $sid + ':(OI)(CI)F'
  & icacls.exe $Path /inheritance:r /grant:r $grant | Out-Null
  if ($LASTEXITCODE -ne 0) { throw 'fixture_acl_failed' }
}

function Initialize-CleanupFixture {
  if (Test-Path -LiteralPath $env:LOCALAPPDATA) {
    Remove-Item -LiteralPath $env:LOCALAPPDATA -Recurse -Force
  }
  New-Item -ItemType Directory -Path $env:LOCALAPPDATA,$env:TEMP | Out-Null
  New-Item -ItemType Directory -Path $root,$evidence,$privateRoot,$privateEvidence,
    $retainedRoot,$retainedEvidence | Out-Null
  foreach ($directory in @($root,$evidence,$privateRoot,$privateEvidence,$retainedRoot,$retainedEvidence)) {
    Protect-FixtureDirectory $directory
  }
  [IO.File]::WriteAllBytes(
    (Join-Path $evidence 'safe-result.json'),
    [Convert]::FromBase64String($resultBase64)
  )
  [IO.File]::WriteAllBytes(
    (Join-Path $evidence 'safe-status.json'),
    [Convert]::FromBase64String($statusBase64)
  )
  [IO.File]::WriteAllText((Join-Path $retainedEvidence 'retained.json'),'{}')
  $global:CleanupTaskRegistered = $true
  $global:CleanupTaskState = 'Ready'
  $global:CleanupHealthSchema = 29
  $global:CleanupUnregisterCalls = @()
}

function New-CleanupTaskFixture {
  $sid = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value
  [pscustomobject]@{
    TaskName = $script:taskName
    TaskPath = $script:taskPath
    State = $global:CleanupTaskState
    Actions = @([pscustomobject]@{
      Execute = $script:expectedExecutable
      Arguments = $script:expectedArguments
      WorkingDirectory = $script:root
    })
    Principal = [pscustomobject]@{
      UserId = $sid
      RunLevel = 'Limited'
      LogonType = 'Interactive'
    }
    Settings = [pscustomobject]@{
      Enabled = $true
      ExecutionTimeLimit = 'PT0S'
    }
  }
}

function Get-ScheduledTask {
  [CmdletBinding()]
  param([string]$TaskName)
  $hub = [pscustomobject]@{TaskName='OMS Study Hub V2';TaskPath='\';State='Running'}
  if ($PSBoundParameters.ContainsKey('TaskName')) {
    if ($TaskName -ceq 'OMS Study Hub V2') { return $hub }
    return @()
  }
  $items = @($hub)
  if ($global:CleanupTaskRegistered) { $items += New-CleanupTaskFixture }
  return $items
}

function Get-ScheduledTaskInfo {
  [CmdletBinding()]
  param([string]$TaskName,[string]$TaskPath)
  if ($TaskName -cne $script:taskName -or $TaskPath -cne $script:taskPath) {
    throw 'unexpected_task_info_target'
  }
  [pscustomobject]@{
    LastTaskResult = 0
    LastRunTime = [DateTime]::Parse('2026-08-28T23:52:37Z').ToLocalTime()
  }
}

function Unregister-ScheduledTask {
  [CmdletBinding(SupportsShouldProcess = $true)]
  param([string]$TaskName,[string]$TaskPath,[switch]$Confirm)
  if ($TaskName -cne $script:taskName -or $TaskPath -cne $script:taskPath) {
    throw 'unexpected_unregister_target'
  }
  $global:CleanupUnregisterCalls += "$TaskPath$TaskName"
  $global:CleanupTaskRegistered = $false
}

function Invoke-RestMethod {
  [CmdletBinding()]
  param([string]$Uri,[int]$TimeoutSec,[string]$Method)
  if ($Uri -notin @('http://127.0.0.1:8765/health','http://127.0.0.1:8788/health')) {
    throw 'unexpected_health_uri'
  }
  [pscustomobject]@{status='ok';schema_version=$global:CleanupHealthSchema}
}

function Assert-FailureRetained([scriptblock]$Action) {
  $failed = $false
  try { & $Action } catch { $failed = $true }
  if (-not $failed -or -not $global:CleanupTaskRegistered -or
      -not (Test-Path -LiteralPath $root -PathType Container) -or
      $global:CleanupUnregisterCalls.Count -ne 0) {
    throw 'ambiguous_cleanup_did_not_retain_task_and_root'
  }
}

try {
  Initialize-CleanupFixture
  $validation = & $CleanupScript -ValidateOnly
  if ($validation -notcontains 'TASK28_RECONCILIATION_CLEANUP_VALIDATED' -or
      -not $global:CleanupTaskRegistered -or
      -not (Test-Path -LiteralPath $root -PathType Container) -or
      $global:CleanupUnregisterCalls.Count -ne 0) {
    throw 'validate_only_mutated_state'
  }

  Initialize-CleanupFixture
  [IO.File]::AppendAllText((Join-Path $evidence 'safe-result.json'),'x')
  Assert-FailureRetained { & $CleanupScript -ValidateOnly }

  Initialize-CleanupFixture
  $global:CleanupTaskState = 'Running'
  Assert-FailureRetained { & $CleanupScript -ValidateOnly }

  Initialize-CleanupFixture
  $global:CleanupHealthSchema = 28
  Assert-FailureRetained { & $CleanupScript -ValidateOnly }

  Initialize-CleanupFixture
  [IO.File]::WriteAllText((Join-Path $privateEvidence 'unexpected.json'),'{}')
  Assert-FailureRetained { & $CleanupScript -ValidateOnly }

  Initialize-CleanupFixture
  & $CleanupScript
  if ($global:CleanupTaskRegistered -or
      (Test-Path -LiteralPath $root) -or
      $global:CleanupUnregisterCalls.Count -ne 1 -or
      -not (Test-Path -LiteralPath $privateRoot -PathType Container) -or
      -not (Test-Path -LiteralPath $retainedRoot -PathType Container)) {
    throw 'exact_cleanup_success_contract_failed'
  }
  Write-Output 'TASK28_RECONCILIATION_CLEANUP_HARNESS_VERIFIED'
} finally {
  $env:LOCALAPPDATA = $oldLocalAppData
  $env:TEMP = $oldTemp
  Remove-Item -LiteralPath $sandbox -Recurse -Force -ErrorAction SilentlyContinue
}
