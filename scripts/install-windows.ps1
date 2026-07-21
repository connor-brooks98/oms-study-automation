$ErrorActionPreference = "Stop"
$ProjectRoot = "C:\Services\oms-study-automation"
$DataRoot = "C:\ProgramData\OMSStudyHub"
$TaskName = "OMS Study Automation Hub"

if (-not (Test-Path $ProjectRoot)) {
  throw "Project directory not found: $ProjectRoot"
}

& py -3.12 --version
if ($LASTEXITCODE -ne 0) { throw "Python 3.12 is required" }

if (-not (Test-Path "$ProjectRoot\.venv\Scripts\python.exe")) {
  & py -3.12 -m venv "$ProjectRoot\.venv"
}
& "$ProjectRoot\.venv\Scripts\python.exe" -m pip install --upgrade pip
& "$ProjectRoot\.venv\Scripts\python.exe" -m pip install -e $ProjectRoot
New-Item -ItemType Directory -Force -Path $DataRoot | Out-Null

$PowerShell = (Get-Command powershell.exe).Source
$Action = New-ScheduledTaskAction `
  -Execute $PowerShell `
  -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$ProjectRoot\scripts\start-hub.ps1`""
$Trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$Settings = New-ScheduledTaskSettingsSet `
  -RestartCount 3 `
  -RestartInterval (New-TimeSpan -Minutes 1) `
  -StartWhenAvailable
$Principal = New-ScheduledTaskPrincipal `
  -UserId $env:USERNAME `
  -LogonType Interactive

Register-ScheduledTask `
  -TaskName $TaskName `
  -Action $Action `
  -Trigger $Trigger `
  -Settings $Settings `
  -Principal $Principal `
  -Force | Out-Null

Write-Host "Installed. Dashboard: http://127.0.0.1:8765"
Write-Host "Remove with: Unregister-ScheduledTask -TaskName '$TaskName' -Confirm:`$false"
