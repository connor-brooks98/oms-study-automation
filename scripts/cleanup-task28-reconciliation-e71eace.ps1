[CmdletBinding()]
param([switch]$ValidateOnly)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$root = Join-Path $env:LOCALAPPDATA 'Temp\sol0-task28-reconciliation-e71eace'
$evidence = Join-Path $root 'evidence'
$resultPath = Join-Path $evidence 'safe-result.json'
$statusPath = Join-Path $evidence 'safe-status.json'
$diagnostic = Join-Path $env:TEMP 'sol0-task28-reconciliation-diagnostic-e71eace'
$taskName = 'OMS Sol0 Task28 Reconciliation e71eace'
$taskPath = '\'
$launcher = Join-Path $root 'reconciliation-launcher.ps1'
$privateRoot = Join-Path $env:LOCALAPPDATA 'Temp\sol0-task28-private-shadow-9097851'
$retainedRoot = Join-Path $env:LOCALAPPDATA 'Temp\sol0-task28-private-shadow-06848e2'
$privateEvidence = Join-Path $privateRoot 'evidence'
$retainedEvidence = Join-Path $retainedRoot 'evidence'
$expectedExecutable = Join-Path $PSHOME 'powershell.exe'
$expectedArguments = '-NoProfile -NonInteractive -ExecutionPolicy Bypass -File "' +
  $launcher + '"'

