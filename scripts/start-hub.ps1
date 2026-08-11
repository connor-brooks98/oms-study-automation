[CmdletBinding()]
param(
  [Parameter(Mandatory = $true)]
  [string]$DataRoot,
  [ValidateRange(0, 3)]
  [int]$ActionIndex = 0
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

function Test-JsonInteger {
  param([AllowNull()][object]$Value)
  if ($null -eq $Value) { return $false }
  return (
    $Value -is [System.SByte] -or
    $Value -is [System.Byte] -or
    $Value -is [System.Int16] -or
    $Value -is [System.UInt16] -or
    $Value -is [System.Int32] -or
    $Value -is [System.UInt32] -or
    $Value -is [System.Int64] -or
    $Value -is [System.UInt64]
  )
}

function Test-ExactJsonInteger {
  param([AllowNull()][object]$Value, [System.Int64]$Expected)
  return (
    (Test-JsonInteger -Value $Value) -and
    [System.Decimal]$Value -eq [System.Decimal]$Expected
  )
}

function Write-F28LauncherExitEvidence {
  param(
    [string]$Directory,
    [string]$ExpectedRevision,
    [string]$ExpectedTree,
    [int]$NativeExitCode,
    [int]$CurrentActionIndex
  )
  if ($NativeExitCode -ne 75) { return }
  $LatestPath = Join-Path $Directory "latest-server-exit.json"
  if (-not (Test-Path -LiteralPath $LatestPath -PathType Leaf)) {
    throw "F28 server exit evidence is missing."
  }
  $ClaimPath = Join-Path $Directory (".launcher-claim-{0}.json" -f [guid]::NewGuid())
  Move-Item -LiteralPath $LatestPath -Destination $ClaimPath
  $ClaimedBytes = [System.IO.File]::ReadAllBytes($ClaimPath)
  $Hasher = [System.Security.Cryptography.SHA256]::Create()
  try {
    $ClaimedHash = ([BitConverter]::ToString($Hasher.ComputeHash($ClaimedBytes))).Replace("-", "").ToLowerInvariant()
  } finally {
    $Hasher.Dispose()
  }
  $StrictUtf8 = New-Object System.Text.UTF8Encoding($false, $true)
  $Record = $StrictUtf8.GetString($ClaimedBytes) | ConvertFrom-Json
  $Nonce = [string]$Record.nonce
  $ParsedNonce = [guid]::Empty
  if (-not [guid]::TryParseExact($Nonce, "D", [ref]$ParsedNonce)) {
    throw "F28 server exit nonce is not canonical."
  }
  $ClaimedArchiveName = "consumed-launcher-server-exit-$Nonce.json"
  $ClaimedArchivePath = Join-Path $Directory $ClaimedArchiveName
  $Destination = Join-Path $Directory "launcher-exit-$Nonce.json"
  if (
    (Test-Path -LiteralPath $ClaimedArchivePath) -or
    (Test-Path -LiteralPath $Destination)
  ) {
    throw "F28 launcher evidence already exists for this nonce."
  }
  $FinalizedPath = Join-Path $Directory "finalized-$Nonce.json"
  if (-not (Test-Path -LiteralPath $FinalizedPath -PathType Leaf)) {
    throw "F28 finalized server exit evidence is missing."
  }
  $Finalized = Get-Content -LiteralPath $FinalizedPath -Raw | ConvertFrom-Json
  if (-not (Test-JsonInteger -Value $Record.expected_schema)) {
    throw "F28 server exit expected_schema must be a JSON integer."
  }
  $ExpectedSchema = [long]$Record.expected_schema
  if (
    -not (Test-ExactJsonInteger -Value $Record.schema_version -Expected 1) -or
    $ExpectedSchema -le 0 -or
    -not (Test-ExactJsonInteger -Value $Record.exit_code -Expected 75) -or
    [string]$Record.expected_revision -cne $ExpectedRevision -or
    [string]$Record.expected_tree -cne $ExpectedTree -or
    -not (Test-ExactJsonInteger -Value $Finalized.schema_version -Expected 1) -or
    [string]$Finalized.nonce -cne $Nonce -or
    -not (Test-ExactJsonInteger -Value $Finalized.expected_schema -Expected $ExpectedSchema) -or
    -not (Test-ExactJsonInteger -Value $Finalized.exit_code -Expected 75) -or
    [string]$Finalized.expected_revision -cne $ExpectedRevision -or
    [string]$Finalized.expected_tree -cne $ExpectedTree -or
    [string]$Finalized.latest_server_exit_sha256 -cne $ClaimedHash
  ) {
    throw "F28 server exit evidence does not match this launcher."
  }
  Move-Item -LiteralPath $ClaimPath -Destination $ClaimedArchivePath
  $LauncherRecord = [ordered]@{
    schema_version = 1
    nonce = $Nonce
    expected_revision = $ExpectedRevision
    expected_tree = $ExpectedTree
    expected_schema = $ExpectedSchema
    exit_code = $NativeExitCode
    action_index = $CurrentActionIndex
    claimed_latest_server_exit = $ClaimedArchiveName
    claimed_latest_server_exit_sha256 = $ClaimedHash
    launcher_pid = $PID
    recorded_at = (Get-Date).ToUniversalTime().ToString("o")
  } | ConvertTo-Json -Compress
  $Temporary = Join-Path $Directory (".launcher-exit-{0}.tmp" -f [guid]::NewGuid())
  $Utf8NoBom = New-Object System.Text.UTF8Encoding($false)
  [System.IO.File]::WriteAllText($Temporary, $LauncherRecord + "`n", $Utf8NoBom)
  try {
    [System.IO.File]::Move($Temporary, $Destination)
  } finally {
    if (Test-Path -LiteralPath $Temporary) { Remove-Item -LiteralPath $Temporary -Force }
  }
  if (-not (Test-Path -LiteralPath $Destination -PathType Leaf)) {
    throw "F28 launcher evidence did not publish an exact leaf."
  }
  return [pscustomobject]@{
    nonce = $Nonce
    expected_schema = $ExpectedSchema
    sha256 = (Get-FileHash -LiteralPath $Destination -Algorithm SHA256).Hash.ToLowerInvariant()
  }
}

function Write-F28RecoveryAuthorization {
  param(
    [string]$Directory,
    [string]$Nonce,
    [int]$PredecessorActionIndex,
    [int]$ExpectedSchema,
    [string]$ExpectedRevision,
    [string]$ExpectedTree,
    [string]$PredecessorEvidenceSha256
  )
  if ($PredecessorActionIndex -lt 0 -or $PredecessorActionIndex -ge 3) { return }
  $NextIndex = $PredecessorActionIndex + 1
  $EvidenceName = "launcher-exit-$Nonce.json"
  $Destination = Join-Path $Directory ("recovery-authorized-{0}-{1}.json" -f $Nonce, $NextIndex)
  if (Test-Path -LiteralPath $Destination) { throw "F28 recovery authorization already exists." }
  $AuthorizationTime = (Get-Date).ToUniversalTime()
  $Authorization = [ordered]@{
    schema_version = 1
    nonce = $Nonce
    expected_revision = $ExpectedRevision
    expected_tree = $ExpectedTree
    expected_schema = $ExpectedSchema
    exit_code = 75
    predecessor_action_index = $PredecessorActionIndex
    next_action_index = $NextIndex
    predecessor_evidence = $EvidenceName
    predecessor_evidence_sha256 = $PredecessorEvidenceSha256
    authorized_at = $AuthorizationTime.ToString("o")
    expires_at = $AuthorizationTime.AddMinutes(5).ToString("o")
  } | ConvertTo-Json -Compress
  $Temporary = Join-Path $Directory (".recovery-authorized-{0}.tmp" -f [guid]::NewGuid())
  $Utf8NoBom = New-Object System.Text.UTF8Encoding($false)
  [System.IO.File]::WriteAllText($Temporary, $Authorization + "`n", $Utf8NoBom)
  try {
    [System.IO.File]::Move($Temporary, $Destination)
  } finally {
    if (Test-Path -LiteralPath $Temporary) { Remove-Item -LiteralPath $Temporary -Force }
  }
  if (-not (Test-Path -LiteralPath $Destination -PathType Leaf)) {
    throw "F28 recovery authorization did not publish an exact leaf."
  }
}

Set-Location $ProjectRoot
Write-Host "Starting Study Hub from $ProjectRoot (build $BuildRevision, tree $BuildTree)."
& $HubExecutable serve
$ServeExitCode = $LASTEXITCODE
if ($ServeExitCode -eq 75) {
  try {
    $LauncherEvidence = Write-F28LauncherExitEvidence `
      -Directory $GateDirectory `
      -ExpectedRevision $BuildRevision `
      -ExpectedTree $BuildTree `
      -NativeExitCode $ServeExitCode `
      -CurrentActionIndex $ActionIndex
    if ($ActionIndex -lt 3) {
      Write-F28RecoveryAuthorization `
        -Directory $GateDirectory `
        -Nonce $LauncherEvidence.nonce `
        -PredecessorActionIndex $ActionIndex `
        -ExpectedSchema $LauncherEvidence.expected_schema `
        -ExpectedRevision $BuildRevision `
        -ExpectedTree $BuildTree `
        -PredecessorEvidenceSha256 $LauncherEvidence.sha256
    }
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
