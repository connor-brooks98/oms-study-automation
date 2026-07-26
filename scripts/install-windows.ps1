[CmdletBinding(SupportsShouldProcess)]
param(
  [string]$ProjectRoot = "C:\Services\oms-study-automation",
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
    -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$ProjectRoot\scripts\start-hub.ps1`""
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
  Start-ScheduledTask -TaskName $TaskName
}

Write-Host "Study Hub V2 install complete."
Write-Host "Local dashboard: http://127.0.0.1:8765"
Write-Host "Rollback backup: $BackupPath"
