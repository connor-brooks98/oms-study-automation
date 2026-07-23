$ErrorActionPreference = "Stop"
$ProjectRoot = "C:\Services\oms-study-automation"
$DataRoot = "C:\ProgramData\OMSStudyHub"
$TaskName = "OMS Study Automation Hub"
$CanvasInbox = Join-Path $env:USERPROFILE "Downloads\OMSStudyHub\CanvasInbox"
$StudyRoot = Join-Path $env:USERPROFILE "Documents\OMS II"
$RevisionRoot = Join-Path $DataRoot "artifacts\revisions"
$PanoptoRevisionRoot = Join-Path $DataRoot "artifacts\panopto\revisions"
$TaskIdentity = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name

if (-not (Test-Path $ProjectRoot)) {
  throw "Project directory not found: $ProjectRoot"
}

$PyLauncher = Get-Command py -ErrorAction SilentlyContinue
$PythonCommand = if ($PyLauncher) { $PyLauncher.Source } else { (Get-Command python -ErrorAction Stop).Source }
$PythonPrefix = if ($PyLauncher) { @("-3.12") } else { @() }
$PythonVersionArgs = $PythonPrefix + @("--version")
& $PythonCommand @PythonVersionArgs
if ($LASTEXITCODE -ne 0) { throw "Python 3.12 is required" }

if (-not (Test-Path "$ProjectRoot\.venv\Scripts\python.exe")) {
  $PythonVenvArgs = $PythonPrefix + @("-m", "venv", "$ProjectRoot\.venv")
  & $PythonCommand @PythonVenvArgs
}
& "$ProjectRoot\.venv\Scripts\python.exe" -m pip install --upgrade pip
& "$ProjectRoot\.venv\Scripts\python.exe" -m pip install -e $ProjectRoot
New-Item -ItemType Directory -Force -Path $DataRoot, $CanvasInbox, $StudyRoot, $RevisionRoot, $PanoptoRevisionRoot | Out-Null
& icacls.exe $DataRoot /grant "${TaskIdentity}:(OI)(CI)M" /T /C | Out-Null
if ($LASTEXITCODE -ne 0) {
  throw "Failed to grant $TaskIdentity modify access to $DataRoot"
}
if (-not (Test-Path "$ProjectRoot\.env")) {
  Copy-Item "$ProjectRoot\.env.example" "$ProjectRoot\.env"
}

$PowerShell = (Get-Command powershell.exe).Source
$Action = New-ScheduledTaskAction `
  -Execute $PowerShell `
  -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$ProjectRoot\scripts\start-hub.ps1`""
$Trigger = New-ScheduledTaskTrigger -AtLogOn -User $TaskIdentity
$Settings = New-ScheduledTaskSettingsSet `
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
  -Settings $Settings `
  -Principal $Principal `
  -Force | Out-Null

Write-Host "Installed. Dashboard: http://127.0.0.1:8765"
Write-Host "Canvas inbox: $CanvasInbox"
Write-Host "Remove with: Unregister-ScheduledTask -TaskName '$TaskName' -Confirm:`$false"
