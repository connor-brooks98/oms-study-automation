[CmdletBinding()]
param(
  [Parameter(Mandatory = $true)]
  [string]$ReleaseScript
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest
$Tokens = $null
$ParseErrors = $null
$Ast = [Management.Automation.Language.Parser]::ParseFile(
  $ReleaseScript,
  [ref]$Tokens,
  [ref]$ParseErrors
)
if (@($ParseErrors).Count -ne 0) {
  throw "Grouped-matching release script has PowerShell parse errors."
}

foreach ($FunctionName in @(
  "Assert-NonReparsePath",
  "Assert-Leaf",
  "Assert-Directory",
  "Get-SystemPowerShellPath",
  "Assert-InstallerMutationPaths",
  "Invoke-Installer"
)) {
  $Matches = @($Ast.FindAll({
    param($Node)
    $Node -is [Management.Automation.Language.FunctionDefinitionAst] -and
      $Node.Name -ceq $FunctionName
  }, $true))
  if ($Matches.Count -ne 1) {
    throw "Expected exactly one $FunctionName definition."
  }
  . ([scriptblock]::Create($Matches[0].Extent.Text))
}

function Get-NetTCPConnection {
  [CmdletBinding()]
  param(
    [string]$State,
    [string]$LocalAddress,
    [int]$LocalPort
  )
  return @()
}

function Start-Process {
  [CmdletBinding()]
  param(
    [string]$FilePath,
    [object[]]$ArgumentList,
    [switch]$PassThru,
    [string]$WindowStyle,
    [string]$RedirectStandardOutput,
    [string]$RedirectStandardError,
    [string]$WorkingDirectory
  )
  $Parameters = @{
    FilePath = $FilePath
    ArgumentList = $ArgumentList
    PassThru = $true
    Wait = $true
    WindowStyle = $WindowStyle
    RedirectStandardOutput = $RedirectStandardOutput
    RedirectStandardError = $RedirectStandardError
  }
  if ($PSBoundParameters.ContainsKey("WorkingDirectory")) {
    $Parameters.WorkingDirectory = $WorkingDirectory
  }
  return Microsoft.PowerShell.Management\Start-Process @Parameters
}

$TestRoot = Join-Path ([IO.Path]::GetTempPath()) ("oms-installer-cwd-" + [guid]::NewGuid().ToString("N"))
$ProjectRoot = Join-Path $TestRoot "project-root"
$SshWorkingDirectory = Join-Path $TestRoot "ssh-cwd"
$DataRoot = Join-Path $TestRoot "data-root"
$Installer = Join-Path $ProjectRoot "capture-installer.ps1"
$CapturePath = Join-Path $DataRoot "captured-cwd.txt"
$PreviousCapturePath = [Environment]::GetEnvironmentVariable("OMS_INSTALLER_CWD_CAPTURE", "Process")

try {
  New-Item -ItemType Directory -Path $ProjectRoot, $SshWorkingDirectory, $DataRoot, (Join-Path $ProjectRoot ".venv") -Force | Out-Null
  @'
param(
  [string]$ProjectRoot,
  [string]$DataRoot,
  [switch]$WhatIf
)
(Get-Location).Path | Set-Content -LiteralPath $env:OMS_INSTALLER_CWD_CAPTURE -NoNewline -Encoding UTF8
'@ | Set-Content -LiteralPath $Installer -NoNewline -Encoding UTF8
  [Environment]::SetEnvironmentVariable("OMS_INSTALLER_CWD_CAPTURE", $CapturePath, "Process")

  Push-Location -LiteralPath $SshWorkingDirectory
  try {
    Invoke-Installer ([pscustomobject]@{ data_root = $DataRoot; port = 65534 }) -WhatIf
  } finally {
    Pop-Location
  }

  if (-not (Test-Path -LiteralPath $CapturePath -PathType Leaf)) {
    throw "Installer child did not capture its working directory."
  }
  $Observed = [IO.Path]::GetFullPath((Get-Content -LiteralPath $CapturePath -Raw).Trim()).TrimEnd("\")
  $Expected = [IO.Path]::GetFullPath($ProjectRoot).TrimEnd("\")
  if (-not [string]::Equals($Observed, $Expected, [StringComparison]::OrdinalIgnoreCase)) {
    throw "Installer child working directory differed: expected $Expected, observed $Observed."
  }
  Write-Output "GROUPED_MATCHING_INSTALLER_WORKING_DIRECTORY_VERIFIED"
} finally {
  [Environment]::SetEnvironmentVariable("OMS_INSTALLER_CWD_CAPTURE", $PreviousCapturePath, "Process")
  if (Test-Path -LiteralPath $TestRoot) {
    Remove-Item -LiteralPath $TestRoot -Recurse -Force -ErrorAction Stop
  }
}
