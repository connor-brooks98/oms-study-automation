[CmdletBinding()]
param(
  [Parameter(Mandatory = $true)]
  [ValidateSet("Preflight", "Deploy", "Postflight")]
  [string]$Mode,
  [Parameter(Mandatory = $true)][string]$ExpectedScriptSha256,
  [Parameter(Mandatory = $true)][string]$ExpectedMergedCommit,
  [Parameter(Mandatory = $true)][string]$ExpectedMergedTree,
  [string]$BindingJsonBase64 = "",
  [string]$ExpectedBackupPath = ""
)

# This is deliberately a one-release transaction, not a general deployment framework.
$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"
$ProjectRoot = "C:\Services\oms-study-automation-v2"
$TaskName = "OMS Study Hub V2"
$EnvFile = Join-Path $ProjectRoot ".env"
$Installer = Join-Path $ProjectRoot "scripts\install-windows.ps1"
$PlayerAsset = Join-Path $ProjectRoot "src\oms_hub\web\static\public_quiz.js"
$script:CheckoutMutated = $false
$script:InstallerStarted = $false
$script:RollbackAttempted = $false

function Get-FileSha256 {
  param([string]$Path)
  return (Get-FileHash -LiteralPath $Path -Algorithm SHA256 -ErrorAction Stop).Hash.ToLowerInvariant()
}

function Get-StringSha256 {
  param([string]$Value)
  $hasher = [Security.Cryptography.SHA256]::Create()
  try {
    $bytes = (New-Object Text.UTF8Encoding($false)).GetBytes($Value)
    return ([BitConverter]::ToString($hasher.ComputeHash($bytes))).Replace("-", "").ToLowerInvariant()
  } finally { $hasher.Dispose() }
}

function Assert-NativeSuccess {
  param([string]$Operation)
  if ($LASTEXITCODE -ne 0) { throw "$Operation failed with native exit code $LASTEXITCODE." }
}

function Assert-NonReparsePath {
  param([string]$Path)
  $cursor = [IO.Path]::GetFullPath($Path)
  while ($true) {
    $item = Get-Item -LiteralPath $cursor -Force -ErrorAction Stop
    if (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
      throw "Reparse point is not permitted: $cursor"
    }
    $parent = Split-Path -LiteralPath $cursor -Parent
    if (-not $parent -or $parent -eq $cursor) { return }
    $cursor = $parent
  }
}

function Assert-Leaf {
  param([string]$Path)
  Assert-NonReparsePath -Path $Path
  if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) { throw "Missing leaf: $Path" }
}

function Assert-Directory {
  param([string]$Path)
  Assert-NonReparsePath -Path $Path
  if (-not (Test-Path -LiteralPath $Path -PathType Container)) { throw "Missing directory: $Path" }
}

function Get-DotEnvValue {
  param([string]$Name)
  $escapedName = [regex]::Escape($Name)
  $line = Get-Content -LiteralPath $EnvFile | Where-Object {
    $_ -match "^\s*(?:export\s+)?${escapedName}\s*="
  } | Select-Object -Last 1
  if ($null -eq $line) { return $null }
  $value = ([string]$line -replace "^\s*(?:export\s+)?${escapedName}\s*=\s*", "").Trim()
  if ($value.StartsWith('"') -or $value.StartsWith("'")) {
    $quote = [string]$value[0]
    $closing = $value.IndexOf($quote, 1)
    if ($closing -lt 1) { throw "Unterminated $Name assignment." }
    if (($value.Substring($closing + 1).Trim()) -notmatch "^(|#.*)$") { throw "Trailing $Name assignment content." }
    return $value.Substring(1, $closing - 1)
  }
  return ([regex]::Replace($value, "\s+#.*$", "")).Trim()
}

function Get-EffectiveSetting {
  param([string]$Name, [string]$DefaultValue)
  $processValue = [Environment]::GetEnvironmentVariable($Name, "Process")
  if ($null -ne $processValue) { return $processValue }
  $fileValue = Get-DotEnvValue -Name $Name
  if ($null -ne $fileValue) { return $fileValue }
  return $DefaultValue
}

