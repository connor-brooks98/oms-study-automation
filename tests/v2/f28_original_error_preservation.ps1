[CmdletBinding()]
param(
  [Parameter(Mandatory = $true)]
  [string]$AcceptanceScript
)

$ErrorActionPreference = "Stop"
$Tokens = $null
$ParseErrors = $null
$Ast = [System.Management.Automation.Language.Parser]::ParseFile(
  $AcceptanceScript,
  [ref]$Tokens,
  [ref]$ParseErrors
)
if (@($ParseErrors).Count -ne 0) {
  throw "F28 acceptance script has PowerShell parse errors."
}

foreach ($FunctionName in @(
  "Write-F28Diagnostic",
  "Invoke-F28FailurePreservingAction",
  "Test-JsonInteger",
  "Test-ExactJsonInteger"
)) {
  $Matches = @($Ast.FindAll({
    param($Node)
    $Node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and
      $Node.Name -ceq $FunctionName
  }, $true))
  if ($Matches.Count -ne 1) {
    throw "Expected exactly one $FunctionName definition."
  }
  . ([scriptblock]::Create($Matches[0].Extent.Text))
}

$PrimaryMessage = "F28_TEST_PRIMARY_ACCEPTANCE_FAILURE"
$ProbeMessage = "F28_TEST_PATH_PROBE_FAILURE"
function Test-Path {
  param([string]$LiteralPath, [string]$PathType)
  throw $ProbeMessage
}
$PrimaryFailure = $null
try {
  throw $PrimaryMessage
} catch {
  $PrimaryFailure = $_
}

$ObservedFailure = $null
try {
  Invoke-F28FailurePreservingAction `
    -Action {
      Test-Path `
        -LiteralPath "C:\injected-inaccessible-f28-request.json" `
        -PathType Leaf
    } `
    -Diagnostic "Expected injected path-probe failure."
  throw $PrimaryFailure
} catch {
  $ObservedFailure = $_
}

if ($null -eq $ObservedFailure) {
  throw "The original acceptance failure was not rethrown."
}
if ($ObservedFailure.Exception.Message -cne $PrimaryMessage) {
  throw "A secondary path-probe failure replaced the original acceptance failure."
}
if ($ObservedFailure.Exception.Message -ceq $ProbeMessage) {
  throw "The injected path-probe failure escaped its protection boundary."
}

Write-Output "F28_ORIGINAL_ERROR_PRESERVATION_VERIFIED"

foreach ($IntegerValue in @(
  [System.SByte]75,
  [System.Byte]75,
  [System.Int16]75,
  [System.UInt16]75,
  [System.Int32]75,
  [System.UInt32]75,
  [System.Int64]75,
  [System.UInt64]75
)) {
  if (-not (Test-JsonInteger -Value $IntegerValue)) {
    throw "A supported CLR JSON integer type was rejected."
  }
}

$JsonValues = '{"numeric":75,"quoted":"75"}' | ConvertFrom-Json
if (-not (Test-ExactJsonInteger -Value $JsonValues.numeric -Expected 75)) {
  throw "A native ConvertFrom-Json integer was rejected."
}
if (Test-ExactJsonInteger -Value $JsonValues.quoted -Expected 75) {
  throw "A quoted JSON numeric value was accepted."
}

Write-Output "F28_JSON_INTEGER_TYPES_VERIFIED"
