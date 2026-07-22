$ErrorActionPreference = "Stop"
$ProjectRoot = "C:\Services\oms-study-automation"
Set-Location $ProjectRoot

$ChromeCandidates = @(
  "$env:ProgramFiles\Google\Chrome\Application\chrome.exe",
  "${env:ProgramFiles(x86)}\Google\Chrome\Application\chrome.exe"
)
$ChromePath = $ChromeCandidates | Where-Object { Test-Path $_ } | Select-Object -First 1
if (-not (Get-Process -Name chrome -ErrorAction SilentlyContinue)) {
  if ($ChromePath) {
    Start-Process -FilePath $ChromePath -ArgumentList "https://lmunet.instructure.com/"
  } else {
    Write-Warning "Google Chrome was not found; open Canvas manually after installing Chrome."
  }
}

& "$ProjectRoot\.venv\Scripts\oms-hub.exe" serve
