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
    -not [IO.Path]::IsPathFullyQualified([string]$Run.immutable_bundle_path) -or
    -not [IO.Path]::IsPathFullyQualified([string]$Run.mutable_state_path)) {
  throw "Private-shadow manifest is invalid."
}
$Bundle = [IO.Path]::GetFullPath([string]$Run.immutable_bundle_path)
$Source = Join-Path $Bundle "source"
$BundlePrefix = $Bundle.TrimEnd("\\", "/") + [IO.Path]::DirectorySeparatorChar
if (-not $Manifest.StartsWith($BundlePrefix, [StringComparison]::OrdinalIgnoreCase)) {
  throw "Private-shadow manifest escaped its immutable bundle."
}
$ExpectedLauncher = Join-Path $Source "scripts/task28/private-shadow-launcher.ps1"
$ExpectedLauncher = Resolve-SafeExistingFile -Path $ExpectedLauncher
$ActualLauncher = Resolve-SafeExistingFile -Path $PSCommandPath
if ($ExpectedLauncher -cne $ActualLauncher -or
    (Get-FileHash -LiteralPath $ExpectedLauncher -Algorithm SHA256).Hash.ToLowerInvariant() -cne
      [string]$Run.launcher_sha256) {
  throw "Launcher is not the manifest-bound immutable file."
}
if (Test-Path -LiteralPath ([string]$Run.mutable_state_path)) {
  throw "Correction 1 must not create mutable state."
}
$VerifyRoot = $env:OMS_TASK28_COMPOSITION_VERIFY_ROOT
if ([string]::IsNullOrWhiteSpace($VerifyRoot) -or -not [IO.Path]::IsPathFullyQualified($VerifyRoot) -or
    -not (Test-Path -LiteralPath $VerifyRoot -PathType Container)) {
  throw "Launcher is available only to tracked composition verification."
}
$VerifyRoot = (Get-Item -LiteralPath $VerifyRoot -Force).FullName
if ($VerifyRoot.StartsWith($BundlePrefix, [StringComparison]::OrdinalIgnoreCase)) {
  throw "Verification output must remain outside the immutable bundle."
}
$EntryPoint = Join-Path $Source "scripts/private-shadow-operator-entry.py"
$Wrapper = Join-Path $Source "scripts/run-private-shadow-evidence.ps1"
$Evidence = Join-Path $Source "src/oms_hub/providers/gemini/evidence.py"
foreach ($Pair in @(
    @($EntryPoint, [string]$Run.entrypoint_sha256),
    @($Wrapper, [string]$Run.wrapper_sha256),
    @($Evidence, [string]$Run.evidence_sha256))) {
  $Dependency = Resolve-SafeExistingFile -Path $Pair[0]
  if ((Get-FileHash -LiteralPath $Dependency -Algorithm SHA256).Hash.ToLowerInvariant() -cne $Pair[1]) {
    throw "Launcher dependency is not the manifest-bound immutable file."
  }
}
$EvidenceRoot = Join-Path $VerifyRoot "evidence"
New-Item -ItemType Directory -Path $EvidenceRoot | Out-Null
& $Wrapper -CompositionVerify -PythonExecutable ([string]$Run.python_executable) -ProjectRoot $Source `
    -OperatorScript $EntryPoint -DiagnosticRoot (Join-Path $VerifyRoot "diagnostic") `
    -SafeResultPath (Join-Path $EvidenceRoot "result.json") `
    -SafeStatusPath (Join-Path $EvidenceRoot "status.json")
$ExitCode = $LASTEXITCODE
exit $ExitCode
