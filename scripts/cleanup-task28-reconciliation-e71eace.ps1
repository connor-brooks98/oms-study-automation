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
$cleanupScript = Join-Path $root 'cleanup-only-disposition.ps1'
$rootManifest = Join-Path $root 'cleanup-root-manifest.sha256'
$privateRoot = Join-Path $env:LOCALAPPDATA 'Temp\sol0-task28-private-shadow-9097851'
$retainedRoot = Join-Path $env:LOCALAPPDATA 'Temp\sol0-task28-private-shadow-06848e2'
$privateEvidence = Join-Path $privateRoot 'evidence'
$retainedEvidence = Join-Path $retainedRoot 'evidence'
$privateDiagnostic = Join-Path $env:TEMP 'sol0-task28-private-shadow-diagnostic-9097851'
$privateScratch = Join-Path $env:TEMP 'sol0-task28-private-shadow-scratch-9097851'
$privateTaskName = 'OMS Sol0 Task28 Private Shadow 9097851'
$approvedBase = Join-Path $env:LOCALAPPDATA 'Temp'
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
  if (((Get-Item -LiteralPath $Path -Force).Attributes -band
        [IO.FileAttributes]::ReparsePoint) -ne 0 -or
      @(Get-ChildItem -LiteralPath $Path -Force -Recurse | Where-Object {
        ($_.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0
      }).Count -ne 0) {
    throw 'cleanup_reparse_point_detected'
  }
}

