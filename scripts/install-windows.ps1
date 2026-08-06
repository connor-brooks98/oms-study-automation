[CmdletBinding(SupportsShouldProcess)]
param(
  [string]$ProjectRoot = "C:\Services\oms-study-automation-v2",
  [string]$DataRoot = "C:\ProgramData\OMSStudyHub"
)

$ErrorActionPreference = "Stop"
$TaskName = "OMS Study Hub V2"
$BackupRoot = Join-Path $DataRoot "backups"
$Timestamp = Get-Date -Format "yyyyMMdd-HHmmss"
$BackupPath = Join-Path $BackupRoot $Timestamp
$TaskIdentity = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name

if (-not (Test-Path $ProjectRoot)) {
  throw "Project directory not found: $ProjectRoot"
}
$ProjectRoot = (Resolve-Path -LiteralPath $ProjectRoot).Path
$StartScript = Join-Path $ProjectRoot "scripts\start-hub.ps1"
if (-not (Test-Path -LiteralPath $StartScript)) {
  throw "Study Hub start script not found: $StartScript"
}

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
    if ($IsHubLauncher -and -not $IsExpectedRoot) {
      $Pending.Enqueue($Process)
    }
  }

  while ($Pending.Count -gt 0) {
    $Process = $Pending.Dequeue()
    $ProcessId = [int]$Process.ProcessId
    if (-not $Seen.Add($ProcessId)) { continue }
    # The console launcher starts the actual app as a Python child.  Traverse
    # only this identified old-root tree, and select only Hub/Python nodes.
    if ([string]$Process.Name -ieq "oms-hub.exe" -or [string]$Process.Name -ieq "python.exe") {
      $Selected.Add($Process)
    }
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
    # If the wrapper already exited, an orphaned old virtualenv Python child is
    # still safe to stop only when its command line positively identifies OMS Hub.
    if ($IsPython -and -not $IsExpectedRoot -and $HasHubCommandLine -and $Seen.Add([int]$Process.ProcessId)) {
      $Selected.Add($Process)
    }
  }
  return @($Selected)
}

function Get-DashboardPort {
  param([string]$ExpectedProjectRoot)
  $EnvFile = Join-Path $ExpectedProjectRoot ".env"
  if (Test-Path -LiteralPath $EnvFile) {
    $PortLine = Get-Content -LiteralPath $EnvFile | Where-Object {
      $_ -match "^\s*OMS_HUB_DASHBOARD_PORT\s*=\s*\d+\s*$"
    } | Select-Object -First 1
    if ($PortLine -match "=\s*(\d+)\s*$") {
      $Port = [int]$Matches[1]
      if ($Port -ge 1024 -and $Port -le 65535) { return $Port }
    }
  }
  return 8787
}

function Get-ProjectBuildRevision {
  param([string]$ExpectedProjectRoot)
  $Git = Get-Command git.exe -ErrorAction SilentlyContinue
  if (-not $Git) { $Git = Get-Command git -ErrorAction SilentlyContinue }
  if (-not $Git) { return "unreported" }
  $Revision = & $Git.Source -C $ExpectedProjectRoot rev-parse HEAD 2>$null
  if ($LASTEXITCODE -ne 0 -or -not $Revision) { return "unreported" }
  return ([string]$Revision[0]).Trim()
}

