[CmdletBinding()]
param(
  [Parameter(Mandatory = $true)][ValidateSet("Stage", "Verify")][string]$Mode,
  [string]$SourceArchive,
  [string]$RepositoryRoot,
  [string]$SourceCommit,
  [string]$LockedRequirements,
  [string]$Destination,
  [string]$TaskName,
  [string]$MutableStatePath,
  [string]$PythonExecutable,
  [string]$HubHealthUrl,
  [string]$Manifest
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
$Utf8 = [System.Text.UTF8Encoding]::new($false, $true)

function Resolve-ExistingFile {
  param([Parameter(Mandatory = $true)][string]$Path)
  if (-not [IO.Path]::IsPathFullyQualified($Path) -or
      -not (Test-Path -LiteralPath $Path -PathType Leaf)) {
    throw "Expected an absolute existing file."
  }
  $Resolved = (Get-Item -LiteralPath $Path -Force).FullName
  if ((Get-Item -LiteralPath $Resolved -Force).Attributes -band [IO.FileAttributes]::ReparsePoint) {
    throw "File must not be a reparse point."
  }
  return [IO.Path]::GetFullPath($Resolved)
}

function Resolve-ExistingDirectory {
  param([Parameter(Mandatory = $true)][string]$Path)
  if (-not [IO.Path]::IsPathFullyQualified($Path) -or
      -not (Test-Path -LiteralPath $Path -PathType Container)) {
    throw "Expected an absolute existing directory."
  }
  $Resolved = (Get-Item -LiteralPath $Path -Force).FullName
  if ((Get-Item -LiteralPath $Resolved -Force).Attributes -band [IO.FileAttributes]::ReparsePoint) {
    throw "Directory must not be a reparse point."
  }
  return [IO.Path]::GetFullPath($Resolved)
}

function Assert-NoReparsePath {
  param([Parameter(Mandatory = $true)][string]$Path)
  $Cursor = [IO.Path]::GetFullPath($Path)
  while ($true) {
    if (Test-Path -LiteralPath $Cursor) {
      if ((Get-Item -LiteralPath $Cursor -Force).Attributes -band [IO.FileAttributes]::ReparsePoint) {
        throw "Path must not cross a reparse point."
      }
    }
    $Parent = Split-Path -Parent $Cursor
    if ([string]::IsNullOrEmpty($Parent) -or $Parent -ceq $Cursor) { return }
    $Cursor = $Parent
  }
}

function Assert-UnderRoot {
  param([Parameter(Mandatory = $true)][string]$Path, [Parameter(Mandatory = $true)][string]$Root)
  $Prefix = $Root.TrimEnd("\\", "/") + [IO.Path]::DirectorySeparatorChar
  if (-not $Path.StartsWith($Prefix, [StringComparison]::OrdinalIgnoreCase)) {
    throw "Path escaped its expected root."
  }
}

function Get-FileRows {
  param([Parameter(Mandatory = $true)][string]$Root)
  $Prefix = $Root.TrimEnd("\\", "/") + [IO.Path]::DirectorySeparatorChar
  $Items = @(Get-ChildItem -LiteralPath $Root -Force -Recurse)
  if (@($Items | Where-Object { $_.Attributes -band [IO.FileAttributes]::ReparsePoint }).Count -ne 0) {
    throw "Bundle contains a reparse point."
  }
  return @(
    $Items | Where-Object { -not $_.PSIsContainer } | Sort-Object FullName | ForEach-Object {
      [ordered]@{
        path = $_.FullName.Substring($Prefix.Length).Replace("\\", "/")
        sha256 = (Get-FileHash -LiteralPath $_.FullName -Algorithm SHA256).Hash.ToLowerInvariant()
        size = [int64]$_.Length
      }
    }
  )
}

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

function Read-RunManifest {
  param([Parameter(Mandatory = $true)][string]$Path)
  $ManifestPath = Resolve-ExistingFile -Path $Path
  $Value = [IO.File]::ReadAllText($ManifestPath, $Utf8) | ConvertFrom-Json
  if ($Value.schema_version -ne 1 -or
      $Value.authorization_count -ne 0 -or
      -not [IO.Path]::IsPathFullyQualified([string]$Value.immutable_bundle_path) -or
      -not [IO.Path]::IsPathFullyQualified([string]$Value.mutable_state_path)) {
    throw "Run manifest is invalid."
  }
  return [pscustomobject]@{Path=$ManifestPath; Value=$Value}
}

function Assert-ManifestFile {
  param([Parameter(Mandatory = $true)][string]$Path, [Parameter(Mandatory = $true)][object]$Row)
  if (-not (Test-Path -LiteralPath $Path -PathType Leaf) -or
      (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToLowerInvariant() -cne $Row.sha256) {
    throw "Immutable bundle file hash differs."
  }
}

if ($Mode -eq "Stage") {
  foreach ($Required in @($SourceArchive, $RepositoryRoot, $SourceCommit, $LockedRequirements,
      $Destination, $TaskName, $MutableStatePath, $PythonExecutable, $HubHealthUrl)) {
    if ([string]::IsNullOrWhiteSpace($Required)) { throw "Stage input was missing." }
  }
  if ($SourceCommit -notmatch "^[0-9a-f]{40}$" -or $TaskName -notmatch "^[A-Za-z0-9._-]{1,120}$") {
    throw "Stage identity input is invalid."
  }
  $HealthUri = [Uri]$HubHealthUrl
  if ($HealthUri.Scheme -notin @("http", "https") -or
      $HealthUri.Host -notin @("127.0.0.1", "localhost") -or
      $HealthUri.AbsolutePath -cne "/health") {
    throw "Hub health URL must be local and exact."
  }
  $SourceArchive = Resolve-ExistingFile -Path $SourceArchive
  $RepositoryRoot = Resolve-ExistingDirectory -Path $RepositoryRoot
  $LockedRequirements = Resolve-ExistingFile -Path $LockedRequirements
  $PythonExecutable = Resolve-ExistingFile -Path $PythonExecutable
  if (-not [IO.Path]::IsPathFullyQualified($Destination) -or
      -not [IO.Path]::IsPathFullyQualified($MutableStatePath)) {
    throw "Stage output paths must be absolute."
  }
  $Destination = [IO.Path]::GetFullPath($Destination)
  $MutableStatePath = [IO.Path]::GetFullPath($MutableStatePath)
  $FinalDestination = $Destination
  Assert-NoReparsePath -Path (Split-Path -Parent $Destination)
  Assert-NoReparsePath -Path (Split-Path -Parent $MutableStatePath)
  if (Test-Path -LiteralPath $FinalDestination -or Test-Path -LiteralPath $MutableStatePath) {
    throw "Stage destinations must be absent."
  }
  if ($FinalDestination -ceq $MutableStatePath -or
      $MutableStatePath.StartsWith($FinalDestination.TrimEnd("\\", "/") + [IO.Path]::DirectorySeparatorChar,
      [StringComparison]::OrdinalIgnoreCase) -or
      $FinalDestination.StartsWith($MutableStatePath.TrimEnd("\\", "/") + [IO.Path]::DirectorySeparatorChar,
      [StringComparison]::OrdinalIgnoreCase)) {
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
  $SourceRows = Get-FileRows -Root $SourceRoot
  $SourceManifestPath = Join-Path $Destination "source-manifest.json"
  $RuntimeManifestPath = Join-Path $Destination "runtime-manifest.json"
  Write-CanonicalJson -Path $SourceManifestPath -Value ([ordered]@{schema_version=1; files=$SourceRows})
  $RuntimeRows = @([ordered]@{
    path="requirements.lock"
    sha256=(Get-FileHash -LiteralPath (Join-Path $Destination "requirements.lock") -Algorithm SHA256).Hash.ToLowerInvariant()
    size=[int64](Get-Item -LiteralPath (Join-Path $Destination "requirements.lock")).Length
  })
  Write-CanonicalJson -Path $RuntimeManifestPath -Value ([ordered]@{schema_version=1; files=$RuntimeRows})
  $Controller = Join-Path $SourceRoot "scripts/task28/private-shadow-controller.ps1"
  $Launcher = Join-Path $SourceRoot "scripts/task28/private-shadow-launcher.ps1"
  $EntryPoint = Join-Path $SourceRoot "scripts/private-shadow-operator-entry.py"
  $Wrapper = Join-Path $SourceRoot "scripts/run-private-shadow-evidence.ps1"
  $Evidence = Join-Path $SourceRoot "src/oms_hub/providers/gemini/evidence.py"
  foreach ($Required in @($Controller, $Launcher, $EntryPoint, $Wrapper, $Evidence)) {
    if (-not (Test-Path -LiteralPath $Required -PathType Leaf)) { throw "Required tracked bundle file is absent." }
  }
  $RunManifest = [ordered]@{
    schema_version=1
    source=[ordered]@{commit=$SourceCommit; tree=$Tree; archive_sha256=(Get-FileHash -LiteralPath (Join-Path $Destination "source.tar") -Algorithm SHA256).Hash.ToLowerInvariant(); manifest_sha256=(Get-FileHash -LiteralPath $SourceManifestPath -Algorithm SHA256).Hash.ToLowerInvariant()}
    runtime=[ordered]@{lock_sha256=$RuntimeRows[0].sha256; manifest_sha256=(Get-FileHash -LiteralPath $RuntimeManifestPath -Algorithm SHA256).Hash.ToLowerInvariant()}
    allowed_task_name=$TaskName
    immutable_bundle_path=$FinalDestination
    mutable_state_path=$MutableStatePath
    controller_sha256=(Get-FileHash -LiteralPath $Controller -Algorithm SHA256).Hash.ToLowerInvariant()
    launcher_sha256=(Get-FileHash -LiteralPath $Launcher -Algorithm SHA256).Hash.ToLowerInvariant()
    entrypoint_sha256=(Get-FileHash -LiteralPath $EntryPoint -Algorithm SHA256).Hash.ToLowerInvariant()
    wrapper_sha256=(Get-FileHash -LiteralPath $Wrapper -Algorithm SHA256).Hash.ToLowerInvariant()
    evidence_sha256=(Get-FileHash -LiteralPath $Evidence -Algorithm SHA256).Hash.ToLowerInvariant()
    python_executable=$PythonExecutable
    hub_health_url=$HealthUri.AbsoluteUri.TrimEnd("/")
    authorization_count=0
  }
  $ManifestJson = ($RunManifest | ConvertTo-Json -Compress -Depth 12) + "`n"
  $RunManifestHash = Get-StringSha256 -Value $ManifestJson
  $RunManifestPath = Join-Path $Destination ("run-manifest.$RunManifestHash.json")
  [IO.File]::WriteAllText($RunManifestPath, $ManifestJson, $Utf8)
  [IO.Directory]::Move($StageRoot, $FinalDestination)
  if ((Get-Item -LiteralPath $FinalDestination -Force).FullName -cne $FinalDestination) {
    throw "Final bundle identity changed during promotion."
  }
  $StageRoot = $null
  } finally {
    if ($StageRoot -and (Test-Path -LiteralPath $StageRoot)) {
      Remove-Item -LiteralPath $StageRoot -Recurse -Force -ErrorAction SilentlyContinue
    }
  }
  exit 0
}

$Run = Read-RunManifest -Path $Manifest
$ManifestHash = (Get-FileHash -LiteralPath $Run.Path -Algorithm SHA256).Hash.ToLowerInvariant()
if ((Split-Path -Leaf $Run.Path) -cne "run-manifest.$ManifestHash.json") {
  throw "Run manifest filename is not bound to its contents."
}
$Bundle = Resolve-ExistingDirectory -Path ([string]$Run.Value.immutable_bundle_path)
Assert-UnderRoot -Path $Run.Path -Root $Bundle
if (Test-Path -LiteralPath ([string]$Run.Value.mutable_state_path)) {
  throw "Correction 1 must not create mutable state."
}
$SourceRoot = Join-Path $Bundle "source"
Assert-ManifestFile -Path (Join-Path $Bundle "source.tar") -Row ([pscustomobject]@{sha256=$Run.Value.source.archive_sha256})
Assert-ManifestFile -Path (Join-Path $Bundle "source-manifest.json") -Row ([pscustomobject]@{sha256=$Run.Value.source.manifest_sha256})
Assert-ManifestFile -Path (Join-Path $Bundle "runtime-manifest.json") -Row ([pscustomobject]@{sha256=$Run.Value.runtime.manifest_sha256})
foreach ($Row in @((Get-Content -LiteralPath (Join-Path $Bundle "source-manifest.json") -Raw | ConvertFrom-Json).files)) {
  Assert-ManifestFile -Path (Join-Path $SourceRoot ([string]$Row.path)) -Row $Row
}
$ExpectedSourceRows = @((Get-Content -LiteralPath (Join-Path $Bundle "source-manifest.json") -Raw | ConvertFrom-Json).files)
$ActualSourceRows = @(Get-FileRows -Root $SourceRoot)
if (($ExpectedSourceRows | ConvertTo-Json -Compress -Depth 5) -cne
    ($ActualSourceRows | ConvertTo-Json -Compress -Depth 5)) {
  throw "Source manifest does not exactly match immutable source files."
}
foreach ($Row in @((Get-Content -LiteralPath (Join-Path $Bundle "runtime-manifest.json") -Raw | ConvertFrom-Json).files)) {
  Assert-ManifestFile -Path (Join-Path $Bundle ([string]$Row.path)) -Row $Row
}
$Controller = Join-Path $SourceRoot "scripts/task28/private-shadow-controller.ps1"
$Launcher = Join-Path $SourceRoot "scripts/task28/private-shadow-launcher.ps1"
Assert-ManifestFile -Path $Controller -Row ([pscustomobject]@{sha256=$Run.Value.controller_sha256})
Assert-ManifestFile -Path $Launcher -Row ([pscustomobject]@{sha256=$Run.Value.launcher_sha256})
$VerifyRoot = Join-Path ([IO.Path]::GetTempPath()) ("oms-task28-verify-{0}" -f [Guid]::NewGuid().ToString("N"))
New-Item -ItemType Directory -Path $VerifyRoot | Out-Null
try {
  $Previous = $env:OMS_TASK28_COMPOSITION_VERIFY_ROOT
  $PreviousVerify = $env:OMS_TASK28_COMPOSITION_VERIFY
  $env:OMS_TASK28_COMPOSITION_VERIFY_ROOT = $VerifyRoot
  $env:OMS_TASK28_COMPOSITION_VERIFY = "1"
  & $Controller -Manifest $Run.Path
  if ($LASTEXITCODE -ne 1) { throw "Synthetic controller path did not return blocked evidence." }
  $Result = Join-Path $VerifyRoot "evidence/result.json"
  $Status = Join-Path $VerifyRoot "evidence/status.json"
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
  $env:OMS_TASK28_COMPOSITION_VERIFY_ROOT = $Previous
  $env:OMS_TASK28_COMPOSITION_VERIFY = $PreviousVerify
  Remove-Item -LiteralPath $VerifyRoot -Recurse -Force -ErrorAction SilentlyContinue
}
