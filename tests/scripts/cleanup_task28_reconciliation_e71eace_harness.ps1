[CmdletBinding()]
param(
  [Parameter(Mandatory = $true)][string]$CleanupScript,
  [switch]$EarlyTaskMismatch
)

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
$fixtureCleanup = Join-Path $root 'cleanup-only-disposition.ps1'
$rootManifest = Join-Path $root 'cleanup-root-manifest.sha256'
$privateRoot = Join-Path $env:LOCALAPPDATA 'Temp\sol0-task28-private-shadow-9097851'
$retainedRoot = Join-Path $env:LOCALAPPDATA 'Temp\sol0-task28-private-shadow-06848e2'
$privateEvidence = Join-Path $privateRoot 'evidence'
$retainedEvidence = Join-Path $retainedRoot 'evidence'
$privateDiagnostic = Join-Path $env:TEMP 'sol0-task28-private-shadow-diagnostic-9097851'
$privateScratch = Join-Path $env:TEMP 'sol0-task28-private-shadow-scratch-9097851'
$privateTaskName = 'OMS Sol0 Task28 Private Shadow 9097851'
$auditRoot = Join-Path $env:LOCALAPPDATA 'Temp\sol0-task28-reconciliation-e71eace-audit'
$auditPath = Join-Path $auditRoot 'cleanup-failure.json'
$auditTemp = Join-Path $auditRoot 'cleanup-failure.tmp'
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
$global:CleanupTaskExecute = $expectedExecutable
$global:CleanupTaskArguments = $expectedArguments
$global:CleanupTaskWorkingDirectory = $root
$global:CleanupPrivateTaskRegistered = $false
$global:CleanupUnregisterFailure = $false
$global:CleanupPostUnregisterRootDrift = $false
$global:CleanupPreRemovalRootDrift = $false
$global:CleanupRootRemovalFailure = $false
$global:CleanupPostRemovalHubFailure = $false
$global:CleanupImmediatePreUnregisterRootDrift = $false
$global:CleanupImmediatePreRemovalRootDrift = $false
$global:CleanupHealthCalls = 0

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
  if (Test-Path -LiteralPath $env:TEMP) {
    Remove-Item -LiteralPath $env:TEMP -Recurse -Force
  }
  New-Item -ItemType Directory -Path $env:LOCALAPPDATA,$env:TEMP -Force | Out-Null
  New-Item -ItemType Directory -Path $root,$evidence,$privateRoot,$auditRoot,
    $retainedRoot,$retainedEvidence | Out-Null
  foreach ($directory in @($root,$evidence,$privateRoot,$auditRoot,$retainedRoot,$retainedEvidence)) {
    Protect-FixtureDirectory $directory
  }
  [IO.File]::WriteAllBytes($auditPath,[byte[]]@())
  [IO.File]::WriteAllBytes(
    (Join-Path $evidence 'safe-result.json'),
    [Convert]::FromBase64String($resultBase64)
  )
  [IO.File]::WriteAllBytes(
    (Join-Path $evidence 'safe-status.json'),
    [Convert]::FromBase64String($statusBase64)
  )
  $payload = Join-Path $root 'payload.txt'
  [IO.File]::WriteAllText($payload,'bound cleanup payload')
  $fixtureLauncher = Join-Path $root 'reconciliation-launcher.ps1'
  [IO.File]::WriteAllText($fixtureLauncher,'fixture reconciliation launcher')

  $privateFiles = [ordered]@{
    'source-manifest.sha256' = ((1..674 | ForEach-Object {
      ('{0}  source/{1:d4}.py' -f ('a' * 64),$_)
    }) -join [char]10) + [char]10
    'runtime-manifest.sha256' = ((1..11868 | ForEach-Object {
      ('{0}  runtime/{1:d5}.py' -f ('b' * 64),$_)
    }) -join [char]10) + [char]10
    'private-shadow-controller.ps1' = 'fixture controller'
    'private-shadow-launcher.ps1' = 'fixture launcher'
    'private-shadow-operator-entry.py' = 'fixture entrypoint'
  }
  foreach ($item in $privateFiles.GetEnumerator()) {
    [IO.File]::WriteAllText(
      (Join-Path $privateRoot $item.Key),$item.Value,[Text.UTF8Encoding]::new($false)
    )
  }
  foreach ($index in 1..14) {
    New-Item -ItemType Directory -Path (Join-Path $privateRoot ('bound-{0:d2}' -f $index)) |
      Out-Null
  }
  $payloadHash = (Microsoft.PowerShell.Utility\Get-FileHash `
    -LiteralPath $payload -Algorithm SHA256).Hash.ToLowerInvariant()
  $launcherHash = (Microsoft.PowerShell.Utility\Get-FileHash `
    -LiteralPath $fixtureLauncher -Algorithm SHA256).Hash.ToLowerInvariant()
  [IO.File]::WriteAllText(
    $rootManifest,
    $payloadHash + '  payload.txt' + [char]10 +
      $launcherHash + '  reconciliation-launcher.ps1' + [char]10,
    [Text.UTF8Encoding]::new($false)
  )
  $manifestHash = (Microsoft.PowerShell.Utility\Get-FileHash `
    -LiteralPath $rootManifest -Algorithm SHA256).Hash.ToLowerInvariant()
  $cleanupSource = [IO.File]::ReadAllText($CleanupScript)
  $fixtureSource = $cleanupSource.Replace(
    'ea671e594d9494aec7be240e322baa382dc954bab8f1de9ca196c24052887184',
    $manifestHash
  )
  if ($fixtureSource -ceq $cleanupSource) { throw 'fixture_manifest_binding_missing' }
  foreach ($binding in @(
    @('d36c6a64ef342ff0d4e88c370c794a2add46ef2f98fbdfb9dcabd6bd86f702b0','source-manifest.sha256'),
    @('ad8e00b852d32c3b1216452e25e62160a68fb07745f3589321b20fec3ccfc5a7','runtime-manifest.sha256'),
    @('5a955d65feb3adf03759bd62c8e2f842b2e81a27abfc5c9e10b8912c72796587','private-shadow-controller.ps1'),
    @('0795af225426707b9a49454b19538b6b0eb420a9f05ab74280d1d541fd87fffa','private-shadow-launcher.ps1'),
    @('96c77c083d665fe945cde5a31265d83276fe07778a1bb732bccee1b28f1acad2','private-shadow-operator-entry.py')
  )) {
    $fixtureHash = (Microsoft.PowerShell.Utility\Get-FileHash `
      -LiteralPath (Join-Path $privateRoot $binding[1]) -Algorithm SHA256).Hash.ToLowerInvariant()
    $fixtureSource = $fixtureSource.Replace($binding[0],$fixtureHash)
  }
  [IO.File]::WriteAllText($fixtureCleanup,$fixtureSource,[Text.UTF8Encoding]::new($false))
  [IO.File]::WriteAllText((Join-Path $retainedEvidence 'retained.json'),'{}')
  $global:CleanupTaskRegistered = $true
  $global:CleanupTaskState = 'Ready'
  $global:CleanupHealthSchema = 29
  $global:CleanupUnregisterCalls = @()
  $global:CleanupTaskExecute = $expectedExecutable
  $global:CleanupTaskArguments = $expectedArguments
  $global:CleanupTaskWorkingDirectory = $root
  $global:CleanupPrivateTaskRegistered = $false
  $global:CleanupUnregisterFailure = $false
  $global:CleanupPostUnregisterRootDrift = $false
  $global:CleanupPreRemovalRootDrift = $false
  $global:CleanupRootRemovalFailure = $false
  $global:CleanupPostRemovalHubFailure = $false
  $global:CleanupImmediatePreUnregisterRootDrift = $false
  $global:CleanupImmediatePreRemovalRootDrift = $false
  $global:CleanupHealthCalls = 0
}

