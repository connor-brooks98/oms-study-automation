[CmdletBinding()]
param(
  [Parameter(Mandatory = $true)]
  [string]$DataRoot
)

$ErrorActionPreference = "Stop"
$ProjectRoot = (Resolve-Path -LiteralPath (Split-Path -Parent $PSScriptRoot)).Path
$ResolvedDataRoot = (Resolve-Path -LiteralPath $DataRoot).Path
$DataRootItem = Get-Item -LiteralPath $ResolvedDataRoot -Force
if (($DataRootItem.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
  throw "Study Hub data root must not be a reparse point."
}
$AcceptanceDirectory = Join-Path $ResolvedDataRoot "acceptance"
if (-not (Test-Path -LiteralPath $AcceptanceDirectory -PathType Container)) {
  throw "F28 acceptance directory is missing: $AcceptanceDirectory"
}
$AcceptanceDirectoryItem = Get-Item -LiteralPath $AcceptanceDirectory -Force
if (($AcceptanceDirectoryItem.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
  throw "F28 acceptance directory must not be a reparse point."
}
$GateDirectory = Join-Path $ResolvedDataRoot "acceptance\f28"
if (-not (Test-Path -LiteralPath $GateDirectory -PathType Container)) {
  throw "F28 gate directory is missing: $GateDirectory"
}
$GateDirectoryItem = Get-Item -LiteralPath $GateDirectory -Force
if (($GateDirectoryItem.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
  throw "F28 gate directory must not be a reparse point."
}
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
$env:OMS_HUB_F28_GATE_DIR = $GateDirectory

function Write-F28LauncherExitEvidence {
  param(
    [string]$Directory,
    [string]$ExpectedRevision,
    [string]$ExpectedTree,
    [int]$NativeExitCode
  )
  if ($NativeExitCode -ne 75) { return }
  $LatestPath = Join-Path $Directory "latest-server-exit.json"
  if (-not (Test-Path -LiteralPath $LatestPath -PathType Leaf)) {
    throw "F28 server exit evidence is missing."
  }
  $ClaimPath = Join-Path $Directory (".launcher-claim-{0}.json" -f [guid]::NewGuid())
  Move-Item -LiteralPath $LatestPath -Destination $ClaimPath
  try {
    $Record = Get-Content -LiteralPath $ClaimPath -Raw | ConvertFrom-Json
    $Nonce = [string]$Record.nonce
    $ParsedNonce = [guid]::Empty
    if (-not [guid]::TryParseExact($Nonce, "D", [ref]$ParsedNonce)) {
      throw "F28 server exit nonce is not canonical."
    }
    if (
      [int]$Record.exit_code -ne 75 -or
      [string]$Record.expected_revision -cne $ExpectedRevision -or
      [string]$Record.expected_tree -cne $ExpectedTree
    ) {
      throw "F28 server exit evidence does not match this launcher."
    }
    $LauncherRecord = [ordered]@{
      schema_version = 1
      nonce = $Nonce
      expected_revision = $ExpectedRevision
      expected_tree = $ExpectedTree
      exit_code = $NativeExitCode
      launcher_pid = $PID
      recorded_at = (Get-Date).ToUniversalTime().ToString("o")
    } | ConvertTo-Json -Compress
    $Destination = Join-Path $Directory "launcher-exit-$Nonce.json"
    $Temporary = Join-Path $Directory (".launcher-exit-{0}.tmp" -f [guid]::NewGuid())
    $Utf8NoBom = New-Object System.Text.UTF8Encoding($false)
    [System.IO.File]::WriteAllText($Temporary, $LauncherRecord + "`n", $Utf8NoBom)
    Move-Item -LiteralPath $Temporary -Destination $Destination
  } finally {
    if (Test-Path -LiteralPath $ClaimPath) {
      Move-Item -LiteralPath $ClaimPath -Destination (
        Join-Path $Directory "consumed-launcher-server-exit-$([guid]::NewGuid()).json"
      )
    }
  }
}

Set-Location $ProjectRoot
Write-Host "Starting Study Hub from $ProjectRoot (build $BuildRevision, tree $BuildTree)."
& $HubExecutable serve
$ServeExitCode = $LASTEXITCODE
if ($ServeExitCode -eq 75) {
  try {
    Write-F28LauncherExitEvidence `
      -Directory $GateDirectory `
      -ExpectedRevision $BuildRevision `
      -ExpectedTree $BuildTree `
      -NativeExitCode $ServeExitCode
  } catch {
    [Console]::Error.WriteLine("F28 launcher evidence failure: $($_.Exception.Message)")
  }
}
if ($ServeExitCode -ne 0) {
  [Console]::Error.WriteLine(
    "Study Hub server exited with native exit code $ServeExitCode"
  )
}
exit $ServeExitCode
