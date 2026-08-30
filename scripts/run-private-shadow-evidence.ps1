[CmdletBinding()]
param(
  [Parameter(Mandatory = $true)][string]$PythonExecutable,
  [Parameter(Mandatory = $true)][string]$ProjectRoot,
  [Parameter(Mandatory = $true)][string]$OperatorScript,
  [Parameter(Mandatory = $true)][string]$DiagnosticRoot,
  [Parameter(Mandatory = $true)][string]$SafeResultPath,
  [Parameter(Mandatory = $true)][string]$SafeStatusPath,
  [Parameter(Mandatory = $true)][switch]$CompositionVerify
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
$Utf8 = [System.Text.UTF8Encoding]::new($false, $true)
$ProjectRoot = [System.IO.Path]::GetFullPath($ProjectRoot)
$DiagnosticRoot = [System.IO.Path]::GetFullPath($DiagnosticRoot)
$SafeResultPath = [System.IO.Path]::GetFullPath($SafeResultPath)
$SafeStatusPath = [System.IO.Path]::GetFullPath($SafeStatusPath)
$EvidenceScript = Join-Path $PSScriptRoot "private-shadow-evidence.ps1"
$RawStdout = Join-Path $DiagnosticRoot "operator.stdout"
$RawStderr = Join-Path $DiagnosticRoot "operator.stderr"
$ExitCode = 54
$WrapperStage = "bootstrap"
$EvidenceUsable = $false
$OperatorArtifactsDeleted = $false

function Set-CompositionVerifyEnvironment {
  param([Parameter(Mandatory = $true)][Diagnostics.ProcessStartInfo]$ProcessInfo)
  $ProcessInfo.EnvironmentVariables.Clear()
  foreach ($Name in @("SystemRoot", "WINDIR", "ComSpec", "PATH", "TEMP", "TMP")) {
    $Value = [Environment]::GetEnvironmentVariable($Name)
    if ($Value) { $ProcessInfo.EnvironmentVariables[$Name] = $Value }
  }
  $ProcessInfo.EnvironmentVariables["OMS_TASK28_COMPOSITION_VERIFY"] = "1"
  $ProcessInfo.EnvironmentVariables["OMS_TASK28_PRIVATE_PROJECT"] = $ProjectRoot
  $ProcessInfo.EnvironmentVariables["PYTHONPATH"] = $SourceRoot
}

function Protect-PrivateShadowDirectory {
  param([Parameter(Mandatory = $true)][string]$Path)
  $Identity = & whoami.exe /user /fo csv /nh | ConvertFrom-Csv -Header Name,Sid
  if ($LASTEXITCODE -ne 0 -or $Identity.Sid -notmatch '^S-1(?:-\d+)+$') {
    throw "Current Windows SID was unavailable."
  }
  & icacls.exe $Path /inheritance:r /grant:r "*$($Identity.Sid):(OI)(CI)F" | Out-Null
  if ($LASTEXITCODE -ne 0) {
    throw "Private-shadow DACL initialization failed."
  }
  & icacls.exe $Path /verify | Out-Null
  if ($LASTEXITCODE -ne 0) {
    throw "Private-shadow DACL verification failed."
  }
  $Acl = Get-Acl -LiteralPath $Path
  $Rules = @($Acl.GetAccessRules(
      $true, $false, [System.Security.Principal.SecurityIdentifier]
  ))
  $RequiredRights = [System.Security.AccessControl.FileSystemRights]::FullControl
  $RequiredInheritance =
    [System.Security.AccessControl.InheritanceFlags]::ContainerInherit -bor
    [System.Security.AccessControl.InheritanceFlags]::ObjectInherit
  if (-not $Acl.AreAccessRulesProtected -or $Rules.Count -ne 1 -or
      $Rules[0].IdentityReference.Value -cne $Identity.Sid -or
      $Rules[0].AccessControlType -ne
        [System.Security.AccessControl.AccessControlType]::Allow -or
      $Rules[0].IsInherited -or
      ($Rules[0].FileSystemRights -band $RequiredRights) -ne $RequiredRights -or
      ($Rules[0].InheritanceFlags -band $RequiredInheritance) -ne
        $RequiredInheritance -or
      $Rules[0].PropagationFlags -ne
        [System.Security.AccessControl.PropagationFlags]::None) {
    throw "Private-shadow DACL was not current-user-only."
  }
}

function Resolve-PrivateShadowSafePath {
  param(
    [Parameter(Mandatory = $true)][string]$Path,
    [switch]$ExistingLeaf,
    [switch]$ExistingContainer
  )
  $FullPath = [System.IO.Path]::GetFullPath($Path)
  $ExistingPath = if ($ExistingLeaf -or $ExistingContainer) {
    $FullPath
  } else {
    Split-Path -Parent $FullPath
  }
  $RequiredType = if ($ExistingLeaf) {"Leaf"} else {"Container"}
  if (-not (Test-Path -LiteralPath $ExistingPath -PathType $RequiredType)) {
    throw "Private-shadow path input was unavailable."
  }
  $Cursor = $ExistingPath
  while (-not [string]::IsNullOrEmpty($Cursor)) {
    $Item = Get-Item -LiteralPath $Cursor -Force
    if ($Item.Attributes -band [System.IO.FileAttributes]::ReparsePoint) {
      throw "Private-shadow path crossed a reparse point."
    }
    $Next = Split-Path -Parent $Cursor
    if ([string]::IsNullOrEmpty($Next) -or $Next -ceq $Cursor) { break }
    $Cursor = $Next
  }
  $CanonicalExisting = (Get-Item -LiteralPath $ExistingPath -Force).FullName
  if ($ExistingLeaf -or $ExistingContainer) {
    return [System.IO.Path]::GetFullPath($CanonicalExisting)
  }
  return [System.IO.Path]::GetFullPath(
    (Join-Path $CanonicalExisting (Split-Path -Leaf $FullPath))
  )
}

function Assert-PrivateShadowPathParents {
  foreach ($Path in @($SafeResultPath, $SafeStatusPath)) {
    $Parent = Split-Path -Parent $Path
    if (-not (Test-Path -LiteralPath $Parent -PathType Container)) {
      throw "Private-shadow output parent was unavailable."
    }
    $Cursor = $Parent
    while (-not [string]::IsNullOrEmpty($Cursor)) {
      $Item = Get-Item -LiteralPath $Cursor -Force
      if ($Item.Attributes -band [System.IO.FileAttributes]::ReparsePoint) {
        throw "Private-shadow output path crossed a reparse point."
      }
      $Next = Split-Path -Parent $Cursor
      if ([string]::IsNullOrEmpty($Next) -or $Next -ceq $Cursor) { break }
      $Cursor = $Next
    }
  }
  if ((Test-Path -LiteralPath $SafeResultPath) -or
      (Test-Path -LiteralPath $SafeStatusPath)) {
    throw "Private-shadow output destination already exists."
  }
}

function Write-PrivateShadowStatus {
  param([Parameter(Mandatory = $true)][object]$Record)
  $Parent = Split-Path -Parent $SafeStatusPath
  $Temporary = Join-Path $Parent (".{0}.tmp" -f [Guid]::NewGuid().ToString("N"))
  try {
    $Payload = $Record | ConvertTo-Json -Compress -Depth 4
    [System.IO.File]::WriteAllText($Temporary, $Payload + "`n", $Utf8)
    [System.IO.File]::Move($Temporary, $SafeStatusPath)
  } finally {
    Remove-Item -LiteralPath $Temporary -Force -ErrorAction SilentlyContinue
  }
}

try {
  $PythonExecutable = Resolve-PrivateShadowSafePath `
    -Path $PythonExecutable -ExistingLeaf
  $ProjectRoot = Resolve-PrivateShadowSafePath `
    -Path $ProjectRoot -ExistingContainer
  $SourceRoot = Resolve-PrivateShadowSafePath `
    -Path (Join-Path $ProjectRoot "src") -ExistingContainer
  $EvidenceModule = Resolve-PrivateShadowSafePath `
    -Path (Join-Path $SourceRoot "oms_hub/providers/gemini/evidence.py") -ExistingLeaf
  $OperatorScript = Resolve-PrivateShadowSafePath `
    -Path $OperatorScript -ExistingLeaf
  $EvidenceScript = Resolve-PrivateShadowSafePath `
    -Path $EvidenceScript -ExistingLeaf
  $DiagnosticRoot = Resolve-PrivateShadowSafePath -Path $DiagnosticRoot
  $SafeResultPath = Resolve-PrivateShadowSafePath -Path $SafeResultPath
  $SafeStatusPath = Resolve-PrivateShadowSafePath -Path $SafeStatusPath
  $ProjectPrefix = $ProjectRoot.TrimEnd('\', '/') +
    [System.IO.Path]::DirectorySeparatorChar
  if (-not $OperatorScript.StartsWith(
      $ProjectPrefix, [System.StringComparison]::OrdinalIgnoreCase
  )) {
    throw "Private-shadow operator must be inside the project root."
  }
  if (-not $SourceRoot.StartsWith(
      $ProjectPrefix, [System.StringComparison]::OrdinalIgnoreCase
  )) {
    throw "Private-shadow source root must be inside the project root."
  }
  $SourcePrefix = $SourceRoot.TrimEnd('\\', '/') +
    [System.IO.Path]::DirectorySeparatorChar
  if (-not $EvidenceModule.StartsWith(
      $SourcePrefix, [System.StringComparison]::OrdinalIgnoreCase
  )) {
    throw "Private-shadow evidence module must be inside the source root."
  }
  if ($DiagnosticRoot -ceq $ProjectRoot -or $DiagnosticRoot.StartsWith(
      $ProjectPrefix,
      [System.StringComparison]::OrdinalIgnoreCase
  )) {
    throw "Private-shadow diagnostic root must be outside the project root."
  }
  $RawStdout = Join-Path $DiagnosticRoot "operator.stdout"
  $RawStderr = Join-Path $DiagnosticRoot "operator.stderr"
  Assert-PrivateShadowPathParents
  if (Test-Path -LiteralPath $DiagnosticRoot) {
    throw "Private-shadow diagnostic root must be newly created."
  }
  New-Item -ItemType Directory -Path $DiagnosticRoot | Out-Null
  if ((Get-Item -LiteralPath $DiagnosticRoot -Force).Attributes -band
      [System.IO.FileAttributes]::ReparsePoint) {
    throw "Private-shadow diagnostic root was a reparse point."
  }
  Protect-PrivateShadowDirectory -Path $DiagnosticRoot
  . $EvidenceScript

  $WrapperStage = "operator"
  $ProcessInfo = [System.Diagnostics.ProcessStartInfo]::new()
  $ProcessInfo.FileName = $PythonExecutable
  $ProcessInfo.Arguments = '"' + $OperatorScript.Replace('"', '\"') + '"'
  $ProcessInfo.WorkingDirectory = Split-Path -Parent $OperatorScript
  $ProcessInfo.UseShellExecute = $false
  $ProcessInfo.CreateNoWindow = $true
  $ProcessInfo.RedirectStandardOutput = $true
  $ProcessInfo.RedirectStandardError = $true
  $ProcessInfo.StandardOutputEncoding = $Utf8
  $ProcessInfo.StandardErrorEncoding = $Utf8
  Set-CompositionVerifyEnvironment -ProcessInfo $ProcessInfo
  $Process = [System.Diagnostics.Process]::new()
  $Process.StartInfo = $ProcessInfo
  if (-not $Process.Start()) {
    throw "Private-shadow operator did not start."
  }
  $StdoutTask = $Process.StandardOutput.ReadToEndAsync()
  $StderrTask = $Process.StandardError.ReadToEndAsync()
  $Process.WaitForExit()
  $OperatorExit = $Process.ExitCode
  $Stdout = $StdoutTask.GetAwaiter().GetResult()
  $Stderr = $StderrTask.GetAwaiter().GetResult()
  if ($Utf8.GetByteCount($Stdout) -gt 1048576 -or
      $Utf8.GetByteCount($Stderr) -gt 1048576) {
    $ExitCode = 51
    $WrapperStage = "parse"
  } else {
    [System.IO.File]::WriteAllText($RawStdout, $Stdout, $Utf8)
    [System.IO.File]::WriteAllText($RawStderr, $Stderr, $Utf8)
    $WrapperStage = "evidence"
    $Evidence = Convert-PrivateShadowEvidence `
      -RawStdoutPath $RawStdout -SafeResultPath $SafeResultPath `
      -ProcessExitCode $OperatorExit -PythonExecutable $PythonExecutable `
      -SourceRoot $SourceRoot -EvidenceModule $EvidenceModule
    $EvidenceUsable = $Evidence.EvidenceUsable
    $WrapperStage = $Evidence.Stage
    $ExitCode = if ($Evidence.ExitCode -ne 0) {$Evidence.ExitCode} else {$OperatorExit}
  }
} catch {
  $EvidenceUsable = $false
} finally {
  if (Test-Path -LiteralPath $DiagnosticRoot) {
    Remove-Item -LiteralPath $DiagnosticRoot -Recurse -Force -ErrorAction SilentlyContinue
  }
  $OperatorArtifactsDeleted = -not (Test-Path -LiteralPath $DiagnosticRoot)
  if (-not $OperatorArtifactsDeleted) {
    $EvidenceUsable = $false
    $WrapperStage = "cleanup"
    $ExitCode = 54
    Remove-Item -LiteralPath $SafeResultPath -Force -ErrorAction SilentlyContinue
  }
  $Status = [ordered]@{
    schema_version = 1
    wrapper_stage = $WrapperStage
    exit_code = $ExitCode
    evidence_usable = $EvidenceUsable
    operator_artifacts_deleted = $OperatorArtifactsDeleted
    raw_content_retained = (-not $OperatorArtifactsDeleted)
  }
  Write-PrivateShadowStatus -Record $Status
}

exit $ExitCode
