$ErrorActionPreference = "Stop"
$ProjectRoot = (Resolve-Path -LiteralPath (Split-Path -Parent $PSScriptRoot)).Path
$HubExecutable = Join-Path $ProjectRoot ".venv\Scripts\oms-hub.exe"
if (-not (Test-Path -LiteralPath $HubExecutable)) {
  throw "Study Hub executable not found under scheduled-task project root: $ProjectRoot"
}

$env:OMS_HUB_DEPLOYMENT_ROOT = $ProjectRoot
$Git = Get-Command git.exe -ErrorAction SilentlyContinue
if (-not $Git) { $Git = Get-Command git -ErrorAction SilentlyContinue }
if (-not $Git) { throw "Git is required to establish exact runtime provenance." }
$SourceStatus = @(
  & $Git.Source -C $ProjectRoot status --porcelain=v1 --untracked-files=all -- src scripts pyproject.toml 2>$null
)
if ($LASTEXITCODE -ne 0) {
  throw "Study Hub runtime source cleanliness could not be verified."
}
if ($SourceStatus.Count -gt 0) {
  throw "Study Hub runtime source differs from HEAD; refusing to start an editable deployment."
}
$Revision = @(& $Git.Source -C $ProjectRoot rev-parse HEAD 2>$null)
if ($LASTEXITCODE -ne 0 -or -not $Revision) {
  throw "Study Hub runtime revision could not be resolved."
}
$BuildRevision = ([string]$Revision[0]).Trim().ToLowerInvariant()
if ($BuildRevision -notmatch "^[0-9a-f]{40}$") {
  throw "Study Hub runtime revision is not a full commit SHA."
}
$Tree = @(& $Git.Source -C $ProjectRoot rev-parse "HEAD^{tree}" 2>$null)
if ($LASTEXITCODE -ne 0 -or -not $Tree) {
  throw "Study Hub runtime tree could not be resolved."
}
$BuildTree = ([string]$Tree[0]).Trim().ToLowerInvariant()
if ($BuildTree -notmatch "^[0-9a-f]{40}$") {
  throw "Study Hub runtime tree is not a full tree SHA."
}
$env:OMS_HUB_BUILD_REVISION = $BuildRevision
$env:OMS_HUB_BUILD_TREE = $BuildTree

Set-Location $ProjectRoot
Write-Host "Starting Study Hub from $ProjectRoot (build $BuildRevision, tree $BuildTree)."
& $HubExecutable serve
