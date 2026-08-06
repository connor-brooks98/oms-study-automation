$ErrorActionPreference = "Stop"
$ProjectRoot = (Resolve-Path -LiteralPath (Split-Path -Parent $PSScriptRoot)).Path
$HubExecutable = Join-Path $ProjectRoot ".venv\Scripts\oms-hub.exe"
if (-not (Test-Path -LiteralPath $HubExecutable)) {
  throw "Study Hub executable not found under scheduled-task project root: $ProjectRoot"
}

$env:OMS_HUB_DEPLOYMENT_ROOT = $ProjectRoot
$Git = Get-Command git.exe -ErrorAction SilentlyContinue
if (-not $Git) { $Git = Get-Command git -ErrorAction SilentlyContinue }
$BuildRevision = "unreported"
if ($Git) {
  $Revision = & $Git.Source -C $ProjectRoot rev-parse HEAD 2>$null
  if ($LASTEXITCODE -eq 0 -and $Revision) {
    $BuildRevision = ([string]$Revision[0]).Trim()
  }
}
$env:OMS_HUB_BUILD_REVISION = $BuildRevision

Set-Location $ProjectRoot
Write-Host "Starting Study Hub from $ProjectRoot (build $BuildRevision)."
& $HubExecutable serve
