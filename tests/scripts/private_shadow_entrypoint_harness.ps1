[CmdletBinding()]
param(
  [Parameter(Mandatory = $true)][string]$EntryPoint,
  [Parameter(Mandatory = $true)][string]$FixtureScript,
  [Parameter(Mandatory = $true)][string]$EvidenceScript,
  [Parameter(Mandatory = $true)][string]$PythonExecutable
)

$ErrorActionPreference = "Stop"
$Sandbox = Join-Path ([IO.Path]::GetTempPath()) (
  "oms-private-shadow-entrypoint-{0}" -f [Guid]::NewGuid().ToString("N")
)
$Utf8 = [Text.UTF8Encoding]::new($false, $true)
New-Item -ItemType Directory -Path $Sandbox | Out-Null
. $EvidenceScript

try {
  foreach ($Mode in @("corrected", "fallback", "close_failure", "cleanup_failure")) {
    $Raw = Join-Path $Sandbox "$Mode.raw.json"
    $Safe = Join-Path $Sandbox "$Mode.safe.json"
    $Start = [Diagnostics.ProcessStartInfo]::new()
    $Start.FileName = $PythonExecutable
    $Start.Arguments = (
      '"' + $FixtureScript + '" --entrypoint "' + $EntryPoint + '" --mode ' + $Mode
    )
    $Start.UseShellExecute = $false
    $Start.CreateNoWindow = $true
    $Start.RedirectStandardOutput = $true
    $Start.RedirectStandardError = $true
    $Start.StandardOutputEncoding = $Utf8
    $Start.StandardErrorEncoding = $Utf8
    $Process = [Diagnostics.Process]::new()
    $Process.StartInfo = $Start
    if (-not $Process.Start()) { throw "Entrypoint fixture did not start." }
    $Stdout = $Process.StandardOutput.ReadToEndAsync()
    $Stderr = $Process.StandardError.ReadToEndAsync()
    $Process.WaitForExit()
    $Output = $Stdout.GetAwaiter().GetResult()
    $ErrorOutput = $Stderr.GetAwaiter().GetResult()
    if ($Process.ExitCode -ne 1 -or -not [string]::IsNullOrEmpty($ErrorOutput)) {
      throw "Entrypoint fixture failed unexpectedly."
    }
    [IO.File]::WriteAllText($Raw, $Output, $Utf8)
    $Result = Convert-PrivateShadowEvidence `
      -RawStdoutPath $Raw -SafeResultPath $Safe -ProcessExitCode $Process.ExitCode
    if ($Result.ExitCode -ne 0 -or $Result.Stage -cne "complete" -or
        -not $Result.EvidenceUsable -or -not (Test-Path -LiteralPath $Safe -PathType Leaf)) {
      throw "Entrypoint evidence did not pass the real validator."
    }
    $Record = [IO.File]::ReadAllText($Safe, $Utf8) | ConvertFrom-Json
    if (@($Record.PSObject.Properties).Count -ne 15 -or
        $Record.failure_input_identity -cne "none" -or
        $Record.provider_error_category -cne "none" -or
        $null -ne $Record.provider_status_code -or $Record.provider_reason -cne "none") {
      throw "Entrypoint evidence schema was not the corrected 15-key contract."
    }
  }
  Write-Output "PRIVATE_SHADOW_ENTRYPOINT_HARNESS_VERIFIED"
} finally {
  if (Test-Path -LiteralPath $Sandbox) {
    Remove-Item -LiteralPath $Sandbox -Recurse -Force
  }
}
