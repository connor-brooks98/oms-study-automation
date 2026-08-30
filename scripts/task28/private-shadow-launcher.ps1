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
$State = Get-Task28StatePaths -RunId ([string]$Run.Value.run_id)
Assert-Task28ProtectedState -State $State
Assert-ImmutableBundle -Run $Run | Out-Null
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
$EvidenceRoot = $State.Evidence
$ExitCode = 54
try {
  & $Wrapper -CompositionVerify -PythonExecutable ([string]$Run.Value.python_executable) -ProjectRoot $Source `
      -OperatorScript $EntryPoint -StateRoot $State.Root -DiagnosticRoot $State.Diagnostic `
      -SafeResultPath (Join-Path $EvidenceRoot "result.json") `
      -SafeStatusPath (Join-Path $EvidenceRoot "status.json")
  $ExitCode = $LASTEXITCODE
} finally {
  Remove-Item -LiteralPath $State.Scratch -Recurse -Force -ErrorAction SilentlyContinue
  if (Test-Path -LiteralPath $State.Scratch) { throw "Task28 scratch cleanup failed." }
}
exit $ExitCode
