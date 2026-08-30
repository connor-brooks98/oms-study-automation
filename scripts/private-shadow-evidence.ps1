$script:PrivateShadowParseExit = 51
$script:PrivateShadowValidationExit = 52
$script:PrivateShadowWriteExit = 53
$script:PrivateShadowUtf8 = [System.Text.UTF8Encoding]::new($false, $true)

function New-PrivateShadowEvidenceResult {
  param([int]$ExitCode, [string]$Stage, [bool]$EvidenceUsable)
  [pscustomobject]@{ExitCode=$ExitCode; Stage=$Stage; EvidenceUsable=$EvidenceUsable}
}

function Write-PrivateShadowSafeResult {
  param([string]$Path, [string]$Payload)
  if (Test-Path -LiteralPath $Path) { throw "Private-shadow safe result destination already exists." }
  $Parent = Split-Path -Parent $Path
  if (-not (Test-Path -LiteralPath $Parent -PathType Container)) {
    throw "Private-shadow safe result parent does not exist."
  }
  $Temporary = Join-Path $Parent (".{0}.tmp" -f [Guid]::NewGuid().ToString("N"))
  try {
    [IO.File]::WriteAllText($Temporary, $Payload, $script:PrivateShadowUtf8)
    [IO.File]::Move($Temporary, $Path)
  } finally {
    Remove-Item -LiteralPath $Temporary -Force -ErrorAction SilentlyContinue
  }
}

function Set-PrivateShadowChildEnvironment {
  param(
    [Parameter(Mandatory = $true)][Diagnostics.ProcessStartInfo]$ProcessInfo,
    [Parameter(Mandatory = $true)][string]$SourceRoot,
    [Parameter(Mandatory = $true)][string]$ScratchRoot,
    [switch]$Sanitize,
    [switch]$CompositionVerify,
    [string]$ProjectRoot
  )
  if ($Sanitize) {
    $ProcessInfo.EnvironmentVariables.Clear()
    foreach ($Name in @("SystemRoot", "WINDIR", "ComSpec", "PATH")) {
      $Value = [Environment]::GetEnvironmentVariable($Name)
      if ($Value) { $ProcessInfo.EnvironmentVariables[$Name] = $Value }
    }
  }
  $ProcessInfo.EnvironmentVariables["TEMP"] = $ScratchRoot
  $ProcessInfo.EnvironmentVariables["TMP"] = $ScratchRoot
  $ProcessInfo.EnvironmentVariables["PYTHONDONTWRITEBYTECODE"] = "1"
  $ProcessInfo.EnvironmentVariables["PYTHONPATH"] = $SourceRoot
  if ($CompositionVerify) {
    if ([string]::IsNullOrWhiteSpace($ProjectRoot)) { throw "Composition project root is unavailable." }
    $ProcessInfo.EnvironmentVariables["OMS_TASK28_COMPOSITION_VERIFY"] = "1"
    $ProcessInfo.EnvironmentVariables["OMS_TASK28_PRIVATE_PROJECT"] = $ProjectRoot
  }
}

function Convert-PrivateShadowEvidence {
  [CmdletBinding()]
  param(
    [Parameter(Mandatory = $true)][string]$RawStdoutPath,
    [Parameter(Mandatory = $true)][string]$SafeResultPath,
    [Parameter(Mandatory = $true)][int]$ProcessExitCode,
    [Parameter(Mandatory = $true)][string]$PythonExecutable,
    [Parameter(Mandatory = $true)][string]$SourceRoot,
    [Parameter(Mandatory = $true)][string]$EvidenceModule,
    [Parameter(Mandatory = $true)][string]$ScratchRoot
  )
  try {
    $Raw = [IO.File]::ReadAllText($RawStdoutPath, $script:PrivateShadowUtf8).TrimEnd("`r", "`n")
    if ([string]::IsNullOrWhiteSpace($Raw) -or $Raw -match "`r|`n") {
      throw "Private-shadow stdout was not one JSON record."
    }
  } catch {
    return New-PrivateShadowEvidenceResult $script:PrivateShadowParseExit "parse" $false
  }
  try {
    $ExpectedEvidenceModule = [IO.Path]::GetFullPath(
      (Join-Path $SourceRoot "oms_hub/providers/gemini/evidence.py")
    )
    if (-not [string]::Equals(
        [IO.Path]::GetFullPath($EvidenceModule), $ExpectedEvidenceModule,
        [System.StringComparison]::OrdinalIgnoreCase
    )) {
      return New-PrivateShadowEvidenceResult $script:PrivateShadowValidationExit "validation" $false
    }
    $Start = [Diagnostics.ProcessStartInfo]::new()
    $Start.FileName = $PythonExecutable
    $Start.Arguments = "-m oms_hub.providers.gemini.evidence --process-exit-code $ProcessExitCode"
    $Start.WorkingDirectory = $SourceRoot
    $Start.UseShellExecute = $false
    $Start.CreateNoWindow = $true
    $Start.RedirectStandardInput = $true
    $Start.RedirectStandardOutput = $true
    $Start.RedirectStandardError = $true
    $Start.StandardInputEncoding = $script:PrivateShadowUtf8
    $Start.StandardOutputEncoding = $script:PrivateShadowUtf8
    $Start.StandardErrorEncoding = $script:PrivateShadowUtf8
    Set-PrivateShadowChildEnvironment -ProcessInfo $Start -SourceRoot $SourceRoot `
      -ScratchRoot $ScratchRoot -Sanitize
    $Process = [Diagnostics.Process]::new()
    $Process.StartInfo = $Start
    if (-not $Process.Start()) { throw "Private-shadow evidence validator did not start." }
    $Process.StandardInput.Write($Raw)
    $Process.StandardInput.Close()
    $Stdout = $Process.StandardOutput.ReadToEndAsync()
    $Stderr = $Process.StandardError.ReadToEndAsync()
    $Process.WaitForExit()
    $Payload = $Stdout.GetAwaiter().GetResult()
    $ErrorOutput = $Stderr.GetAwaiter().GetResult()
    if ($Process.ExitCode -ne 0) {
      $ExitCode = if ($Process.ExitCode -in @(
          $script:PrivateShadowParseExit, $script:PrivateShadowValidationExit
        )) {$Process.ExitCode} else {$script:PrivateShadowValidationExit}
      return New-PrivateShadowEvidenceResult $ExitCode "validation" $false
    }
    if (-not [string]::IsNullOrEmpty($ErrorOutput) -or
        [string]::IsNullOrWhiteSpace($Payload) -or $Payload -notmatch "\n\z" -or
        $Payload.TrimEnd("`r", "`n") -match "`r|`n") {
      return New-PrivateShadowEvidenceResult $script:PrivateShadowValidationExit "validation" $false
    }
  } catch {
    return New-PrivateShadowEvidenceResult $script:PrivateShadowValidationExit "validation" $false
  }
  try {
    Write-PrivateShadowSafeResult -Path $SafeResultPath -Payload $Payload
  } catch {
    return New-PrivateShadowEvidenceResult $script:PrivateShadowWriteExit "safe_result_write" $false
  }
  return New-PrivateShadowEvidenceResult 0 "complete" $true
}
