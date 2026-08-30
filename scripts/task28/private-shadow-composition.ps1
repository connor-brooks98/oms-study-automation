[CmdletBinding()]
param(
  [Parameter(Mandatory = $true)][ValidateSet("Stage", "Verify")][string]$Mode,
  [string]$SourceArchive,
  [string]$RepositoryRoot,
  [string]$SourceCommit,
  [string]$LockedRequirements,
  [string]$Destination,
  [string]$TaskName,
  [string]$RunId,
  [string]$PythonExecutable,
  [string]$HubHealthUrl,
  [string]$Manifest
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
$Utf8 = [System.Text.UTF8Encoding]::new($false, $true)
. (Join-Path $PSScriptRoot "private-shadow-common.ps1")

function Write-CanonicalJson {
  param([Parameter(Mandatory = $true)][string]$Path, [Parameter(Mandatory = $true)][object]$Value)
  $Temporary = Join-Path (Split-Path -Parent $Path) (".{0}.tmp" -f [Guid]::NewGuid().ToString("N"))
  try {
    [IO.File]::WriteAllText($Temporary, (($Value | ConvertTo-Json -Compress -Depth 12) + "`n"), $Utf8)
    [IO.File]::Move($Temporary, $Path)
  } finally {
    Remove-Item -LiteralPath $Temporary -Force -ErrorAction SilentlyContinue
  }
}

function Get-StringSha256 {
  param([Parameter(Mandatory = $true)][string]$Value)
  $Hasher = [Security.Cryptography.SHA256]::Create()
  try { return ([BitConverter]::ToString($Hasher.ComputeHash($Utf8.GetBytes($Value)))).Replace("-", "").ToLowerInvariant() }
  finally { $Hasher.Dispose() }
}


if ($Mode -eq "Stage") {
  foreach ($Required in @($SourceArchive, $RepositoryRoot, $SourceCommit, $LockedRequirements,
      $Destination, $TaskName, $RunId, $PythonExecutable, $HubHealthUrl)) {
    if ([string]::IsNullOrWhiteSpace($Required)) { throw "Stage input was missing." }
  }
  if ($SourceCommit -notmatch "^[0-9a-f]{40}$" -or $TaskName -notmatch "^[A-Za-z0-9._-]{1,120}$" -or
      $RunId -cnotmatch "^[0-9a-f]{32}$") {
    throw "Stage identity input is invalid."
  }
  $HealthUri = [Uri]$HubHealthUrl
  if ($HealthUri.Scheme -notin @("http", "https") -or
      $HealthUri.Host -notin @("127.0.0.1", "localhost") -or
      $HealthUri.AbsolutePath -cne "/health" -or -not [string]::IsNullOrEmpty($HealthUri.Query)) {
    throw "Hub health URL must be local and exact."
  }
  $SourceArchive = Resolve-Task28ExistingPath -Path $SourceArchive -Type Leaf
  $RepositoryRoot = Resolve-Task28ExistingPath -Path $RepositoryRoot -Type Container
  $LockedRequirements = Resolve-Task28ExistingPath -Path $LockedRequirements -Type Leaf
  $PythonExecutable = Resolve-Task28ExistingPath -Path $PythonExecutable -Type Leaf
  if (-not (Test-Task28FullyQualifiedPath -Path $Destination)) { throw "Stage destination must be absolute." }
  $Destination = [IO.Path]::GetFullPath($Destination)
  $State = Get-Task28StatePaths -RunId $RunId
  $FinalDestination = $Destination
  Assert-Task28NoReparsePath -Path (Split-Path -Parent $Destination)
  if ((Test-Path -LiteralPath $FinalDestination) -or (Test-Path -LiteralPath $State.Root)) {
    throw "Stage destinations must be absent."
  }
  if ([string]::Equals($FinalDestination, $State.Root, [StringComparison]::OrdinalIgnoreCase) -or
      (Test-Task28DescendantPath -Path $State.Root -Root $FinalDestination) -or
      (Test-Task28DescendantPath -Path $FinalDestination -Root $State.Root)) {
    throw "Immutable and mutable paths must not overlap."
  }
  $StageRoot = Join-Path (Split-Path -Parent $FinalDestination) (
    ".{0}.stage-{1}" -f (Split-Path -Leaf $FinalDestination), [Guid]::NewGuid().ToString("N")
  )
  if (Test-Path -LiteralPath $StageRoot) { throw "Stage root already exists." }
  $Destination = $StageRoot
  New-Item -ItemType Directory -Path $StageRoot | Out-Null
  try {
  $Tree = (& git -C $RepositoryRoot rev-parse "$SourceCommit^{tree}").Trim()
  if ($LASTEXITCODE -ne 0 -or $Tree -notmatch "^[0-9a-f]{40}$") { throw "Declared commit is unavailable." }
  $ExpectedArchive = Join-Path ([IO.Path]::GetTempPath()) ("oms-task28-archive-{0}.tar" -f [Guid]::NewGuid().ToString("N"))
  try {
    & git -C $RepositoryRoot archive --format=tar --prefix=source/ `
      "--add-virtual-file=source/.task28-source-commit:$SourceCommit" "--output=$ExpectedArchive" $SourceCommit
    if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $ExpectedArchive -PathType Leaf) -or
        -not [System.Linq.Enumerable]::SequenceEqual(
          [IO.File]::ReadAllBytes($ExpectedArchive), [IO.File]::ReadAllBytes($SourceArchive)
        )) {
      throw "Supplied archive does not exactly match the declared Git commit."
    }
  } finally {
    Remove-Item -LiteralPath $ExpectedArchive -Force -ErrorAction SilentlyContinue
  }
  Copy-Item -LiteralPath $SourceArchive -Destination (Join-Path $Destination "source.tar")
  Copy-Item -LiteralPath $LockedRequirements -Destination (Join-Path $Destination "requirements.lock")
  & tar -xf (Join-Path $Destination "source.tar") -C $Destination
  if ($LASTEXITCODE -ne 0) { throw "Source archive extraction failed." }
  $SourceRoot = Join-Path $Destination "source"
  $CommitMarker = Join-Path $SourceRoot ".task28-source-commit"
  if (-not (Test-Path -LiteralPath $CommitMarker -PathType Leaf) -or
      ([IO.File]::ReadAllText($CommitMarker, $Utf8).Trim() -cne $SourceCommit)) {
    throw "Source archive commit marker is invalid."
  }
  $SourceRows = Get-Task28FileRows -Root $SourceRoot
  $SourceManifestPath = Join-Path $Destination "source-manifest.json"
  $RuntimeManifestPath = Join-Path $Destination "runtime-manifest.json"
  Write-CanonicalJson -Path $SourceManifestPath -Value ([ordered]@{schema_version=1; files=$SourceRows})
  $RuntimeRoot = Join-Path $Destination "runtime"
  New-Item -ItemType Directory -Path $RuntimeRoot | Out-Null
  Move-Item -LiteralPath (Join-Path $Destination "requirements.lock") -Destination (Join-Path $RuntimeRoot "requirements.lock")
  $RuntimeRows = @(Get-Task28FileRows -Root $RuntimeRoot)
  Write-CanonicalJson -Path $RuntimeManifestPath -Value ([ordered]@{schema_version=1; files=$RuntimeRows})
  $Controller = Join-Path $SourceRoot "scripts/task28/private-shadow-controller.ps1"
  $Launcher = Join-Path $SourceRoot "scripts/task28/private-shadow-launcher.ps1"
  $Common = Join-Path $SourceRoot "scripts/task28/private-shadow-common.ps1"
  $EntryPoint = Join-Path $SourceRoot "scripts/private-shadow-operator-entry.py"
  $Wrapper = Join-Path $SourceRoot "scripts/run-private-shadow-evidence.ps1"
  $Evidence = Join-Path $SourceRoot "src/oms_hub/providers/gemini/evidence.py"
  $Smoke = Join-Path $SourceRoot "scripts/run-gemini-contract-smoke.py"
  foreach ($Required in @($Controller, $Launcher, $Common, $EntryPoint, $Wrapper, $Evidence, $Smoke)) {
    if (-not (Test-Path -LiteralPath $Required -PathType Leaf)) { throw "Required tracked bundle file is absent." }
  }
  $RunManifest = [ordered]@{
    schema_version=1
    source=[ordered]@{commit=$SourceCommit; tree=$Tree; archive_sha256=(Get-FileHash -LiteralPath (Join-Path $Destination "source.tar") -Algorithm SHA256).Hash.ToLowerInvariant(); manifest_sha256=(Get-FileHash -LiteralPath $SourceManifestPath -Algorithm SHA256).Hash.ToLowerInvariant()}
    runtime=[ordered]@{lock_sha256=$RuntimeRows[0].sha256; manifest_sha256=(Get-FileHash -LiteralPath $RuntimeManifestPath -Algorithm SHA256).Hash.ToLowerInvariant()}
    allowed_task_name=$TaskName
    immutable_bundle_path=$FinalDestination
    run_id=$RunId
    mutable_state_path=$State.Root
    controller_sha256=(Get-FileHash -LiteralPath $Controller -Algorithm SHA256).Hash.ToLowerInvariant()
    launcher_sha256=(Get-FileHash -LiteralPath $Launcher -Algorithm SHA256).Hash.ToLowerInvariant()
    common_sha256=(Get-FileHash -LiteralPath $Common -Algorithm SHA256).Hash.ToLowerInvariant()
    entrypoint_sha256=(Get-FileHash -LiteralPath $EntryPoint -Algorithm SHA256).Hash.ToLowerInvariant()
    wrapper_sha256=(Get-FileHash -LiteralPath $Wrapper -Algorithm SHA256).Hash.ToLowerInvariant()
    evidence_sha256=(Get-FileHash -LiteralPath $Evidence -Algorithm SHA256).Hash.ToLowerInvariant()
    smoke_sha256=(Get-FileHash -LiteralPath $Smoke -Algorithm SHA256).Hash.ToLowerInvariant()
    python_executable=$PythonExecutable
    hub_health_url=$HealthUri.AbsoluteUri.TrimEnd("/")
    authorization_count=0
  }
  $ManifestJson = ($RunManifest | ConvertTo-Json -Compress -Depth 12) + "`n"
  $RunManifestHash = Get-StringSha256 -Value $ManifestJson
  $RunManifestPath = Join-Path $Destination ("run-manifest.$RunManifestHash.json")
  [IO.File]::WriteAllText($RunManifestPath, $ManifestJson, $Utf8)
  $BundleRows = Get-Task28FileRows -Root $Destination
  $BundleJson = (([ordered]@{schema_version=1; files=$BundleRows} | ConvertTo-Json -Compress -Depth 12) + "`n")
  $BundleManifestHash = Get-StringSha256 -Value $BundleJson
  [IO.File]::WriteAllText((Join-Path $Destination "bundle-manifest.$BundleManifestHash.json"), $BundleJson, $Utf8)
  [IO.Directory]::Move($StageRoot, $FinalDestination)
  $StageRoot = $null
  } finally {
    if ($StageRoot -and (Test-Path -LiteralPath $StageRoot)) {
      Remove-Item -LiteralPath $StageRoot -Recurse -Force -ErrorAction SilentlyContinue
    }
  }
  exit 0
}

if ([string]::IsNullOrWhiteSpace($RepositoryRoot)) { throw "Verify requires an absolute repository root." }
$RepositoryRoot = Resolve-Task28ExistingPath -Path $RepositoryRoot -Type Container
$Run = Read-BoundRunManifest -Path $Manifest
$Bundle = Assert-ImmutableBundle -Run $Run
$Tree = (& git -C $RepositoryRoot rev-parse "$($Run.Value.source.commit)^{tree}").Trim()
if ($LASTEXITCODE -ne 0 -or -not [string]::Equals($Tree, [string]$Run.Value.source.tree, [StringComparison]::OrdinalIgnoreCase)) {
  throw "Declared source tree does not match the bound run."
}
$VerifySourceRoot = Join-Path ([IO.Path]::GetTempPath()) ("oms-task28-verify-source-{0}" -f [Guid]::NewGuid().ToString("N"))
$ExpectedArchive = Join-Path ([IO.Path]::GetTempPath()) ("oms-task28-verify-archive-{0}.tar" -f [Guid]::NewGuid().ToString("N"))
New-Item -ItemType Directory -Path $VerifySourceRoot | Out-Null
try {
  & git -C $RepositoryRoot archive --format=tar --prefix=source/ `
    "--add-virtual-file=source/.task28-source-commit:$($Run.Value.source.commit)" `
    "--output=$ExpectedArchive" $Run.Value.source.commit
  if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $ExpectedArchive -PathType Leaf) -or
      -not [System.Linq.Enumerable]::SequenceEqual(
        [IO.File]::ReadAllBytes($ExpectedArchive), [IO.File]::ReadAllBytes((Join-Path $Bundle "source.tar"))
      )) {
    throw "Source archive does not exactly match the bound Git commit."
  }
  & tar -xf $ExpectedArchive -C $VerifySourceRoot
  if ($LASTEXITCODE -ne 0) { throw "Bound source archive extraction failed." }
  $ExtractedSource = Join-Path $VerifySourceRoot "source"
  $CommitMarker = Join-Path $ExtractedSource ".task28-source-commit"
  if (-not (Test-Path -LiteralPath $CommitMarker -PathType Leaf) -or
      ([IO.File]::ReadAllText($CommitMarker, $Utf8).Trim() -cne [string]$Run.Value.source.commit)) {
    throw "Bound source archive commit marker is invalid."
  }
  Assert-ManifestEquality -Root $ExtractedSource -ManifestPath (Join-Path $Bundle "source-manifest.json") | Out-Null
} finally {
  Remove-Item -LiteralPath $ExpectedArchive -Force -ErrorAction SilentlyContinue
  Remove-Item -LiteralPath $VerifySourceRoot -Recurse -Force -ErrorAction SilentlyContinue
}
$Controller = Join-Path $Bundle "source/scripts/task28/private-shadow-controller.ps1"
try {
  $PreviousVerify = $env:OMS_TASK28_COMPOSITION_VERIFY
  $env:OMS_TASK28_COMPOSITION_VERIFY = "1"
  & $Controller -Manifest $Run.Path
  if ($LASTEXITCODE -ne 1) { throw "Synthetic controller path did not return blocked evidence." }
  $State = Get-Task28StatePaths -RunId ([string]$Run.Value.run_id)
  $Result = Join-Path $State.Evidence "result.json"
  $Status = Join-Path $State.Evidence "status.json"
  if (-not (Test-Path -LiteralPath $Result -PathType Leaf) -or
      -not (Test-Path -LiteralPath $Status -PathType Leaf) -or
      ([IO.File]::ReadAllText($Result, $Utf8) | ConvertFrom-Json).status -cne "blocked" -or
      -not ([IO.File]::ReadAllText($Status, $Utf8) | ConvertFrom-Json).evidence_usable) {
    throw "Synthetic controller path did not produce canonical evidence."
  }
  $Health = Invoke-WebRequest -UseBasicParsing -Uri ([string]$Run.Value.hub_health_url) -TimeoutSec 5
  if ($Health.StatusCode -ne 200 -or ($Health.Content | ConvertFrom-Json).status -cne "ok") {
    throw "Hub health check failed."
  }
} finally {
  $env:OMS_TASK28_COMPOSITION_VERIFY = $PreviousVerify
}
