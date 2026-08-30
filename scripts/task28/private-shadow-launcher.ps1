[CmdletBinding()]
param([Parameter(Mandatory = $true)][string]$Manifest)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
. (Join-Path $PSScriptRoot "private-shadow-common.ps1")

$Run = Read-BoundRunManifest -Path $Manifest
$Manifest = $Run.Path
$Bundle = Assert-ImmutableBundle -Run $Run
$Source = Join-Path $Bundle "source"
$ExpectedLauncher = Join-Path $Source "scripts/task28/private-shadow-launcher.ps1"
$ExpectedLauncher = Resolve-Task28ExistingPath -Path $ExpectedLauncher -Type Leaf
$ActualLauncher = Resolve-Task28ExistingPath -Path $PSCommandPath -Type Leaf
if (-not [string]::Equals($ExpectedLauncher, $ActualLauncher, [StringComparison]::OrdinalIgnoreCase) -or
    -not [string]::Equals((Get-FileHash -LiteralPath $ExpectedLauncher -Algorithm SHA256).Hash.ToLowerInvariant(),
      [string]$Run.Value.launcher_sha256, [StringComparison]::OrdinalIgnoreCase)) {
  throw "Launcher is not the manifest-bound immutable file."
}
if (Test-Path -LiteralPath ([string]$Run.Value.mutable_state_path)) {
  throw "Correction 1 must not create mutable state."
}
$VerifyRoot = $env:OMS_TASK28_COMPOSITION_VERIFY_ROOT
if ([string]::IsNullOrWhiteSpace($VerifyRoot) -or -not (Test-Task28FullyQualifiedPath -Path $VerifyRoot) -or
    -not (Test-Path -LiteralPath $VerifyRoot -PathType Container)) {
  throw "Launcher is available only to tracked composition verification."
}
$VerifyRoot = Resolve-Task28ExistingPath -Path $VerifyRoot -Type Container
if (Test-Task28DescendantPath -Path $VerifyRoot -Root $Bundle) {
  throw "Verification output must remain outside the immutable bundle."
}
$EntryPoint = Join-Path $Source "scripts/private-shadow-operator-entry.py"
$Wrapper = Join-Path $Source "scripts/run-private-shadow-evidence.ps1"
$Evidence = Join-Path $Source "src/oms_hub/providers/gemini/evidence.py"
foreach ($Pair in @(
    @($EntryPoint, [string]$Run.Value.entrypoint_sha256),
    @($Wrapper, [string]$Run.Value.wrapper_sha256),
    @($Evidence, [string]$Run.Value.evidence_sha256))) {
  $Dependency = Resolve-Task28ExistingPath -Path $Pair[0] -Type Leaf
  if (-not [string]::Equals((Get-FileHash -LiteralPath $Dependency -Algorithm SHA256).Hash.ToLowerInvariant(), $Pair[1], [StringComparison]::OrdinalIgnoreCase)) {
    throw "Launcher dependency is not the manifest-bound immutable file."
  }
}
$EvidenceRoot = Join-Path $VerifyRoot "evidence"
New-Item -ItemType Directory -Path $EvidenceRoot | Out-Null
& $Wrapper -CompositionVerify -PythonExecutable ([string]$Run.Value.python_executable) -ProjectRoot $Source `
    -OperatorScript $EntryPoint -DiagnosticRoot (Join-Path $VerifyRoot "diagnostic") `
    -SafeResultPath (Join-Path $EvidenceRoot "result.json") `
    -SafeStatusPath (Join-Path $EvidenceRoot "status.json")
$ExitCode = $LASTEXITCODE
exit $ExitCode