function Get-TombstoneSnapshot {
  if (-not (Test-Path -LiteralPath $privateRoot -PathType Container)) { return 'missing' }
  $records = @(Get-ChildItem -LiteralPath $privateRoot -Force -Recurse | Sort-Object FullName |
    ForEach-Object {
      $relative = $_.FullName.Substring($privateRoot.Length)
      $hash = if (-not $_.PSIsContainer) {
        (Microsoft.PowerShell.Utility\Get-FileHash -LiteralPath $_.FullName -Algorithm SHA256).Hash
      } else { '' }
      '{0}|{1}|{2}|{3}' -f $relative,$_.Attributes,$_.Length,$hash
    })
  return ($records -join [char]10)
}

function New-CleanupTaskFixture {
  $sid = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value
  [pscustomobject]@{
    TaskName = $script:taskName
    TaskPath = $script:taskPath
    State = $global:CleanupTaskState
    Actions = @([pscustomobject]@{
      Execute = $global:CleanupTaskExecute
      Arguments = $global:CleanupTaskArguments
      WorkingDirectory = $global:CleanupTaskWorkingDirectory
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
  if ($global:CleanupPrivateTaskRegistered) {
    $items += [pscustomobject]@{TaskName=$script:privateTaskName;TaskPath='\';State='Ready'}
  }
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
  param([string]$TaskName,[string]$TaskPath)
  if ($TaskName -cne $script:taskName -or $TaskPath -cne $script:taskPath) {
    throw 'unexpected_unregister_target'
  }
  if ($global:CleanupUnregisterFailure) { throw 'synthetic_unregister_failure' }
  $global:CleanupUnregisterCalls += "$TaskPath$TaskName"
  $global:CleanupTaskRegistered = $false
  if ($global:CleanupPostUnregisterRootDrift) {
    [IO.File]::WriteAllText((Join-Path $script:root 'post-unregister-drift.txt'),'drift')
  }
}

function Invoke-RestMethod {
  [CmdletBinding()]
  param([string]$Uri,[int]$TimeoutSec,[string]$Method)
  if ($Uri -notin @('http://127.0.0.1:8765/health','http://127.0.0.1:8788/health')) {
    throw 'unexpected_health_uri'
  }
  $global:CleanupHealthCalls += 1
  if ($global:CleanupImmediatePreUnregisterRootDrift -and
      $global:CleanupHealthCalls -eq 2) {
    [IO.File]::WriteAllText((Join-Path $script:root 'immediate-unregister-drift.txt'),'drift')
  }
  if ($global:CleanupPreRemovalRootDrift -and $global:CleanupHealthCalls -eq 6) {
    [IO.File]::WriteAllText((Join-Path $script:root 'pre-removal-drift.txt'),'drift')
  }
  if ($global:CleanupImmediatePreRemovalRootDrift -and
      $global:CleanupHealthCalls -eq 8) {
    [IO.File]::WriteAllText((Join-Path $script:root 'immediate-removal-drift.txt'),'drift')
  }
  $schema = if ($global:CleanupPostRemovalHubFailure -and
    -not (Test-Path -LiteralPath $script:root) -and $global:CleanupHealthCalls -ge 5) {
    28
  } else { $global:CleanupHealthSchema }
  [pscustomobject]@{status='ok';schema_version=$schema}
}

function Remove-Item {
  [CmdletBinding()]
  param(
    [string[]]$LiteralPath,
    [switch]$Recurse,
    [switch]$Force
  )
  $effectiveErrorAction = if ($PSBoundParameters.ContainsKey('ErrorAction')) {
    $PSBoundParameters['ErrorAction']
  } else { 'Continue' }
  foreach ($path in $LiteralPath) {
    if ($global:CleanupRootRemovalFailure -and
        [string]::Equals($path,$script:root,[StringComparison]::OrdinalIgnoreCase)) {
      throw 'synthetic_root_removal_failure'
    }
    Microsoft.PowerShell.Management\Remove-Item -LiteralPath $path `
      -Recurse:$Recurse -Force:$Force -ErrorAction $effectiveErrorAction
  }
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

function Assert-FailureAudit(
  [string]$Stage,[string]$TaskPredicate,[string]$RootPredicate,
  [string]$TombstonePredicate,[string]$HubPredicate,
  [bool]$Attempted,[bool]$Completed
) {
  if (-not (Test-Path -LiteralPath $auditPath -PathType Leaf) -or
      (Get-Item -LiteralPath $auditPath).Length -eq 0 -or
      (Test-Path -LiteralPath $auditTemp)) {
    throw 'failure_audit_missing_or_nonatomic'
  }
  $audit = Get-Content -LiteralPath $auditPath -Raw | ConvertFrom-Json
  $names = @($audit.PSObject.Properties.Name | Sort-Object)
  $expectedNames = @(
    'failure_stage','hub_predicate','mutation_attempted','mutation_completed',
    'reconciliation_root_predicate','task_predicate','tombstone_predicate'
  ) | Sort-Object
  if (($names -join "`n") -cne ($expectedNames -join "`n") -or
      $audit.failure_stage -cne $Stage -or
      $audit.task_predicate -cne $TaskPredicate -or
      $audit.reconciliation_root_predicate -cne $RootPredicate -or
      $audit.tombstone_predicate -cne $TombstonePredicate -or
      $audit.hub_predicate -cne $HubPredicate -or
      $audit.mutation_attempted -ne $Attempted -or
      $audit.mutation_completed -ne $Completed -or
      ([IO.File]::ReadAllText($auditPath) -match 'sol0-task28|C:\\|provider|private')) {
    throw 'failure_audit_contract_mismatch'
  }
}

try {
  if ($EarlyTaskMismatch) {
    Initialize-CleanupFixture
    $global:CleanupTaskState = 'Running'
    Assert-FailureRetained { & $fixtureCleanup }
    Assert-FailureAudit 'pre_unregister_validation' 'failed' 'bound' `
      'bound' 'healthy' $false $false
    Write-Output 'TASK28_EARLY_TASK_MISMATCH_AUDITED'
    return
  }

  $earlyOutput = & (Join-Path $PSHOME 'powershell.exe') -NoProfile `
    -ExecutionPolicy Bypass -File $PSCommandPath -CleanupScript $CleanupScript `
    -EarlyTaskMismatch
  if ($LASTEXITCODE -ne 0 -or
      $earlyOutput -notcontains 'TASK28_EARLY_TASK_MISMATCH_AUDITED') {
    throw 'early_task_mismatch_audit_unproven'
  }

  Initialize-CleanupFixture
  $tombstoneBefore = Get-TombstoneSnapshot
  $validation = & $fixtureCleanup -ValidateOnly
  if ($validation -notcontains 'TASK28_RECONCILIATION_CLEANUP_VALIDATED' -or
      -not $global:CleanupTaskRegistered -or
      -not (Test-Path -LiteralPath $root -PathType Container) -or
      (Test-Path -LiteralPath $privateEvidence) -or
      (Get-TombstoneSnapshot) -cne $tombstoneBefore -or
      (Get-Item -LiteralPath $auditPath).Length -ne 0 -or
      $global:CleanupUnregisterCalls.Count -ne 0) {
    throw 'validate_only_mutated_state'
  }

  Initialize-CleanupFixture
  [IO.File]::AppendAllText((Join-Path $evidence 'safe-result.json'),'x')
  Assert-FailureRetained { & $fixtureCleanup -ValidateOnly }

  Initialize-CleanupFixture
  $global:CleanupTaskState = 'Running'
  Assert-FailureRetained { & $fixtureCleanup -ValidateOnly }

  Initialize-CleanupFixture
  $global:CleanupHealthSchema = 28
  Assert-FailureRetained { & $fixtureCleanup -ValidateOnly }

  Initialize-CleanupFixture
  [IO.File]::WriteAllText((Join-Path $root 'unexpected.txt'),'unexpected')
  Assert-FailureRetained { & $fixtureCleanup -ValidateOnly }

  Initialize-CleanupFixture
  $rootTarget = $root + '-target'
  Move-Item -LiteralPath $root -Destination $rootTarget
  New-Item -ItemType Junction -Path $root -Target $rootTarget | Out-Null
  Assert-FailureRetained { & $fixtureCleanup -ValidateOnly }

  Initialize-CleanupFixture
  $approvedBase = Join-Path $env:LOCALAPPDATA 'Temp'
  $approvedBaseTarget = $approvedBase + '-target'
  Move-Item -LiteralPath $approvedBase -Destination $approvedBaseTarget
  New-Item -ItemType Junction -Path $approvedBase -Target $approvedBaseTarget | Out-Null
  Assert-FailureRetained { & $fixtureCleanup -ValidateOnly }

  Initialize-CleanupFixture
  New-Item -ItemType Directory -Path $privateEvidence | Out-Null
  Assert-FailureRetained { & $fixtureCleanup -ValidateOnly }

  Initialize-CleanupFixture
  $global:CleanupPrivateTaskRegistered = $true
  Assert-FailureRetained { & $fixtureCleanup -ValidateOnly }

  Initialize-CleanupFixture
  New-Item -ItemType Directory -Path $privateDiagnostic | Out-Null
  Assert-FailureRetained { & $fixtureCleanup -ValidateOnly }

  Initialize-CleanupFixture
  New-Item -ItemType Directory -Path $privateScratch | Out-Null
  Assert-FailureRetained { & $fixtureCleanup -ValidateOnly }

  Initialize-CleanupFixture
  [IO.File]::AppendAllText((Join-Path $privateRoot 'private-shadow-controller.ps1'),'x')
  Assert-FailureRetained { & $fixtureCleanup -ValidateOnly }

  Initialize-CleanupFixture
  New-Item -ItemType Directory -Path (Join-Path $privateRoot 'unexpected-top-level') | Out-Null
  Assert-FailureRetained { & $fixtureCleanup -ValidateOnly }

  Initialize-CleanupFixture
  $sourceManifest = Join-Path $privateRoot 'source-manifest.sha256'
  [IO.File]::WriteAllLines($sourceManifest,@([IO.File]::ReadAllLines($sourceManifest)[0]))
  Assert-FailureRetained { & $fixtureCleanup -ValidateOnly }

  Initialize-CleanupFixture
  $global:CleanupTaskExecute = Join-Path $root 'powershell.exe'
  Assert-FailureRetained { & $fixtureCleanup -ValidateOnly }

  Initialize-CleanupFixture
  $global:CleanupTaskArguments = '-File "C:\outside\reconciliation-launcher.ps1"'
  Assert-FailureRetained { & $fixtureCleanup -ValidateOnly }

  Initialize-CleanupFixture
  $global:CleanupTaskWorkingDirectory = $privateRoot
  Assert-FailureRetained { & $fixtureCleanup -ValidateOnly }

  Initialize-CleanupFixture
  $privateTarget = $privateRoot + '-target'
  Move-Item -LiteralPath $privateRoot -Destination $privateTarget
  New-Item -ItemType Junction -Path $privateRoot -Target $privateTarget | Out-Null
  Assert-FailureRetained { & $fixtureCleanup -ValidateOnly }

  Initialize-CleanupFixture
  $global:CleanupHealthSchema = 28
  Assert-FailureRetained { & $fixtureCleanup }
  Assert-FailureAudit 'pre_unregister_validation' 'exact_task_present' 'bound' `
    'bound' 'failed' $false $false

  Initialize-CleanupFixture
  $global:CleanupImmediatePreUnregisterRootDrift = $true
  Assert-FailureRetained { & $fixtureCleanup }
  Assert-FailureAudit 'pre_unregister_validation' 'exact_task_present' 'failed' `
    'bound' 'healthy' $false $false

  Initialize-CleanupFixture
  $global:CleanupUnregisterFailure = $true
  Assert-FailureRetained { & $fixtureCleanup }
  Assert-FailureAudit 'unregister_request' 'exact_task_present' 'bound' `
    'bound' 'healthy' $true $false

  Initialize-CleanupFixture
  $global:CleanupPostUnregisterRootDrift = $true
  $failed = $false
  try { & $fixtureCleanup } catch { $failed = $true }
  if (-not $failed -or $global:CleanupTaskRegistered -or
      -not (Test-Path -LiteralPath $root -PathType Container)) {
    throw 'post_unregister_failure_contract_mismatch'
  }
  Assert-FailureAudit 'post_unregister_validation' 'exact_task_absent' 'failed' `
    'bound' 'healthy' $true $true

  Initialize-CleanupFixture
  $global:CleanupPreRemovalRootDrift = $true
  $failed = $false
  try { & $fixtureCleanup } catch { $failed = $true }
  if (-not $failed -or $global:CleanupTaskRegistered -or
      -not (Test-Path -LiteralPath $root -PathType Container)) {
    throw 'pre_removal_failure_contract_mismatch'
  }
  Assert-FailureAudit 'pre_root_removal_validation' 'exact_task_absent' 'failed' `
    'bound' 'healthy' $false $false

  Initialize-CleanupFixture
  $global:CleanupImmediatePreRemovalRootDrift = $true
  $failed = $false
  try { & $fixtureCleanup } catch { $failed = $true }
  if (-not $failed -or $global:CleanupTaskRegistered -or
      -not (Test-Path -LiteralPath $root -PathType Container)) {
    throw 'immediate_pre_removal_failure_contract_mismatch'
  }
  Assert-FailureAudit 'pre_root_removal_validation' 'exact_task_absent' 'failed' `
    'bound' 'healthy' $false $false

  Initialize-CleanupFixture
  $global:CleanupRootRemovalFailure = $true
  $failed = $false
  try { & $fixtureCleanup } catch { $failed = $true }
  if (-not $failed -or $global:CleanupTaskRegistered -or
      -not (Test-Path -LiteralPath $root -PathType Container)) {
    throw 'root_removal_failure_contract_mismatch'
  }
  Assert-FailureAudit 'root_removal' 'exact_task_absent' 'bound' `
    'bound' 'healthy' $true $false

  Initialize-CleanupFixture
  $global:CleanupPostRemovalHubFailure = $true
  $failed = $false
  try { & $fixtureCleanup } catch { $failed = $true }
  if (-not $failed -or $global:CleanupTaskRegistered -or
      (Test-Path -LiteralPath $root)) {
    throw 'post_removal_failure_contract_mismatch'
  }
  Assert-FailureAudit 'post_root_removal_validation' 'exact_task_absent' 'absent' `
    'bound' 'failed' $true $true

  Initialize-CleanupFixture
  $tombstoneBefore = Get-TombstoneSnapshot
  & $fixtureCleanup
  if ($global:CleanupTaskRegistered -or
      (Test-Path -LiteralPath $root) -or
      $global:CleanupUnregisterCalls.Count -ne 1 -or
      -not (Test-Path -LiteralPath $privateRoot -PathType Container) -or
      (Test-Path -LiteralPath $privateEvidence) -or
      (Get-TombstoneSnapshot) -cne $tombstoneBefore -or
      (Get-Item -LiteralPath $auditPath).Length -ne 0 -or
      -not (Test-Path -LiteralPath $retainedRoot -PathType Container)) {
    throw 'exact_cleanup_success_contract_failed'
  }
  Write-Output 'TASK28_RECONCILIATION_CLEANUP_HARNESS_VERIFIED'
} finally {
  $env:LOCALAPPDATA = $oldLocalAppData
  $env:TEMP = $oldTemp
  Remove-Item -LiteralPath $sandbox -Recurse -Force -ErrorAction SilentlyContinue
}
