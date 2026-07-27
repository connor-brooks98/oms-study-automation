$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $PSScriptRoot
Set-Location $ProjectRoot

& "$ProjectRoot\.venv\Scripts\oms-hub.exe" serve
