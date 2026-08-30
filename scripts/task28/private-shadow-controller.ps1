[CmdletBinding()]
param([Parameter(Mandatory = $true)][string]$Manifest)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
. (Join-Path $PSScriptRoot "private-shadow-common.ps1")

$Run = Read-BoundRunManifest -Path $Manifest
$Manifest = $Run.Path
$Bundle = Assert-ImmutableBundle -Run $Run
$ExpectedController = Join-Path $Bundle "source/scripts/task28/private-shadow-controller.ps1"
$ExpectedController = Resolve-Task28ExistingPath -Path $ExpectedController -Type Leaf
$ActualController = Resolve-Task28ExistingPath -Path $PSCommandPath -Type Leaf
if (-not [string]::Equals($ExpectedController, $ActualController, [StringComparison]::OrdinalIgnoreCase) -or
    -not [string]::Equals((Get-FileHash -LiteralPath $ExpectedController -Algorithm SHA256).Hash.ToLowerInvariant(),
      [string]$Run.Value.controller_sha256, [StringComparison]::OrdinalIgnoreCase)) {
  throw "Controller is not the manifest-bound immutable file."
}
$Launcher = Join-Path $Bundle "source/scripts/task28/private-shadow-launcher.ps1"
$Launcher = Resolve-Task28ExistingPath -Path $Launcher -Type Leaf
if (-not [string]::Equals((Get-FileHash -LiteralPath $Launcher -Algorithm SHA256).Hash.ToLowerInvariant(),
      [string]$Run.Value.launcher_sha256, [StringComparison]::OrdinalIgnoreCase)) {
  throw "Launcher is not the manifest-bound immutable file."
}
& $Launcher -Manifest $Manifest
exit $LASTEXITCODE