function Assert-StartedHubProvenance {
  param(
    [string]$ExpectedProjectRoot,
    [string]$ExpectedBuildRevision
  )
  $Port = Get-DashboardPort -ExpectedProjectRoot $ExpectedProjectRoot
  $Deadline = (Get-Date).AddSeconds(30)
  $LastFailure = "no local health response"
  while ((Get-Date) -lt $Deadline) {
    try {
      $Health = Invoke-RestMethod -Uri "http://127.0.0.1:$Port/health" -TimeoutSec 3
      $RootMatches = ([string]$Health.deployment_root).TrimEnd("\\") -eq $ExpectedProjectRoot.TrimEnd("\\")
      $BuildMatches = [string]$Health.build_revision -eq $ExpectedBuildRevision
      if ($Health.status -eq "ok" -and $RootMatches -and $BuildMatches) { return }
      $LastFailure = "health reported root '$($Health.deployment_root)' and build '$($Health.build_revision)'"
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
      Write-Host "Stopping stale Study Hub process $($Process.ProcessId) from $($Process.ExecutablePath)."
      Stop-Process -Id $Process.ProcessId -Force -ErrorAction Stop
    }
  }
  if ($Conflicts.Count -gt 0) {
    Start-Sleep -Seconds 1
  }
  $Remaining = @(Get-ConflictingHubProcesses -ExpectedProjectRoot $ExpectedProjectRoot)
  if ($Remaining.Count -gt 0) {
    $Details = $Remaining | ForEach-Object { "$($_.ProcessId): $($_.ExecutablePath)" }
    throw "A stale oms-hub.exe outside $ExpectedProjectRoot is still running: $($Details -join '; '). Stop it before starting the scheduled task."
  }
}

$ExistingTask = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($ExistingTask -and $PSCmdlet.ShouldProcess($TaskName, "Stop scheduled task")) {
  Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
}

if ($PSCmdlet.ShouldProcess($BackupPath, "Create rollback backup")) {
  New-Item -ItemType Directory -Force -Path $BackupPath | Out-Null
  $DatabasePath = Join-Path $DataRoot "hub.db"
  if (Test-Path $DatabasePath) {
    Copy-Item $DatabasePath (Join-Path $BackupPath "hub.db")
  }
  $ArtifactPath = Join-Path $DataRoot "artifacts"
  if (Test-Path $ArtifactPath) {
    Copy-Item $ArtifactPath (Join-Path $BackupPath "artifacts") -Recurse
  }
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

if ($PSCmdlet.ShouldProcess($ProjectRoot, "Install Study Hub V2")) {
  if (-not (Test-Path "$ProjectRoot\.venv\Scripts\python.exe")) {
    $PythonVenvArgs = $PythonPrefix + @("-m", "venv", "$ProjectRoot\.venv")
    & $PythonCommand @PythonVenvArgs
  }
  & "$ProjectRoot\.venv\Scripts\python.exe" -m pip install --upgrade pip
  & "$ProjectRoot\.venv\Scripts\python.exe" -m pip install -e $ProjectRoot
  New-Item -ItemType Directory -Force -Path $DataRoot, $BackupRoot | Out-Null
  & icacls.exe $DataRoot /grant "${TaskIdentity}:(OI)(CI)M" /T /C | Out-Null
  if ($LASTEXITCODE -ne 0) {
    throw "Failed to grant $TaskIdentity modify access to $DataRoot"
  }
  if (-not (Test-Path "$ProjectRoot\.env")) {
    Copy-Item "$ProjectRoot\.env.example" "$ProjectRoot\.env"
    throw "Configure $ProjectRoot\.env, then run this installer again"
  }
  & "$ProjectRoot\.venv\Scripts\oms-hub.exe" validate-config
}

if ($PSCmdlet.ShouldProcess($TaskName, "Install scheduled startup")) {
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
  # ScheduledTask stop does not always wait for its child executable to exit.
  # Terminate only identified old-root oms-hub.exe processes, never generic
  # python.exe processes that may belong to unrelated tools.
  Stop-ConflictingHubProcesses -ExpectedProjectRoot $ProjectRoot
  Start-ScheduledTask -TaskName $TaskName
  $RemainingConflicts = @(Get-ConflictingHubProcesses -ExpectedProjectRoot $ProjectRoot)
  if ($RemainingConflicts.Count -gt 0) {
    throw "The scheduled task started while an old-root oms-hub.exe remains. Inspect the task action and stale process list."
  }
  Assert-StartedHubProvenance `
    -ExpectedProjectRoot $ProjectRoot `
    -ExpectedBuildRevision (Get-ProjectBuildRevision -ExpectedProjectRoot $ProjectRoot)
}

Write-Host "Study Hub V2 install complete."
Write-Host "Scheduled task root: $ProjectRoot"
Write-Host "Local dashboard: use OMS_HUB_DASHBOARD_PORT from .env (V4 example: http://127.0.0.1:8787)"
Write-Host "Rollback backup: $BackupPath"
