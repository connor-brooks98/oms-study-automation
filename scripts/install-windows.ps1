[CmdletBinding(SupportsShouldProcess)]
param(
  [string]$ProjectRoot = "C:\Services\oms-study-automation-v2",
  [string]$DataRoot = "C:\ProgramData\OMSStudyHub"
)

$ErrorActionPreference = "Stop"
$TaskName = "OMS Study Hub V2"
$Timestamp = Get-Date -Format "yyyyMMdd-HHmmss"
$TaskIdentity = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name

if (-not (Test-Path $ProjectRoot)) {
  throw "Project directory not found: $ProjectRoot"
}
$ProjectRoot = (Resolve-Path -LiteralPath $ProjectRoot).Path
$StartScript = Join-Path $ProjectRoot "scripts\start-hub.ps1"
if (-not (Test-Path -LiteralPath $StartScript)) {
  throw "Study Hub start script not found: $StartScript"
}
$EnvFile = Join-Path $ProjectRoot ".env"
$BackupScript = Join-Path $ProjectRoot "scripts\backup-sqlite.py"
if (-not (Test-Path -LiteralPath $BackupScript)) {
  throw "SQLite backup helper not found: $BackupScript"
}

function Get-DotEnvValue {
  param(
    [string]$Name,
    [string]$Path
  )
  if (-not (Test-Path -LiteralPath $Path)) { return $null }
  $EscapedName = [regex]::Escape($Name)
  $Line = Get-Content -LiteralPath $Path | Where-Object {
    $_ -match "^\s*(?:export\s+)?${EscapedName}\s*="
  } | Select-Object -Last 1
  if (-not $Line) { return $null }
  $Value = (
    [string]$Line -replace "^\s*(?:export\s+)?${EscapedName}\s*=\s*", ""
  ).Trim()
  if ($Value.StartsWith('"') -or $Value.StartsWith("'")) {
    $Quote = [string]$Value[0]
    $ClosingIndex = $Value.IndexOf($Quote, 1)
    if ($ClosingIndex -lt 1) {
      throw "The $Name assignment in $Path has an unterminated quoted value."
    }
    $Trailing = $Value.Substring($ClosingIndex + 1).Trim()
    if ($Trailing -and -not $Trailing.StartsWith("#")) {
      throw "The $Name assignment in $Path has unsupported trailing content."
    }
    return $Value.Substring(1, $ClosingIndex - 1)
  }
  return ([regex]::Replace($Value, "\s+#.*$", "")).Trim()
}

function Get-EffectiveSetting {
  param(
    [string]$Name,
    [string]$Path,
    [string]$DefaultValue
  )
  $ProcessValue = [Environment]::GetEnvironmentVariable($Name, "Process")
  if ($null -ne $ProcessValue) { return $ProcessValue }
  $FileValue = Get-DotEnvValue -Name $Name -Path $Path
  if ($null -ne $FileValue) { return $FileValue }
  return $DefaultValue
}