function Get-ReleaseConfiguration {
  Assert-Directory -Path $ProjectRoot
  Assert-Leaf -Path $EnvFile
  $dataRoot = [IO.Path]::GetFullPath((Get-EffectiveSetting "OMS_HUB_DATA_DIR" "C:\ProgramData\OMSStudyHub"))
  $databaseUrl = Get-EffectiveSetting "OMS_HUB_DATABASE_URL" "sqlite:///C:/ProgramData/OMSStudyHub/hub.db"
  if (-not $databaseUrl.StartsWith("sqlite:///", [StringComparison]::OrdinalIgnoreCase)) { throw "Only sqlite:/// is supported." }
  $databaseRawPath = $databaseUrl.Substring("sqlite:///".Length).Replace("/", "\")
  if ([string]::IsNullOrWhiteSpace($databaseRawPath) -or $databaseRawPath -eq ":memory:") { throw "Invalid SQLite path." }
  if (-not [IO.Path]::IsPathRooted($databaseRawPath)) { $databaseRawPath = Join-Path $ProjectRoot $databaseRawPath }
  $databasePath = [IO.Path]::GetFullPath($databaseRawPath)
  $portText = Get-EffectiveSetting "OMS_HUB_DASHBOARD_PORT" "8787"
  $port = 0
  if (-not [int]::TryParse($portText, [ref]$port) -or $port -lt 1024 -or $port -gt 65535) { throw "Invalid dashboard port." }
  Assert-Directory -Path $dataRoot
  Assert-Leaf -Path $databasePath
  return [ordered]@{ data_root=$dataRoot; database_url=$databaseUrl; database_path=$databasePath; port=$port; env_sha256=(Get-FileSha256 $EnvFile) }
}

function Invoke-Git {
  param([string[]]$Arguments)
  $result = @(& git.exe -C $ProjectRoot @Arguments 2>$null)
  Assert-NativeSuccess -Operation ("git " + ($Arguments -join " "))
  return @($result)
}

function Get-SourceIdentity {
  $commit = ([string](Invoke-Git @("rev-parse", "HEAD"))[0]).Trim().ToLowerInvariant()
  $tree = ([string](Invoke-Git @("rev-parse", "HEAD^{tree}"))[0]).Trim().ToLowerInvariant()
  if (@(Invoke-Git @("status", "--porcelain=v1", "--untracked-files=no")).Count -ne 0) { throw "Tracked checkout is dirty." }
  return [ordered]@{ commit=$commit; tree=$tree }
}

function Get-SystemPowerShellPath {
  $path = [IO.Path]::GetFullPath((Join-Path [Environment]::SystemDirectory "WindowsPowerShell\v1.0\powershell.exe"))
  Assert-Leaf -Path $path
  return $path
}

function Get-TaskXmlSha256 { return Get-StringSha256 (Export-ScheduledTask -TaskName $TaskName -ErrorAction Stop) }

function Get-ProcessOwner {
  param([int]$ProcessId)
  $process = Get-CimInstance Win32_Process -Filter "ProcessId=$ProcessId" -ErrorAction Stop
  $owner = Invoke-CimMethod -InputObject $process -MethodName GetOwner -ErrorAction Stop
  if ($owner.ReturnValue -ne 0) { throw "Cannot determine owner for process $ProcessId." }
  return "{0}\{1}" -f [string]$owner.Domain, [string]$owner.User
}

function Get-ProcessSnapshot {
  return @(
    Get-CimInstance Win32_Process | ForEach-Object {
      [ordered]@{
        process_id=[int]$_.ProcessId; parent_process_id=[int]$_.ParentProcessId
        name=[string]$_.Name; executable_path=[string]$_.ExecutablePath; command_line=[string]$_.CommandLine
        creation_date=([datetime]$_.CreationDate).ToUniversalTime().ToString("o")
      }
    }
  )
}

function Get-LoopbackListener {
  param([int]$Port)
  $listeners = @(Get-NetTCPConnection -State Listen -LocalAddress 127.0.0.1 -LocalPort $Port -ErrorAction Stop)
  if ($listeners.Count -ne 1) { throw "Expected exactly one loopback listener; observed $($listeners.Count)." }
  $processId = [int]$listeners[0].OwningProcess
  $process = Get-CimInstance Win32_Process -Filter "ProcessId=$processId" -ErrorAction Stop
  return [ordered]@{
    process_id=$processId; creation_date=([datetime]$process.CreationDate).ToUniversalTime().ToString("o")
    executable_path=[string]$process.ExecutablePath; command_line=[string]$process.CommandLine; owner=(Get-ProcessOwner $processId)
  }
}

function Assert-TaskBinding {
  param([object]$Configuration, [object]$Binding)
  $task = Get-ScheduledTask -TaskName $TaskName -ErrorAction Stop
  if ([string]$task.State -cne "Running" -or [string]$task.Settings.ExecutionTimeLimit -cne "PT0S") { throw "Scheduled task state differs." }
  if ([string]$task.Principal.UserId -cne [string]$Binding.task_principal -or [string]$task.Principal.LogonType -cne [string]$Binding.task_logon_type) { throw "Scheduled task principal differs." }
  if ((Get-TaskXmlSha256) -cne [string]$Binding.task_xml_sha256) { throw "Scheduled task XML differs." }
  $actions = @($task.Actions)
  $expectedIds = @("f28-primary-0", "f28-recovery-1", "f28-recovery-2", "f28-recovery-3")
  if ($actions.Count -ne 4) { throw "Scheduled task action count differs." }
  $systemPowerShell = Get-SystemPowerShellPath
  $start = Join-Path $ProjectRoot "scripts\start-hub.ps1"
  $recovery = Join-Path $ProjectRoot "scripts\restart-hub-after-failure.ps1"
  for ($index = 0; $index -lt 4; $index++) {
    $scriptPath = if ($index -eq 0) { $start } else { $recovery }
    $arguments = if ($index -eq 0) {
      "-NoProfile -ExecutionPolicy Bypass -File `"$scriptPath`" -DataRoot `"$($Configuration.data_root)`" -ActionIndex 0"
    } else {
      "-NoProfile -ExecutionPolicy Bypass -File `"$scriptPath`" -DataRoot `"$($Configuration.data_root)`" -ActionIndex $index -DelaySeconds 60"
    }
    if ([string]$actions[$index].Id -cne $expectedIds[$index] -or -not [string]::Equals(([string]$actions[$index].Execute).Trim(), $systemPowerShell, [StringComparison]::OrdinalIgnoreCase) -or -not [string]::Equals(([string]$actions[$index].Arguments).Trim(), $arguments, [StringComparison]::Ordinal) -or -not [string]::Equals(([string]$actions[$index].WorkingDirectory).TrimEnd("\"), $ProjectRoot.TrimEnd("\"), [StringComparison]::OrdinalIgnoreCase)) { throw "Scheduled task action $index differs." }
  }
}

function Assert-ListenerTaskOwnership {
  param([object]$Listener, [object]$Configuration, [object]$Binding)
  $rootPrefix = $ProjectRoot.TrimEnd("\") + "\"
  if ([string]$Listener.executable_path -notlike ($rootPrefix + "*") -or -not ([IO.Path]::GetFileName([string]$Listener.executable_path) -ieq "oms-hub.exe")) { throw "Listener is not same-root oms-hub.exe." }
  if ([string]$Listener.owner -cne [string]$Binding.process_identity -or [string]$Listener.owner -cne [string]$Binding.task_principal -or [string]$Listener.owner -cne [string]$Binding.deployment_identity) { throw "Listener, task, and deployment identities differ." }
  $all = Get-ProcessSnapshot
  $byId = @{}; foreach ($row in $all) { $byId[[string]$row.process_id] = $row }
  $systemPowerShell = Get-SystemPowerShellPath
  $startScript = Join-Path $ProjectRoot "scripts\start-hub.ps1"
  $matchingAncestors = 0; $seen = @{}; $cursor = [int]$Listener.process_id
  while ($byId.ContainsKey([string]$cursor)) {
    if ($seen.ContainsKey([string]$cursor)) { throw "Process ancestry cycle." }
    $seen[[string]$cursor] = $true; $row = $byId[[string]$cursor]
    if ([string]::Equals([string]$row.executable_path, $systemPowerShell, [StringComparison]::OrdinalIgnoreCase) -and [string]$row.command_line -like ("*" + $startScript + "*") -and [string]$row.command_line -like ("*-DataRoot*" + $Configuration.data_root + "*") -and [string]$row.command_line -match "(?:^|\s)-ActionIndex\s+0(?:\s|$)") { $matchingAncestors++ }
    $cursor = [int]$row.parent_process_id
  }
  if ($matchingAncestors -ne 1) { throw "Listener must have exactly one task-launched primary system PowerShell ancestor." }
}

function Assert-ReadyHealth {
  param([object]$Configuration, [object]$Binding, [string]$Commit, [string]$Tree)
  $health = Invoke-RestMethod -Uri ("http://127.0.0.1:" + $Configuration.port + "/health/ready") -TimeoutSec 3
  if ([string]$health.status -cne "ok" -or $health.database_reachable -ne $true -or [string]$health.deployment_root -cne $ProjectRoot -or [string]$health.build_revision -cne $Commit -or [string]$health.build_tree -cne $Tree -or [int]$health.schema_version -ne [int]$Binding.schema_version) { throw "Ready health identity, database, or schema differs." }
  $wantedWorkers = @("generation_worker", "ingestion_worker", "studio_worker")
  if ((@($health.workers.PSObject.Properties.Name | Sort-Object) -join "|") -cne ($wantedWorkers -join "|")) { throw "Ready health workers differ." }
  foreach ($workerName in $wantedWorkers) { $worker = $health.workers.$workerName; if ($worker.alive -ne $true -or [int]$worker.start_count -ne 1 -or $null -ne $worker.active_work_age_seconds) { throw "Ready health worker differs: $workerName" } }
}

function Get-ReleasePaths {
  param([object]$Binding)
  $paths = @(Invoke-Git @("diff", "--name-only", $Binding.old_commit, $Binding.merged_commit))
  foreach ($path in $paths) {
    if (@(Invoke-Git @("ls-files", "--others", "--exclude-standard", "--", $path)).Count -ne 0 -or @(Invoke-Git @("ls-files", "--others", "--ignored", "--exclude-standard", "--", $path)).Count -ne 0) { throw "Ignored or untracked release-path collision: $path" }
  }
  return $paths
}

function Get-BackupNames { param([string]$Root); Assert-Directory $Root; return @((Get-ChildItem -LiteralPath $Root -Directory -Force | ForEach-Object Name) | Sort-Object) }

function Assert-ChildPath {
  param([string]$Parent, [string]$RelativePath)
  if ([string]::IsNullOrWhiteSpace($RelativePath) -or [IO.Path]::IsPathRooted($RelativePath) -or $RelativePath -match "(^|\\)\.\.?(\\|$)") { throw "Unsafe backup member path." }
  $candidate = [IO.Path]::GetFullPath((Join-Path $Parent $RelativePath.Replace("/", "\")))
  $prefix = $Parent.TrimEnd("\") + "\"
  if (-not $candidate.StartsWith($prefix, [StringComparison]::OrdinalIgnoreCase)) { throw "Backup member escapes its root." }
  return $candidate
}

function Assert-VerifiedRollbackBackup {
  param([string]$Path, [object]$Configuration, [object]$Binding)
  Assert-Directory $Path
  $manifestPath = Join-Path $Path "backup-manifest.json"; $sidecarPath = Join-Path $Path "backup-manifest.json.sha256"; $completePath = Join-Path $Path "backup-complete.json"; $configPath = Join-Path $Path "effective-config.json"
  foreach ($requiredPath in @($manifestPath, $sidecarPath, $completePath, $configPath)) { Assert-Leaf $requiredPath }
  $manifestHash = Get-FileSha256 $manifestPath
  if ((Get-Content -LiteralPath $sidecarPath -Raw).Trim() -cne "$manifestHash  backup-manifest.json") { throw "Backup manifest sidecar differs." }
  $complete = Get-Content -LiteralPath $completePath -Raw | ConvertFrom-Json
  $manifest = Get-Content -LiteralPath $manifestPath -Raw | ConvertFrom-Json
  $effectiveConfig = Get-Content -LiteralPath $configPath -Raw | ConvertFrom-Json
  if ([string]$complete.status -cne "complete" -or [string]$complete.manifest -cne "backup-manifest.json" -or [string]$complete.manifest_sha256 -cne $manifestHash -or $complete.database_backed_up -ne $true -or [string]$complete.database_path -cne $Configuration.database_path) { throw "Backup completion record differs." }
  if ([int]$manifest.schema_version -ne 1 -or [string]$manifest.project_root -cne $ProjectRoot -or [string]$manifest.database_path -cne $Configuration.database_path -or $manifest.database.backed_up -ne $true -or [string]$manifest.database.source_path -cne $Configuration.database_path -or [string]$manifest.database.source_url -cne $Configuration.database_url) { throw "Backup manifest bindings differ." }
  if ([string]$effectiveConfig.project_root -cne $ProjectRoot -or [string]$effectiveConfig.data_root -cne $Configuration.data_root -or [string]$effectiveConfig.database_path -cne $Configuration.database_path -or [string]$effectiveConfig.database_url -cne $Configuration.database_url -or [string]$effectiveConfig.build_revision -cne $Binding.merged_commit -or [string]$effectiveConfig.build_tree -cne $Binding.merged_tree) { throw "Backup effective configuration differs." }
  $members = @{}; foreach ($member in @($manifest.files)) { $relative = ([string]$member.path).Replace("/", "\"); $key = $relative.ToLowerInvariant(); if ($members.ContainsKey($key)) { throw "Duplicate backup member." }; $memberPath = Assert-ChildPath $Path $relative; Assert-Leaf $memberPath; if ((Get-FileSha256 $memberPath) -cne [string]$member.sha256 -or (Get-Item -LiteralPath $memberPath).Length -ne [long]$member.size) { throw "Backup member hash or size differs." }; $members[$key] = $memberPath }
  $databaseBackup = Assert-ChildPath $Path ([string]$manifest.database.backup_path)
  $taskBackup = Assert-ChildPath $Path ([string]$effectiveConfig.scheduled_task.xml)
  foreach ($criticalPath in @($databaseBackup, $taskBackup, $configPath)) { if (-not $members.ContainsKey(($criticalPath.Substring(($Path.TrimEnd("\") + "\").Length)).ToLowerInvariant())) { throw "Critical backup member missing or not exactly once." } }
  if ($effectiveConfig.scheduled_task.existed -ne $true -or (Get-FileSha256 $taskBackup) -cne [string]$effectiveConfig.scheduled_task.sha256 -or (Get-FileSha256 $taskBackup) -cne [string]$Binding.task_xml_sha256) { throw "Backup task XML differs." }
  return [ordered]@{ path=$Path; database=$databaseBackup; task_xml=$taskBackup; artifacts=(Join-Path $Path "artifacts") }
}

function Stop-VerifiedRuntime {
  param([object]$Configuration, [object]$Binding)
  Stop-ScheduledTask -TaskName $TaskName -ErrorAction Stop
  $clearSnapshots = 0; $deadline = (Get-Date).AddSeconds(30)
  while ((Get-Date) -lt $deadline) {
    $listeners = @(Get-NetTCPConnection -State Listen -LocalAddress 127.0.0.1 -LocalPort $Configuration.port -ErrorAction SilentlyContinue)
    if ($listeners.Count -eq 0) { $clearSnapshots++; if ($clearSnapshots -ge 2) { return }; Start-Sleep -Milliseconds 500; continue }
    if ($listeners.Count -ne 1) { throw "More than one listener during stop." }
    $clearSnapshots = 0; $targetId = [int]$listeners[0].OwningProcess
    $selected = Get-CimInstance Win32_Process -Filter "ProcessId=$targetId" -ErrorAction Stop
    $selectedCreation = ([datetime]$selected.CreationDate).ToUniversalTime().ToString("o")
    $selectedOwner = Get-ProcessOwner $targetId
    $selectedListener = [ordered]@{ process_id=$targetId; creation_date=$selectedCreation; executable_path=[string]$selected.ExecutablePath; command_line=[string]$selected.CommandLine; owner=$selectedOwner }
    Assert-ListenerTaskOwnership $selectedListener $Configuration $Binding
    $revalidated = Get-CimInstance Win32_Process -Filter "ProcessId=$targetId" -ErrorAction Stop
    if (([datetime]$revalidated.CreationDate).ToUniversalTime().ToString("o") -cne $selectedCreation -or [string]$revalidated.ExecutablePath -cne [string]$selected.ExecutablePath -or [string]$revalidated.CommandLine -cne [string]$selected.CommandLine) { throw "Listener changed identity before termination." }
    $processHandle = Get-Process -Id $targetId -ErrorAction Stop
    $null = $processHandle.Handle
    if ($processHandle.StartTime.ToUniversalTime().ToString("o") -cne $selectedCreation) { throw "Listener changed while acquiring stable process handle." }
    Stop-Process -InputObject $processHandle -Force -ErrorAction Stop
    Start-Sleep -Milliseconds 500
  }
  throw "Same-root runtime did not remain clear for two snapshots."
}

function Invoke-Installer {
  param([object]$Configuration, [switch]$WhatIf)
  $logRoot = Join-Path $Configuration.data_root "release-logs"; if (-not (Test-Path -LiteralPath $logRoot)) { New-Item -ItemType Directory -Path $logRoot -Force | Out-Null }
  Assert-Directory $logRoot
  $logStem = Join-Path $logRoot ("grouped-matching-" + [guid]::NewGuid().ToString("N"))
  $stdoutLog = "$logStem.stdout.log"
  $stderrLog = "$logStem.stderr.log"
  $arguments = @("-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", $Installer, "-ProjectRoot", $ProjectRoot, "-DataRoot", $Configuration.data_root)
  if ($WhatIf) { $arguments += "-WhatIf" }
  $process = Start-Process -FilePath (Get-SystemPowerShellPath) -ArgumentList $arguments -PassThru -WindowStyle Hidden -RedirectStandardOutput $stdoutLog -RedirectStandardError $stderrLog
  $listenerLeft = $false
  while (-not $process.HasExited) {
    $listenerCount = @(Get-NetTCPConnection -State Listen -LocalAddress 127.0.0.1 -LocalPort $Configuration.port -ErrorAction SilentlyContinue).Count
    if ($listenerCount -eq 0) { $listenerLeft = $true }
    if ($listenerCount -gt 1) {
      $process.Kill()
      throw "Installer created more than one loopback listener; diagnostics retained at $stdoutLog and $stderrLog."
    }
    Start-Sleep -Milliseconds 250
    $process.Refresh()
  }
  if ($process.ExitCode -ne 0) { throw "Installer failed; diagnostics retained at $stdoutLog and $stderrLog." }
  if (-not $WhatIf -and -not $listenerLeft) { throw "Installer never cleared the old listener; diagnostics retained at $stdoutLog and $stderrLog." }
}

function Wait-ForFinalState {
  param([object]$Configuration, [object]$Binding, [string]$BackupPath)
  $deadline = (Get-Date).AddSeconds(45); $lastFailure = ""
  while ((Get-Date) -lt $deadline) {
    try {
      $source = Get-SourceIdentity
      if ($source.commit -cne $Binding.merged_commit -or $source.tree -cne $Binding.merged_tree -or (Get-FileSha256 $EnvFile) -cne $Binding.env_sha256) { throw "Final source or env differs." }
      Assert-TaskBinding $Configuration $Binding
      $listener = Get-LoopbackListener $Configuration.port
      if ($listener.process_id -eq [int]$Binding.old_listener_pid -and $listener.creation_date -eq [string]$Binding.old_listener_creation_date) { throw "Listener instance was not replaced." }
      Assert-ListenerTaskOwnership $listener $Configuration $Binding
      Assert-ReadyHealth $Configuration $Binding $Binding.merged_commit $Binding.merged_tree
      Get-ReleasePaths $Binding | Out-Null
      Assert-VerifiedRollbackBackup $BackupPath $Configuration $Binding | Out-Null
      Assert-Leaf $PlayerAsset; if (-not (Get-Content -LiteralPath $PlayerAsset -Raw).Contains("selectedChoiceIds")) { throw "Grouped matching player marker absent." }
      return
    } catch { $lastFailure = $_.Exception.Message; Start-Sleep -Seconds 1 }
  }
  throw $lastFailure
}

function Invoke-UnverifiedOldRuntimeRecovery {
  # A missing/invalid backup cannot prove data or task recovery. This only makes
  # one best-effort attempt to get the old executable running, then reports failure.
  param([object]$Configuration, [object]$Binding)
  try {
    Invoke-Git @("checkout", "--detach", $Binding.old_commit) | Out-Null
    $source = Get-SourceIdentity
    if ($source.commit -cne $Binding.old_commit -or $source.tree -cne $Binding.old_tree) { throw "old checkout did not restore" }
    Invoke-Installer $Configuration
    return "old runtime recovery attempted without certifying data or task"
  } catch {
    return "old runtime recovery attempt failed: $($_.Exception.Message)"
  }
}

function Invoke-Rollback {
  param([object]$Configuration, [object]$Binding, [string]$BackupPath, [string]$Reason)
  $script:RollbackAttempted = $true
  $recoveryFailures = @()
  $rollbackCompletionMessage = $null
  try {
    $backup = Assert-VerifiedRollbackBackup $BackupPath $Configuration $Binding
    Stop-VerifiedRuntime $Configuration $Binding
    Invoke-Git @("checkout", "--detach", $Binding.old_commit) | Out-Null
    $source = Get-SourceIdentity; if ($source.commit -cne $Binding.old_commit -or $source.tree -cne $Binding.old_tree) { throw "Old checkout did not restore." }
    $quarantine = Join-Path $Configuration.data_root ("failed-release-quarantine\" + [guid]::NewGuid().ToString("N")); New-Item -ItemType Directory -Path $quarantine -ErrorAction Stop | Out-Null
    foreach ($runtimePath in @($Configuration.database_path, ($Configuration.database_path + "-wal"), ($Configuration.database_path + "-shm"), (Join-Path $Configuration.data_root "artifacts"))) { if (Test-Path -LiteralPath $runtimePath) { Assert-NonReparsePath $runtimePath; Move-Item -LiteralPath $runtimePath -Destination $quarantine -ErrorAction Stop } }
    Copy-Item -LiteralPath $backup.database -Destination $Configuration.database_path -ErrorAction Stop
    if (Test-Path -LiteralPath $backup.artifacts -PathType Container) { Copy-Item -LiteralPath $backup.artifacts -Destination (Join-Path $Configuration.data_root "artifacts") -Recurse -ErrorAction Stop }
    $python = Join-Path $ProjectRoot ".venv\Scripts\python.exe"; Assert-Leaf $python; & $python -c "import sqlite3,sys; assert sqlite3.connect(sys.argv[1]).execute('PRAGMA integrity_check').fetchone() == ('ok',)" $Configuration.database_path; Assert-NativeSuccess "SQLite integrity check"
    Invoke-Installer $Configuration
    Stop-VerifiedRuntime $Configuration $Binding
    Register-ScheduledTask -TaskName $TaskName -Xml (Get-Content -LiteralPath $backup.task_xml -Raw) -Force | Out-Null
    if ((Get-TaskXmlSha256) -cne [string]$Binding.task_xml_sha256) { throw "Old task XML did not restore." }
    Start-ScheduledTask -TaskName $TaskName
    $deadline = (Get-Date).AddSeconds(45); $restored = $false
    while ((Get-Date) -lt $deadline) { try { Assert-TaskBinding $Configuration $Binding; $listener = Get-LoopbackListener $Configuration.port; Assert-ListenerTaskOwnership $listener $Configuration $Binding; Assert-ReadyHealth $Configuration $Binding $Binding.old_commit $Binding.old_tree; $restored = $true; break } catch { Start-Sleep -Seconds 1 } }
    if (-not $restored) { throw "Old runtime did not restore." }
    $rollbackCompletionMessage = "release failed and rollback completed: $Reason"
  } catch {
    $recoveryFailures += $_.Exception.Message
    # Never rethrow inside this recovery try: the outer caller must see the explicit incomplete state.
  }
  if ($rollbackCompletionMessage) { throw $rollbackCompletionMessage }
  $unverifiedOutcome = Invoke-UnverifiedOldRuntimeRecovery $Configuration $Binding
  throw "rollback incomplete: release failure: $Reason; recovery failure: $($recoveryFailures -join '; '); $unverifiedOutcome"
}

function Get-Binding {
  if ([string]::IsNullOrWhiteSpace($BindingJsonBase64)) { throw "Binding is required." }
  try { $binding = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($BindingJsonBase64)) | ConvertFrom-Json } catch { throw "Binding JSON is invalid." }
  foreach ($field in @("old_commit", "old_tree", "schema_version", "old_listener_pid", "old_listener_creation_date", "process_identity", "task_xml_sha256", "task_principal", "task_logon_type", "deployment_identity", "data_root", "database_path", "port", "env_sha256", "merged_commit", "merged_tree")) { if ($null -eq $binding.$field -or [string]::IsNullOrWhiteSpace([string]$binding.$field)) { throw "Binding field missing: $field" } }
  if ([string]$binding.merged_commit -cne $ExpectedMergedCommit -or [string]$binding.merged_tree -cne $ExpectedMergedTree -or [int]$binding.port -ne 8765) { throw "Binding release identity differs." }
  return $binding
}

foreach ($hash in @($ExpectedScriptSha256, $ExpectedMergedCommit, $ExpectedMergedTree)) { if ($hash -notmatch "^[0-9a-f]{40,64}$") { throw "Expected hash format is invalid." } }
Assert-Leaf $PSCommandPath
if ((Get-FileSha256 $PSCommandPath) -cne $ExpectedScriptSha256) { throw "Release script self hash differs." }
$configuration = Get-ReleaseConfiguration

try {
  if ($Mode -eq "Preflight") {
    $source = Get-SourceIdentity; Invoke-Git @("fetch", "origin", "main") | Out-Null
    $originMain = ([string](Invoke-Git @("rev-parse", "origin/main"))[0]).Trim().ToLowerInvariant()
    if ($originMain -cne $ExpectedMergedCommit) { throw "origin/main does not equal expected merge." }
    Invoke-Git @("merge-base", "--is-ancestor", "HEAD", "origin/main") | Out-Null
    $task = Get-ScheduledTask -TaskName $TaskName -ErrorAction Stop; $identity = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
    if ([string]$task.Principal.UserId -cne $identity) { throw "Deployment identity does not equal task principal." }
    $listener = Get-LoopbackListener $configuration.port
    $binding = [ordered]@{ marker="OMS_GROUPED_MATCHING_PREFLIGHT_COMPLETE"; old_commit=$source.commit; old_tree=$source.tree; schema_version=[int](Invoke-RestMethod -Uri ("http://127.0.0.1:" + $configuration.port + "/health/ready") -TimeoutSec 3).schema_version; old_listener_pid=$listener.process_id; old_listener_creation_date=$listener.creation_date; process_identity=$listener.owner; task_xml_sha256=(Get-TaskXmlSha256); task_principal=[string]$task.Principal.UserId; task_logon_type=[string]$task.Principal.LogonType; deployment_identity=$identity; data_root=$configuration.data_root; database_url=$configuration.database_url; database_path=$configuration.database_path; port=$configuration.port; env_sha256=$configuration.env_sha256; merged_commit=$ExpectedMergedCommit; merged_tree=$ExpectedMergedTree }
    if ($binding.port -ne 8765) { throw "Configured port is not 8765." }
    Assert-TaskBinding $configuration $binding; Assert-ListenerTaskOwnership $listener $configuration $binding; Assert-ReadyHealth $configuration $binding $binding.old_commit $binding.old_tree; $binding.release_paths = Get-ReleasePaths $binding
    $binding | ConvertTo-Json -Compress -Depth 8
    exit 0
  }

  $binding = Get-Binding
  if ([string]$binding.deployment_identity -cne [System.Security.Principal.WindowsIdentity]::GetCurrent().Name -or [string]$configuration.data_root -cne [string]$binding.data_root -or [string]$configuration.database_path -cne [string]$binding.database_path -or $configuration.port -ne [int]$binding.port -or $configuration.env_sha256 -cne [string]$binding.env_sha256) { throw "Runtime changed since preflight." }
  if ($Mode -eq "Deploy") {
    $backupRoot = Join-Path $configuration.data_root "backups"; $backupsBefore = Get-BackupNames $backupRoot; $backupPath = ""
    try {
      $source = Get-SourceIdentity; if ($source.commit -cne $binding.old_commit -or $source.tree -cne $binding.old_tree) { throw "Old source changed." }
      Assert-TaskBinding $configuration $binding; $listener = Get-LoopbackListener $configuration.port
      if ($listener.process_id -ne [int]$binding.old_listener_pid -or $listener.creation_date -cne [string]$binding.old_listener_creation_date) { throw "Old listener changed." }
      Assert-ListenerTaskOwnership $listener $configuration $binding; Assert-ReadyHealth $configuration $binding $binding.old_commit $binding.old_tree; Get-ReleasePaths $binding | Out-Null
      Invoke-Installer $configuration -WhatIf
      $script:CheckoutMutated = $true
      Invoke-Git @("merge", "--ff-only", "origin/main") | Out-Null
      $source = Get-SourceIdentity; if ($source.commit -cne $binding.merged_commit -or $source.tree -cne $binding.merged_tree) { throw "Merged source differs." }
      $script:InstallerStarted = $true; Invoke-Installer $configuration
      $newBackups = @((Get-BackupNames $backupRoot) | Where-Object { $backupsBefore -notcontains $_ }); if ($newBackups.Count -ne 1) { throw "Expected exactly one new backup." }; $backupPath = Join-Path $backupRoot $newBackups[0]
      Assert-VerifiedRollbackBackup $backupPath $configuration $binding | Out-Null
      Wait-ForFinalState $configuration $binding $backupPath
      [ordered]@{ marker="OMS_GROUPED_MATCHING_DEPLOY_COMPLETE"; commit=$binding.merged_commit; tree=$binding.merged_tree; new_backup=$backupPath; env_sha256=$binding.env_sha256 } | ConvertTo-Json -Compress
      exit 0
    } catch {
      $failure = $_.Exception.Message
      if ($script:CheckoutMutated -and -not $script:RollbackAttempted) {
        if (-not $backupPath) { $newBackups = @((Get-BackupNames $backupRoot) | Where-Object { $backupsBefore -notcontains $_ }); if ($newBackups.Count -eq 1) { $backupPath = Join-Path $backupRoot $newBackups[0] } }
        if ($backupPath) { Invoke-Rollback $configuration $binding $backupPath $failure }
        if (-not $script:InstallerStarted) { Invoke-Git @("checkout", "--detach", $binding.old_commit) | Out-Null; $restored = Get-SourceIdentity; if ($restored.commit -ne $binding.old_commit -or $restored.tree -ne $binding.old_tree) { throw "rollback incomplete: checkout mutation occurred before installer and old checkout did not restore." }; throw "release failed before installer; checkout restored and runtime was untouched: $failure" }
        $unverifiedOutcome = Invoke-UnverifiedOldRuntimeRecovery $configuration $binding
        throw "rollback incomplete: installer began without a complete verified backup; $unverifiedOutcome: $failure"
      }
      throw $failure
    }
  }
  if ([string]::IsNullOrWhiteSpace($ExpectedBackupPath)) { throw "Postflight requires backup path." }
  Wait-ForFinalState $configuration $binding $ExpectedBackupPath
  [ordered]@{ marker="OMS_GROUPED_MATCHING_POSTFLIGHT_COMPLETE"; commit=$binding.merged_commit; tree=$binding.merged_tree; backup=$ExpectedBackupPath; env_sha256=$binding.env_sha256 } | ConvertTo-Json -Compress
} catch {
  [Console]::Error.WriteLine($_.Exception.Message)
  exit 1
}
