[CmdletBinding()]
param([Parameter(Mandatory = $true)][string]$Manifest)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

function Resolve-SafeExistingFile {
  param([Parameter(Mandatory = $true)][string]$Path)
  if (-not [IO.Path]::IsPathFullyQualified($Path) -or
      -not (Test-Path -LiteralPath $Path -PathType Leaf)) {
    throw "Private-shadow path must be an absolute existing file."
  }
  $Resolved = (Get-Item -LiteralPath $Path -Force).FullName
  $Cursor = $Resolved
  while ($true) {
    if ((Get-Item -LiteralPath $Cursor -Force).Attributes -band [IO.FileAttributes]::ReparsePoint) {
      throw "Private-shadow path crossed a reparse point."
    }
    $Parent = Split-Path -Parent $Cursor
    if ([string]::IsNullOrEmpty($Parent) -or $Parent -ceq $Cursor) { return $Resolved }
    $Cursor = $Parent
  }
}

$Manifest = Resolve-SafeExistingFile -Path $Manifest
$Run = Get-Content -LiteralPath $Manifest -Raw | ConvertFrom-Json
if ($Run.schema_version -ne 1 -or $Run.authorization_count -ne 0 -or
    -not [IO.Path]::IsPathFullyQualified([string]$Run.immutable_bundle_path)) {
  throw "Private-shadow manifest is invalid."
}
$Bundle = [IO.Path]::GetFullPath([string]$Run.immutable_bundle_path)
$BundlePrefix = $Bundle.TrimEnd("\\", "/") + [IO.Path]::DirectorySeparatorChar
if (-not $Manifest.StartsWith($BundlePrefix, [StringComparison]::OrdinalIgnoreCase)) {
  throw "Private-shadow manifest escaped its immutable bundle."
}
$ExpectedController = Join-Path $Bundle "source/scripts/task28/private-shadow-controller.ps1"
$ExpectedController = Resolve-SafeExistingFile -Path $ExpectedController
$ActualController = Resolve-SafeExistingFile -Path $PSCommandPath
if ($ExpectedController -cne $ActualController -or
    (Get-FileHash -LiteralPath $ExpectedController -Algorithm SHA256).Hash.ToLowerInvariant() -cne
      [string]$Run.controller_sha256) {
  throw "Controller is not the manifest-bound immutable file."
}
$Launcher = Join-Path $Bundle "source/scripts/task28/private-shadow-launcher.ps1"
$Launcher = Resolve-SafeExistingFile -Path $Launcher
if ((Get-FileHash -LiteralPath $Launcher -Algorithm SHA256).Hash.ToLowerInvariant() -cne
      [string]$Run.launcher_sha256) {
  throw "Launcher is not the manifest-bound immutable file."
}
& $Launcher -Manifest $Manifest
exit $LASTEXITCODE