function Resolve-SqliteDatabasePath {
  param(
    [string]$DatabaseUrl,
    [string]$ExpectedProjectRoot
  )
  if (-not $DatabaseUrl.StartsWith("sqlite:///", [System.StringComparison]::OrdinalIgnoreCase)) {
    throw "Windows rollback backup currently requires a sqlite:/// OMS_HUB_DATABASE_URL."
  }
  $RawPath = $DatabaseUrl.Substring("sqlite:///".Length).Replace("/", "\")
  if ($RawPath -eq ":memory:") {
    throw "The Windows installer cannot back up an in-memory SQLite database."
  }
  if (-not [System.IO.Path]::IsPathRooted($RawPath)) {
    $RawPath = Join-Path $ExpectedProjectRoot $RawPath
  }
  return [System.IO.Path]::GetFullPath($RawPath)
}

function Assert-NativeCommandSucceeded {
  param([string]$Operation)
  if ($LASTEXITCODE -ne 0) {
    throw "$Operation failed with native exit code $LASTEXITCODE."
  }
}

$EffectiveDataRootValue = Get-EffectiveSetting `
  -Name "OMS_HUB_DATA_DIR" `
  -Path $EnvFile `
  -DefaultValue $DataRoot
$EffectiveDataRoot = [System.IO.Path]::GetFullPath($EffectiveDataRootValue)
# Match Settings.database_url exactly when OMS_HUB_DATABASE_URL is absent.
# OMS_HUB_DATA_DIR does not implicitly relocate the runtime database.
$DefaultDatabaseUrl = "sqlite:///C:/ProgramData/OMSStudyHub/hub.db"
$EffectiveDatabaseUrl = Get-EffectiveSetting `
  -Name "OMS_HUB_DATABASE_URL" `
  -Path $EnvFile `
  -DefaultValue $DefaultDatabaseUrl
$DatabasePath = Resolve-SqliteDatabasePath `
  -DatabaseUrl $EffectiveDatabaseUrl `
  -ExpectedProjectRoot $ProjectRoot
$BackupRoot = Join-Path $EffectiveDataRoot "backups"
$BackupPath = Join-Path $BackupRoot $Timestamp

function Assert-TaskActionTargetsProjectRoot {
  param(
    [string]$Name,
    [string]$ExpectedProjectRoot,
    [string]$ExpectedStartScript
  )
  $Task = Get-ScheduledTask -TaskName $Name -ErrorAction Stop
  $Actions = @($Task.Actions)
  if ($Actions.Count -ne 1) {
    throw "Scheduled task $Name must have exactly one action; found $($Actions.Count)."
  }
  $Action = $Actions[0]
  $ScriptMatches = $Action.Arguments -match [regex]::Escape($ExpectedStartScript)
  $WorkingDirectory = ([string]$Action.WorkingDirectory).TrimEnd("\\")
  $ExpectedDirectory = $ExpectedProjectRoot.TrimEnd("\\")
  if (-not $ScriptMatches -or $WorkingDirectory -ne $ExpectedDirectory) {
    throw "Scheduled task $Name does not target $ExpectedProjectRoot. Re-run the installer with the intended -ProjectRoot."
  }
}

function Test-ProcessPathUnderRoot {
  param(
    [string]$ExecutablePath,
    [string]$ExpectedProjectRoot
  )
  if ([string]::IsNullOrWhiteSpace($ExecutablePath)) { return $false }
  $ExpectedPrefix = $ExpectedProjectRoot.TrimEnd("\\") + "\\"
  return $ExecutablePath.StartsWith(
    $ExpectedPrefix,
    [System.StringComparison]::OrdinalIgnoreCase
  )
}

function Get-ConflictingHubProcesses {
  param([string]$ExpectedProjectRoot)
  $Processes = @(Get-CimInstance Win32_Process)
  $ChildrenByParent = @{}
  foreach ($Process in $Processes) {
    $ParentKey = [string]$Process.ParentProcessId
    if (-not $ChildrenByParent.ContainsKey($ParentKey)) {
      $ChildrenByParent[$ParentKey] = [System.Collections.Generic.List[object]]::new()
    }
    $ChildrenByParent[$ParentKey].Add($Process)
  }

  $Selected = [System.Collections.Generic.List[object]]::new()
  $Seen = [System.Collections.Generic.HashSet[int]]::new()
  $Pending = [System.Collections.Generic.Queue[object]]::new()
  foreach ($Process in $Processes) {
    $IsHubLauncher = [string]$Process.Name -ieq "oms-hub.exe"
    $IsExpectedRoot = Test-ProcessPathUnderRoot `
      -ExecutablePath ([string]$Process.ExecutablePath) `
      -ExpectedProjectRoot $ExpectedProjectRoot
    if ($IsHubLauncher -and $IsExpectedRoot) {
      $Pending.Enqueue($Process)
    }
  }

  while ($Pending.Count -gt 0) {
    $Process = $Pending.Dequeue()
    $ProcessId = [int]$Process.ProcessId
    if (-not $Seen.Add($ProcessId)) { continue }
    # The same-root launcher positively identifies the process tree. Every
    # descendant belongs to that tree and must stop before editable install,
    # including helpers whose executable name is neither Hub nor Python.
    $Selected.Add($Process)
    $ChildKey = [string]$ProcessId
    if ($ChildrenByParent.ContainsKey($ChildKey)) {
      foreach ($Child in $ChildrenByParent[$ChildKey]) {
        $Pending.Enqueue($Child)
      }
    }
  }

  foreach ($Process in $Processes) {
    $IsPython = [string]$Process.Name -ieq "python.exe"
    $IsExpectedRoot = Test-ProcessPathUnderRoot `
      -ExecutablePath ([string]$Process.ExecutablePath) `
      -ExpectedProjectRoot $ExpectedProjectRoot
    $HasHubCommandLine = ([string]$Process.CommandLine) -match "(?i)oms[-_]hub"
    # If the wrapper already exited, a same-root orphaned Python child is safe
    # to stop only when its command line also positively identifies OMS Hub.
    if ($IsPython -and $IsExpectedRoot -and $HasHubCommandLine -and $Seen.Add([int]$Process.ProcessId)) {
      $Selected.Add($Process)
    }
  }
  return @($Selected)
}

function Get-DashboardPort {
  param([string]$ExpectedProjectRoot)
  $EnvFile = Join-Path $ExpectedProjectRoot ".env"
  $Configured = Get-EffectiveSetting `
    -Name "OMS_HUB_DASHBOARD_PORT" `
    -Path $EnvFile `
    -DefaultValue "8787"
  $Port = 0
  if ([int]::TryParse($Configured, [ref]$Port) -and $Port -ge 1024 -and $Port -le 65535) {
    return $Port
  }
  throw "OMS_HUB_DASHBOARD_PORT must be between 1024 and 65535."
}

function Get-ProjectBuildRevision {
  param([string]$ExpectedProjectRoot)
  $Git = Get-Command git.exe -ErrorAction SilentlyContinue
  if (-not $Git) { $Git = Get-Command git -ErrorAction SilentlyContinue }
  if (-not $Git) { throw "Git is required to establish exact build provenance." }
  $SourceStatus = @(
    & $Git.Source -C $ExpectedProjectRoot status --porcelain=v1 --untracked-files=all -- src scripts pyproject.toml 2>$null
  )
  if ($LASTEXITCODE -ne 0) {
    throw "The project source cleanliness could not be verified."
  }
  if ($SourceStatus.Count -gt 0) {
    throw "The editable Study Hub runtime differs from HEAD; installation is blocked."
  }
  $Revision = @(& $Git.Source -C $ExpectedProjectRoot rev-parse HEAD 2>$null)
  if ($LASTEXITCODE -ne 0 -or -not $Revision) {
    throw "The project build revision could not be resolved."
  }
  $FullRevision = ([string]$Revision[0]).Trim()
  if ($FullRevision -notmatch "^[0-9a-fA-F]{40}$") {
    throw "The project build revision is not a full commit SHA."
  }
  return $FullRevision.ToLowerInvariant()
}

function Get-ProjectBuildTree {
  param([string]$ExpectedProjectRoot)
  $Git = Get-Command git.exe -ErrorAction SilentlyContinue
  if (-not $Git) { $Git = Get-Command git -ErrorAction SilentlyContinue }
  if (-not $Git) { throw "Git is required to establish exact build provenance." }
  $Tree = @(& $Git.Source -C $ExpectedProjectRoot rev-parse "HEAD^{tree}" 2>$null)
  if ($LASTEXITCODE -ne 0 -or -not $Tree) {
    throw "The project build tree could not be resolved."
  }
  $FullTree = ([string]$Tree[0]).Trim()
  if ($FullTree -notmatch "^[0-9a-fA-F]{40}$") {
    throw "The project build tree is not a full tree SHA."
  }
  return $FullTree.ToLowerInvariant()
}

function Assert-ProjectBuildIdentity {
  param(
    [string]$ExpectedProjectRoot,
    [string]$ExpectedBuildRevision,
    [string]$ExpectedBuildTree
  )
  $ActualBuildRevision = Get-ProjectBuildRevision -ExpectedProjectRoot $ExpectedProjectRoot
  $ActualBuildTree = Get-ProjectBuildTree -ExpectedProjectRoot $ExpectedProjectRoot
  if (
    $ActualBuildRevision -ne $ExpectedBuildRevision -or
    $ActualBuildTree -ne $ExpectedBuildTree
  ) {
    throw "The project checkout changed after installer preflight; installation is blocked."
  }
}

function Assert-StartedHubProvenance {
  param(
    [string]$ExpectedProjectRoot,
    [string]$ExpectedBuildRevision,
    [string]$ExpectedBuildTree
  )
  $Port = Get-DashboardPort -ExpectedProjectRoot $ExpectedProjectRoot
  $Deadline = (Get-Date).AddSeconds(30)
  $LastFailure = "no local health response"
  while ((Get-Date) -lt $Deadline) {
    try {
      $Health = Invoke-RestMethod -Uri "http://127.0.0.1:$Port/health/ready" -TimeoutSec 3
      $RootMatches = ([string]$Health.deployment_root).TrimEnd("\\") -eq $ExpectedProjectRoot.TrimEnd("\\")
      $BuildMatches = [string]$Health.build_revision -eq $ExpectedBuildRevision
      $TreeMatches = [string]$Health.build_tree -eq $ExpectedBuildTree
      $ExpectedWorkers = @("generation_worker", "ingestion_worker", "studio_worker")
      $WorkerNames = @($Health.workers.PSObject.Properties.Name | Sort-Object)
      $WorkerNamesMatch = ($WorkerNames -join ",") -eq (($ExpectedWorkers | Sort-Object) -join ",")
      $WorkersHealthy = $WorkerNamesMatch
      foreach ($WorkerName in $ExpectedWorkers) {
        $Worker = $Health.workers.$WorkerName
        if (-not $Worker.alive -or [int]$Worker.start_count -ne 1) {
          $WorkersHealthy = $false
        }
      }
      if (
        $Health.status -eq "ok" -and
        $RootMatches -and
        $BuildMatches -and
        $TreeMatches -and
        $Health.database_reachable -eq $true -and
        [int]$Health.schema_version -gt 0 -and
        $WorkersHealthy
      ) { return }
      $LastFailure = "health reported root '$($Health.deployment_root)', build '$($Health.build_revision)', and tree '$($Health.build_tree)'"
    } catch {
      $LastFailure = $_.Exception.Message
    }
    Start-Sleep -Seconds 1
  }
  throw "Study Hub did not start from the expected root/build on port ${Port}: $LastFailure"
}

function Stop-ConflictingHubProcesses {
  param([string]$ExpectedProjectRoot)
  $Conflicts = @(Get-ConflictingHubProcesses -ExpectedProjectRoot $ExpectedProjectRoot)
  foreach ($Process in $Conflicts) {
    $LiveProcess = Get-Process -Id $Process.ProcessId -ErrorAction SilentlyContinue
    if ($LiveProcess) {
      Write-Host "Stopping same-root Study Hub process $($Process.ProcessId) from $($Process.ExecutablePath)."
      Stop-Process -Id $Process.ProcessId -Force -ErrorAction Stop
    }
  }
  if ($Conflicts.Count -gt 0) {
    Start-Sleep -Seconds 1
  }
  $Remaining = @(Get-ConflictingHubProcesses -ExpectedProjectRoot $ExpectedProjectRoot)
  if ($Remaining.Count -gt 0) {
    $Details = $Remaining | ForEach-Object { "$($_.ProcessId): $($_.ExecutablePath)" }
    throw "A same-root Study Hub process under $ExpectedProjectRoot is still running: $($Details -join '; '). Stop it before installation."
  }
}

# Establish clean, exact source provenance before stopping a task, process, or
# writing a rollback backup. Re-check immediately before startup below.
$PreflightBuildRevision = Get-ProjectBuildRevision -ExpectedProjectRoot $ProjectRoot
$PreflightBuildTree = Get-ProjectBuildTree -ExpectedProjectRoot $ProjectRoot
Write-Host "Verified install source $PreflightBuildRevision (tree $PreflightBuildTree)."

$ExistingTask = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
$DatabasePresentAtPreflight = Test-Path -LiteralPath $DatabasePath
if ($ExistingTask -and -not $DatabasePresentAtPreflight) {
  throw "The effective runtime database does not exist at $DatabasePath; refusing to certify an incomplete rollback backup."
}
if ($ExistingTask -and $PSCmdlet.ShouldProcess($TaskName, "Stop scheduled task")) {
  Assert-ProjectBuildIdentity `
    -ExpectedProjectRoot $ProjectRoot `
    -ExpectedBuildRevision $PreflightBuildRevision `
    -ExpectedBuildTree $PreflightBuildTree
  Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
}

$PyLauncher = Get-Command py -ErrorAction SilentlyContinue
$PythonCommand = if ($PyLauncher) {
  $PyLauncher.Source
} else {
  (Get-Command python -ErrorAction Stop).Source
}
$PythonPrefix = if ($PyLauncher) { @("-3.12") } else { @() }
$PythonVersionArgs = $PythonPrefix + @("--version")
& $PythonCommand @PythonVersionArgs
if ($LASTEXITCODE -ne 0) { throw "Python 3.12 is required" }

# The scheduled-task command can return before its launcher and child exit.
# Stop and verify only the exact process tree rooted in this deployment.
Assert-ProjectBuildIdentity `
  -ExpectedProjectRoot $ProjectRoot `
  -ExpectedBuildRevision $PreflightBuildRevision `
  -ExpectedBuildTree $PreflightBuildTree
Stop-ConflictingHubProcesses -ExpectedProjectRoot $ProjectRoot

$BackupComplete = $false
$DatabaseBackedUp = $false
if ($PSCmdlet.ShouldProcess($BackupPath, "Create verified rollback backup")) {
  Assert-ProjectBuildIdentity `
    -ExpectedProjectRoot $ProjectRoot `
    -ExpectedBuildRevision $PreflightBuildRevision `
    -ExpectedBuildTree $PreflightBuildTree
  if (Test-Path -LiteralPath $BackupPath) {
    throw "Rollback backup path already exists: $BackupPath"
  }
  New-Item -ItemType Directory -Path $BackupPath | Out-Null
  $ArtifactPath = Join-Path $EffectiveDataRoot "artifacts"
  $BackupDatabasePath = Join-Path $BackupPath "hub.db"
  if ($DatabasePresentAtPreflight -and -not (Test-Path -LiteralPath $DatabasePath)) {
    throw "The effective runtime database disappeared before backup: $DatabasePath"
  }
  if (Test-Path -LiteralPath $DatabasePath) {
    $BackupArguments = $PythonPrefix + @(
      $BackupScript,
      "--source", $DatabasePath,
      "--destination", $BackupDatabasePath
    )
    & $PythonCommand @BackupArguments
    if ($LASTEXITCODE -ne 0) {
      throw "SQLite online backup or integrity verification failed."
    }
    $DatabaseBackedUp = $true
  }

  if (Test-Path -LiteralPath $ArtifactPath) {
    $ArtifactBackupPath = Join-Path $BackupPath "artifacts"
    Copy-Item -LiteralPath $ArtifactPath -Destination $ArtifactBackupPath -Recurse -Force
    $ArtifactPrefix = $ArtifactPath.TrimEnd("\") + "\"
    foreach ($SourceFile in Get-ChildItem -LiteralPath $ArtifactPath -File -Recurse -Force) {
      $RelativeArtifactPath = $SourceFile.FullName.Substring($ArtifactPrefix.Length)
      $CopiedFile = Join-Path $ArtifactBackupPath $RelativeArtifactPath
      if (-not (Test-Path -LiteralPath $CopiedFile)) {
        throw "Artifact backup is missing $RelativeArtifactPath."
      }
      $SourceHash = (Get-FileHash -LiteralPath $SourceFile.FullName -Algorithm SHA256).Hash
      $CopiedHash = (Get-FileHash -LiteralPath $CopiedFile -Algorithm SHA256).Hash
      if ($SourceHash -ne $CopiedHash) {
        throw "Artifact backup checksum mismatch for $RelativeArtifactPath."
      }
    }
  }

  $ConfigMetadataPath = Join-Path $BackupPath "effective-config.json"
  $Utf8NoBom = New-Object System.Text.UTF8Encoding($false)
  $ConfigMetadata = [ordered]@{
    build_revision = $PreflightBuildRevision
    build_tree = $PreflightBuildTree
    database_path = $DatabasePath
    database_url = $EffectiveDatabaseUrl
    data_root = $EffectiveDataRoot
    project_root = $ProjectRoot
  } | ConvertTo-Json -Depth 4
  [System.IO.File]::WriteAllText($ConfigMetadataPath, $ConfigMetadata + "`n", $Utf8NoBom)

  $BackupFiles = @(
    Get-ChildItem -LiteralPath $BackupPath -File -Recurse -Force | ForEach-Object {
      $RelativePath = $_.FullName.Substring($BackupPath.TrimEnd("\").Length).TrimStart("\")
      [ordered]@{
        path = $RelativePath.Replace("\", "/")
        sha256 = (Get-FileHash -LiteralPath $_.FullName -Algorithm SHA256).Hash.ToLowerInvariant()
        size = $_.Length
      }
    }
  )
  $ManifestPath = Join-Path $BackupPath "backup-manifest.json"
  $ManifestPartial = Join-Path $BackupPath ".backup-manifest.json.partial"
  $Manifest = [ordered]@{
    created_at = (Get-Date).ToUniversalTime().ToString("o")
    database = [ordered]@{
      backed_up = $DatabaseBackedUp
      backup_path = $(if ($DatabaseBackedUp) { "hub.db" } else { $null })
      source_path = $DatabasePath
      source_url = $EffectiveDatabaseUrl
    }
    database_path = $DatabasePath
    files = $BackupFiles
    project_root = $ProjectRoot
    schema_version = 1
  } | ConvertTo-Json -Depth 8
  [System.IO.File]::WriteAllText($ManifestPartial, $Manifest + "`n", $Utf8NoBom)
  Move-Item -LiteralPath $ManifestPartial -Destination $ManifestPath
  $ManifestHash = (
    Get-FileHash -LiteralPath $ManifestPath -Algorithm SHA256
  ).Hash.ToLowerInvariant()
  $ManifestChecksumPath = "$ManifestPath.sha256"
  $ChecksumPartial = Join-Path $BackupPath ".backup-manifest.json.sha256.partial"
  [System.IO.File]::WriteAllText(
    $ChecksumPartial,
    "$ManifestHash  backup-manifest.json`n",
    $Utf8NoBom
  )
  Move-Item -LiteralPath $ChecksumPartial -Destination $ManifestChecksumPath

  # This atomic rename is the final transaction marker. Its presence means
  # database integrity, artifact comparisons, and manifest checksum all passed.
  $CompletePath = Join-Path $BackupPath "backup-complete.json"
  $CompletePartial = Join-Path $BackupPath ".backup-complete.json.partial"
  $CompleteRecord = [ordered]@{
    database_backed_up = $DatabaseBackedUp
    database_path = $DatabasePath
    manifest = "backup-manifest.json"
    manifest_sha256 = $ManifestHash
    status = "complete"
  } | ConvertTo-Json -Compress
  [System.IO.File]::WriteAllText($CompletePartial, $CompleteRecord + "`n", $Utf8NoBom)
  Move-Item -LiteralPath $CompletePartial -Destination $CompletePath
  $BackupComplete = $true
}
if (-not $BackupComplete -and -not $WhatIfPreference) {
  throw "Verified rollback backup was not completed; installation is blocked."
}

if ($PSCmdlet.ShouldProcess($ProjectRoot, "Install Study Hub V2")) {
  Assert-ProjectBuildIdentity `
    -ExpectedProjectRoot $ProjectRoot `
    -ExpectedBuildRevision $PreflightBuildRevision `
    -ExpectedBuildTree $PreflightBuildTree
  if (-not (Test-Path "$ProjectRoot\.venv\Scripts\python.exe")) {
    $PythonVenvArgs = $PythonPrefix + @("-m", "venv", "$ProjectRoot\.venv")
    & $PythonCommand @PythonVenvArgs
    Assert-NativeCommandSucceeded -Operation "Python virtual environment creation"
  }
  & "$ProjectRoot\.venv\Scripts\python.exe" -m pip install --upgrade pip
  Assert-NativeCommandSucceeded -Operation "pip upgrade"
  $EditableInstallTarget = "${ProjectRoot}[document-processing]"
  & "$ProjectRoot\.venv\Scripts\python.exe" -m pip install -e $EditableInstallTarget
  Assert-NativeCommandSucceeded -Operation "editable Study Hub installation"
  & "$ProjectRoot\.venv\Scripts\python.exe" -c "import anydoc"
  Assert-NativeCommandSucceeded -Operation "Anydoc document parser import check"
  New-Item -ItemType Directory -Force -Path $EffectiveDataRoot, $BackupRoot | Out-Null
  & icacls.exe $EffectiveDataRoot /grant "${TaskIdentity}:(OI)(CI)M" /T /C | Out-Null
  if ($LASTEXITCODE -ne 0) {
    throw "Failed to grant $TaskIdentity modify access to $EffectiveDataRoot"
  }
  if (-not (Test-Path "$ProjectRoot\.env")) {
    Copy-Item "$ProjectRoot\.env.example" "$ProjectRoot\.env"
    throw "Configure $ProjectRoot\.env, then run this installer again"
  }
  & "$ProjectRoot\.venv\Scripts\oms-hub.exe" validate-config
  Assert-NativeCommandSucceeded -Operation "Study Hub configuration validation"
}

if ($PSCmdlet.ShouldProcess($TaskName, "Install scheduled startup")) {
  Assert-ProjectBuildIdentity `
    -ExpectedProjectRoot $ProjectRoot `
    -ExpectedBuildRevision $PreflightBuildRevision `
    -ExpectedBuildTree $PreflightBuildTree
  $PowerShell = (Get-Command powershell.exe).Source
  $Action = New-ScheduledTaskAction `
    -Execute $PowerShell `
    -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$StartScript`"" `
    -WorkingDirectory $ProjectRoot
  $Trigger = New-ScheduledTaskTrigger -AtLogOn -User $TaskIdentity
  $TaskSettings = New-ScheduledTaskSettingsSet `
    -RestartCount 3 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -StartWhenAvailable
  $Principal = New-ScheduledTaskPrincipal `
    -UserId $TaskIdentity `
    -LogonType Interactive
  Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $Action `
    -Trigger $Trigger `
    -Settings $TaskSettings `
    -Principal $Principal `
    -Force | Out-Null
  Assert-TaskActionTargetsProjectRoot `
    -Name $TaskName `
    -ExpectedProjectRoot $ProjectRoot `
    -ExpectedStartScript $StartScript
  # Re-assert the same-root stop invariant immediately before task startup.
  # Never terminate generic Python processes from another deployment.
  Assert-ProjectBuildIdentity `
    -ExpectedProjectRoot $ProjectRoot `
    -ExpectedBuildRevision $PreflightBuildRevision `
    -ExpectedBuildTree $PreflightBuildTree
  Stop-ConflictingHubProcesses -ExpectedProjectRoot $ProjectRoot
  Assert-ProjectBuildIdentity `
    -ExpectedProjectRoot $ProjectRoot `
    -ExpectedBuildRevision $PreflightBuildRevision `
    -ExpectedBuildTree $PreflightBuildTree
  Start-ScheduledTask -TaskName $TaskName
  Assert-StartedHubProvenance `
    -ExpectedProjectRoot $ProjectRoot `
    -ExpectedBuildRevision $PreflightBuildRevision `
    -ExpectedBuildTree $PreflightBuildTree
}

Write-Host "Study Hub V2 install complete."
Write-Host "Scheduled task root: $ProjectRoot"
Write-Host "Local dashboard: use OMS_HUB_DASHBOARD_PORT from .env (V4 example: http://127.0.0.1:8787)"
Write-Host "Rollback backup: $BackupPath"