function Assert-ExactHash([string]$Path,[string]$Expected) {
  if (-not (Test-Path -LiteralPath $Path -PathType Leaf) -or
      (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToLowerInvariant() -cne $Expected) {
    throw 'cleanup_exact_hash_mismatch'
  }
}

function Get-CurrentSid {
  $identity = & whoami.exe /user /fo csv /nh | ConvertFrom-Csv -Header Name,Sid
  if ($LASTEXITCODE -ne 0 -or $identity.Sid -notmatch '^S-1(?:-\d+)+$') {
    throw 'cleanup_identity_unavailable'
  }
  return [string]$identity.Sid
}

function Resolve-PrincipalSid([string]$UserId) {
  try {
    if ($UserId -match '^S-1(?:-\d+)+$') {
      return ([Security.Principal.SecurityIdentifier]::new($UserId)).Value
    }
    return ([Security.Principal.NTAccount]::new($UserId)).Translate(
      [Security.Principal.SecurityIdentifier]
    ).Value
  } catch {
    throw 'cleanup_task_contract_mismatch'
  }
}

function Assert-ProtectedDirectory([string]$Path) {
  if (-not (Test-Path -LiteralPath $Path -PathType Container)) {
    throw 'cleanup_directory_unavailable'
  }
  $sid = Get-CurrentSid
  $acl = Get-Acl -LiteralPath $Path
  $rules = @($acl.GetAccessRules($true,$false,[Security.Principal.SecurityIdentifier]))
  $rule = if ($rules.Count -eq 1) {$rules[0]} else {$null}
  $inherit = [Security.AccessControl.InheritanceFlags]::ContainerInherit -bor
    [Security.AccessControl.InheritanceFlags]::ObjectInherit
  if (-not $acl.AreAccessRulesProtected -or $null -eq $rule -or
      $rule.IdentityReference.Value -cne $sid -or
      $rule.AccessControlType -ne [Security.AccessControl.AccessControlType]::Allow -or
      $rule.IsInherited -or ($rule.InheritanceFlags -band $inherit) -ne $inherit -or
      $rule.PropagationFlags -ne [Security.AccessControl.PropagationFlags]::None -or
      (($rule.FileSystemRights -band [Security.AccessControl.FileSystemRights]::FullControl) -ne
        [Security.AccessControl.FileSystemRights]::FullControl)) {
    throw 'cleanup_acl_mismatch'
  }
}

function Assert-NoReparsePoint([string]$Path) {
  if (@(Get-ChildItem -LiteralPath $Path -Force -Recurse | Where-Object {
        ($_.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0
      }).Count -ne 0) {
    throw 'cleanup_reparse_point_detected'
  }
}

function Assert-PreservedPrivateRoots {
  foreach ($directory in @($privateRoot,$privateEvidence,$retainedRoot,$retainedEvidence)) {
    Assert-ProtectedDirectory $directory
  }
  Assert-NoReparsePoint $privateRoot
  Assert-NoReparsePoint $retainedRoot
  if (@(Get-ChildItem -LiteralPath $privateEvidence -Force).Count -ne 0 -or
      @(Get-ChildItem -LiteralPath $retainedEvidence -Force -File).Count -ne 1) {
    throw 'cleanup_private_root_state_mismatch'
  }
}

function Assert-HubHealthy {
  $hub = Get-ScheduledTask -TaskName 'OMS Study Hub V2' -ErrorAction Stop
  if ([string]$hub.State -cne 'Running') { throw 'cleanup_hub_task_not_running' }
  foreach ($port in @(8765,8788)) {
    $health = Invoke-RestMethod `
      -Uri ('http://127.0.0.1:{0}/health' -f $port) -TimeoutSec 5 -Method Get
    if ($health.status -cne 'ok' -or $health.schema_version -ne 29) {
      throw 'cleanup_hub_health_failed'
    }
  }
}

function Get-ExactTask {
  return @(Get-ScheduledTask -ErrorAction Stop | Where-Object {
    [string]$_.TaskName -ceq $taskName
  })
}

function Assert-ExactRetainedTask {
  $tasks = @(Get-ExactTask)
  if ($tasks.Count -ne 1) { throw 'cleanup_task_contract_mismatch' }
  $task = $tasks[0]
  $actions = @($task.Actions)
  $principal = $task.Principal
  $info = Get-ScheduledTaskInfo -TaskName $taskName -TaskPath $taskPath -ErrorAction Stop
  if ([string]$task.TaskName -cne $taskName -or
      [string]$task.TaskPath -cne $taskPath -or
      $actions.Count -ne 1 -or
      -not [string]::Equals(
        [string]$actions[0].Execute,$expectedExecutable,[StringComparison]::OrdinalIgnoreCase
      ) -or
      -not [string]::Equals(
        [string]$actions[0].Arguments,$expectedArguments,[StringComparison]::Ordinal
      ) -or
      -not [string]::Equals(
        [string]$actions[0].WorkingDirectory,$root,[StringComparison]::OrdinalIgnoreCase
      ) -or
      (Resolve-PrincipalSid ([string]$principal.UserId)) -cne (Get-CurrentSid) -or
      [string]$principal.RunLevel -cne 'Limited' -or
      [string]$principal.LogonType -cne 'Interactive' -or
      $task.Settings.Enabled -ne $true -or
      [string]$task.Settings.ExecutionTimeLimit -cne 'PT0S' -or
      [string]$task.State -cne 'Ready' -or
      [int]$info.LastTaskResult -ne 0 -or
      $info.LastRunTime.ToUniversalTime().ToString('o') -cne
        '2026-08-28T23:52:37.0000000Z') {
    throw 'cleanup_task_contract_mismatch'
  }
}

function Assert-ExactLegacyEvidence {
  $names = @(Get-ChildItem -LiteralPath $evidence -Force -File |
    ForEach-Object {$_.Name} | Sort-Object)
  if (($names -join "`n") -cne "safe-result.json`nsafe-status.json") {
    throw 'cleanup_evidence_set_mismatch'
  }
  Assert-ExactHash $resultPath 'bd26cc4b7b3eeb34e875caaa043b4118bea375bc4473a3dab34dd65dffdf5a7d'
  Assert-ExactHash $statusPath 'e5ec9fcf1fb790b62a18939d78d350ea1572a903e02fae6e4e916ad5f4661c3f'
  $safe = Get-Content -LiteralPath $resultPath -Raw | ConvertFrom-Json
  $status = Get-Content -LiteralPath $statusPath -Raw | ConvertFrom-Json
  $warnings = $safe.wrapper_warnings
  $operator = $safe.operator_result
  if ($warnings.GetType().FullName -cne 'System.Management.Automation.PSCustomObject' -or
      @($warnings.PSObject.Properties).Count -ne 0 -or
      $safe.schema_version -ne 1 -or -not $safe.evidence_usable -or
      $safe.wrapper_stage -cne 'complete' -or $safe.stdout_prefix_line_count -ne 0 -or
      $status.schema_version -ne 1 -or $status.wrapper_stage -cne 'complete' -or
      $status.exit_code -ne 0 -or -not $status.evidence_usable -or
      -not $status.provider_cleanup_complete -or -not $status.operator_artifacts_deleted -or
      $status.raw_diagnostic_retained -or $status.hub_health_before -cne 'ok' -or
      $status.hub_health_after -cne 'ok' -or
      $operator.schema_version -ne 2 -or $operator.status -cne 'passed' -or
      -not $operator.provider_cleanup_complete -or @($operator.warnings).Count -ne 0 -or
      (@($operator.provider_operation_states) -join "`n") -cne
        "inventory_complete`ndeletes_attempted`nreconciliation_empty" -or
      $operator.inventory_failure_stage -cne 'not_applicable' -or
      $operator.provider_error_category -cne 'none' -or
      $operator.remaining_counts.stores -ne 0 -or
      $operator.remaining_counts.files -ne 0 -or
      $operator.remaining_counts.documents -ne 0) {
    throw 'cleanup_legacy_evidence_mismatch'
  }
}

Assert-ProtectedDirectory $root
Assert-ProtectedDirectory $evidence
Assert-NoReparsePoint $root
Assert-PreservedPrivateRoots
Assert-ExactRetainedTask
Assert-ExactLegacyEvidence
if (Test-Path -LiteralPath $diagnostic) { throw 'cleanup_diagnostic_present' }
Assert-HubHealthy

if ($ValidateOnly) {
  Write-Output 'TASK28_RECONCILIATION_CLEANUP_VALIDATED'
  return
}

Unregister-ScheduledTask `
  -TaskName $taskName -TaskPath $taskPath -Confirm:$false -ErrorAction Stop
if (@(Get-ExactTask).Count -ne 0) { throw 'cleanup_task_removal_unproven' }
Assert-HubHealthy
Assert-PreservedPrivateRoots
Remove-Item -LiteralPath $root -Recurse -Force -ErrorAction Stop
if (Test-Path -LiteralPath $root) { throw 'cleanup_root_removal_unproven' }
Assert-HubHealthy
Assert-PreservedPrivateRoots
Write-Output 'TASK28_RECONCILIATION_CLEANUP_COMPLETE'