function Get-CanonicalPath(
  [string]$Base,[string]$Path,[bool]$Container
) {
  $baseFull = [IO.Path]::GetFullPath($Base).TrimEnd('\')
  $pathFull = [IO.Path]::GetFullPath($Path).TrimEnd('\')
  if (-not ($pathFull.StartsWith($baseFull + '\',[StringComparison]::OrdinalIgnoreCase))) {
    throw 'cleanup_path_escape'
  }
  if (-not (Test-Path -LiteralPath $baseFull -PathType Container) -or
      ((Get-Item -LiteralPath $baseFull -Force).Attributes -band
        [IO.FileAttributes]::ReparsePoint) -ne 0) {
    throw 'cleanup_reparse_point_detected'
  }
  $current = $baseFull
  foreach ($part in $pathFull.Substring($baseFull.Length + 1).Split('\')) {
    $current = Join-Path $current $part
    if (-not (Test-Path -LiteralPath $current)) { throw 'cleanup_path_unavailable' }
    if (((Get-Item -LiteralPath $current -Force).Attributes -band
          [IO.FileAttributes]::ReparsePoint) -ne 0) {
      throw 'cleanup_reparse_point_detected'
    }
  }
  $available = if ($Container) {
    Test-Path -LiteralPath $pathFull -PathType Container
  } else {
    Test-Path -LiteralPath $pathFull -PathType Leaf
  }
  if (-not $available) {
    throw 'cleanup_path_unavailable'
  }
  return [IO.Path]::GetFullPath((Get-Item -LiteralPath $pathFull -Force).FullName).TrimEnd('\')
}

function Test-PathWithin([string]$Parent,[string]$Child) {
  return $Child.StartsWith($Parent.TrimEnd('\') + '\',[StringComparison]::OrdinalIgnoreCase)
}

function Assert-DisjointPaths([string]$Left,[string]$Right) {
  if ($Left.Equals($Right,[StringComparison]::OrdinalIgnoreCase) -or
      (Test-PathWithin $Left $Right) -or (Test-PathWithin $Right $Left)) {
    throw 'cleanup_path_overlap'
  }
}

function Get-CleanupPathContract {
  $canonicalRoot = Get-CanonicalPath $approvedBase $root $true
  $canonicalPrivate = Get-CanonicalPath $approvedBase $privateRoot $true
  Assert-DisjointPaths $canonicalRoot $canonicalPrivate
  $canonicalLauncher = Get-CanonicalPath $approvedBase $launcher $false
  $canonicalCleanup = Get-CanonicalPath $approvedBase $cleanupScript $false
  $canonicalManifest = Get-CanonicalPath $approvedBase $rootManifest $false
  foreach ($path in @($canonicalLauncher,$canonicalCleanup,$canonicalManifest)) {
    if (-not (Test-PathWithin $canonicalRoot $path)) { throw 'cleanup_path_escape' }
    Assert-DisjointPaths $canonicalPrivate $path
  }
  return [pscustomobject]@{
    Root = $canonicalRoot
    PrivateRoot = $canonicalPrivate
    Launcher = $canonicalLauncher
  }
}

function Assert-ExactRootManifest {
  Assert-ExactHash $rootManifest 'ea671e594d9494aec7be240e322baa382dc954bab8f1de9ca196c24052887184'
  $listed = [Collections.Generic.HashSet[string]]::new([StringComparer]::OrdinalIgnoreCase)
  foreach ($line in @(Get-Content -LiteralPath $rootManifest)) {
    if ($line -notmatch '^([0-9a-f]{64})  (.+)$') {
      throw 'cleanup_root_manifest_invalid'
    }
    $expectedHash = $Matches[1]
    $relative = $Matches[2]
    if ([IO.Path]::IsPathRooted($relative) -or
        $relative -match '(^|[\\/])\.\.([\\/]|$)' -or
        $relative -like 'evidence/*' -or
        $relative -ceq 'cleanup-only-disposition.ps1' -or
        $relative -ceq 'cleanup-root-manifest.sha256' -or
        -not $listed.Add($relative)) {
      throw 'cleanup_root_manifest_invalid'
    }
    $path = Join-Path $root $relative.Replace('/',[IO.Path]::DirectorySeparatorChar)
    Assert-ExactHash $path $expectedHash
  }
  if ($listed.Count -eq 0) { throw 'cleanup_root_manifest_invalid' }
  $allowed = [Collections.Generic.HashSet[string]]::new(
    $listed,[StringComparer]::OrdinalIgnoreCase
  )
  foreach ($relative in @(
    'evidence/safe-result.json','evidence/safe-status.json',
    'cleanup-only-disposition.ps1','cleanup-root-manifest.sha256'
  )) {
    if (-not $allowed.Add($relative)) { throw 'cleanup_root_manifest_invalid' }
  }
  $actual = @(Get-ChildItem -LiteralPath $root -Recurse -File)
  if ($actual.Count -ne $allowed.Count) { throw 'cleanup_root_inventory_mismatch' }
  foreach ($file in $actual) {
    $relative = $file.FullName.Substring($root.Length + 1).Replace('\','/')
    if (-not $allowed.Contains($relative)) { throw 'cleanup_root_inventory_mismatch' }
  }
}

function Assert-TerminalPrivateTombstone([object]$Paths) {
  Assert-ProtectedDirectory $privateRoot
  Assert-NoReparsePoint $privateRoot
  if ((Test-Path -LiteralPath $privateEvidence) -or
      (Test-Path -LiteralPath $privateDiagnostic) -or
      (Test-Path -LiteralPath $privateScratch) -or
      @(Get-ExactTaskNamed $privateTaskName).Count -ne 0 -or
      @(Get-ChildItem -LiteralPath $privateRoot -Force).Count -ne 19) {
    throw 'cleanup_tombstone_state_mismatch'
  }
  Assert-ExactHash (Join-Path $privateRoot 'source-manifest.sha256') `
    'd36c6a64ef342ff0d4e88c370c794a2add46ef2f98fbdfb9dcabd6bd86f702b0'
  Assert-ExactHash (Join-Path $privateRoot 'runtime-manifest.sha256') `
    'ad8e00b852d32c3b1216452e25e62160a68fb07745f3589321b20fec3ccfc5a7'
  Assert-ExactHash (Join-Path $privateRoot 'private-shadow-controller.ps1') `
    '5a955d65feb3adf03759bd62c8e2f842b2e81a27abfc5c9e10b8912c72796587'
  Assert-ExactHash (Join-Path $privateRoot 'private-shadow-launcher.ps1') `
    '0795af225426707b9a49454b19538b6b0eb420a9f05ab74280d1d541fd87fffa'
  Assert-ExactHash (Join-Path $privateRoot 'private-shadow-operator-entry.py') `
    '96c77c083d665fe945cde5a31265d83276fe07778a1bb732bccee1b28f1acad2'
  if (@(Get-Content -LiteralPath (Join-Path $privateRoot 'source-manifest.sha256')).Count -ne 674 -or
      @(Get-Content -LiteralPath (Join-Path $privateRoot 'runtime-manifest.sha256')).Count -ne 11868) {
    throw 'cleanup_tombstone_state_mismatch'
  }
  $currentPrivate = Get-CanonicalPath $approvedBase $privateRoot $true
  if (-not $currentPrivate.Equals($Paths.PrivateRoot,[StringComparison]::OrdinalIgnoreCase)) {
    throw 'cleanup_tombstone_state_mismatch'
  }
}

function Assert-PreservedPrivateRoots([object]$Paths) {
  Assert-TerminalPrivateTombstone $Paths
  foreach ($directory in @($retainedRoot,$retainedEvidence)) {
    Assert-ProtectedDirectory $directory
  }
  Assert-NoReparsePoint $retainedRoot
  if (@(Get-ChildItem -LiteralPath $retainedEvidence -Force -File).Count -ne 1) {
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

function Get-ExactTaskNamed([string]$Name) {
  return @(Get-ScheduledTask -ErrorAction Stop | Where-Object {
    [string]$_.TaskName -ceq $Name
  })
}

function Get-ExactTask { return @(Get-ExactTaskNamed $taskName) }

function Assert-ExactRetainedTask([object]$Paths) {
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
        [IO.Path]::GetFullPath([string]$actions[0].WorkingDirectory),$Paths.Root,
        [StringComparison]::OrdinalIgnoreCase
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
  $actionLauncher = Get-CanonicalPath $approvedBase $launcher $false
  $actionWorkingDirectory = [IO.Path]::GetFullPath([string]$actions[0].WorkingDirectory)
  $actionExecutable = [IO.Path]::GetFullPath([string]$actions[0].Execute)
  if (-not $actionLauncher.Equals($Paths.Launcher,[StringComparison]::OrdinalIgnoreCase) -or
      -not (Test-PathWithin $Paths.Root $actionLauncher) -or
      (-not $actionWorkingDirectory.Equals(
        $Paths.Root,[StringComparison]::OrdinalIgnoreCase
      ) -and -not (Test-PathWithin $Paths.Root $actionWorkingDirectory)) -or
      -not $actionExecutable.Equals(
        [IO.Path]::GetFullPath($expectedExecutable),[StringComparison]::OrdinalIgnoreCase
      )) {
    throw 'cleanup_path_escape'
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
$paths = Get-CleanupPathContract
if ([IO.Path]::GetFullPath($PSCommandPath) -cne [IO.Path]::GetFullPath($cleanupScript)) {
  throw 'cleanup_script_location_mismatch'
}
Assert-ExactRootManifest
Assert-PreservedPrivateRoots $paths
Assert-ExactRetainedTask $paths
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
Assert-PreservedPrivateRoots $paths
Remove-Item -LiteralPath $root -Recurse -Force -ErrorAction Stop
if (Test-Path -LiteralPath $root) { throw 'cleanup_root_removal_unproven' }
Assert-HubHealthy
Assert-PreservedPrivateRoots $paths
Write-Output 'TASK28_RECONCILIATION_CLEANUP_COMPLETE'
