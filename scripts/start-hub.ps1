$ErrorActionPreference = "Stop"
$ProjectRoot = "C:\Services\oms-study-automation"
Set-Location $ProjectRoot
& "$ProjectRoot\.venv\Scripts\oms-hub.exe" serve
