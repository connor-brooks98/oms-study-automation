[CmdletBinding()]
param([Parameter(Mandatory = $true)][string]$Manifest)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
. (Join-Path $PSScriptRoot "private-shadow-common.ps1")

$Run = Read-BoundRunManifest -Path $Manifest
$Manifest = $Run.Path
$Bundle = Assert-ImmutableBundle -Run $Run
$State = Get-Task28StatePaths -RunId ([string]$Run.Value.run_id)
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
New-Task28ProtectedState -State $State
Assert-ImmutableBundle -Run $Run | Out-Null
Assert-ImmutableBundle -Run $Run | Out-Null
& $Launcher -Manifest $Manifest
$LauncherStatus = Join-Path $State.Evidence "status.json"
if (-not (Test-Path -LiteralPath $LauncherStatus -PathType Leaf)) {
  throw "Launcher did not produce canonical status evidence."
}
try {
  $LauncherExit = [int]([IO.File]::ReadAllText($LauncherStatus,
      [Text.UTF8Encoding]::new($false, $true)) | ConvertFrom-Json).exit_code
} catch {
  throw "Launcher status evidence is invalid."
}
if ($LauncherExit -lt 0 -or $LauncherExit -gt 255) {
  throw "Launcher status exit code is invalid."
}
exit $LauncherExit
