[CmdletBinding()]
param(
  [Parameter(Mandatory = $true)]
  [string]$InstallerScript
)

$ErrorActionPreference = "Stop"
$Tokens = $null
$ParseErrors = $null
$Ast = [System.Management.Automation.Language.Parser]::ParseFile(
  $InstallerScript,
  [ref]$Tokens,
  [ref]$ParseErrors
)
if (@($ParseErrors).Count -ne 0) {
  throw "F28 installer script has PowerShell parse errors."
}

$Matches = @($Ast.FindAll({
  param($Node)
  $Node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and
    $Node.Name -ceq "Initialize-F28GateDirectory"
}, $true))
if ($Matches.Count -ne 1) {
  throw "Expected exactly one Initialize-F28GateDirectory definition."
}
. ([scriptblock]::Create($Matches[0].Extent.Text))

$Sandbox = Join-Path ([System.IO.Path]::GetTempPath()) (
  "oms-f28-acl-{0}" -f [Guid]::NewGuid().ToString("N")
)
$DataRoot = Join-Path $Sandbox "data"
$GateDirectory = Join-Path $DataRoot "acceptance\f28"
New-Item -ItemType Directory -Force -Path $GateDirectory | Out-Null

try {
  $Identity = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
  $Directory = Get-Item -LiteralPath $GateDirectory -Force
  $Before = $Directory.GetAccessControl()
  $OwnerBefore = $Before.Owner
  $AuditCountBefore = @($Before.Audit).Count

  Initialize-F28GateDirectory -ExpectedDataRoot $DataRoot -Identity $Identity

  $After = $Directory.GetAccessControl()
  if ($After.Owner -cne $OwnerBefore) {
    throw "Owner changed during DACL-only F28 initialization."
  }
  if (@($After.Audit).Count -ne $AuditCountBefore) {
    throw "Audit rule count changed during DACL-only F28 initialization."
  }
  if (-not $After.AreAccessRulesProtected) {
    throw "F28 gate ACL still inherits access rules."
  }

  $TaskSid = (
    [System.Security.Principal.NTAccount]::new($Identity)
  ).Translate([System.Security.Principal.SecurityIdentifier]).Value
  $ExpectedSids = @($TaskSid, "S-1-5-18", "S-1-5-32-544")
  $Rules = @($After.GetAccessRules(
    $true,
    $false,
    [System.Security.Principal.SecurityIdentifier]
  ))
  if ($Rules.Count -ne 3) {
    throw "Expected exactly three explicit F28 access rules; found $($Rules.Count)."
  }
  foreach ($Rule in $Rules) {
    if ($Rule.IdentityReference.Value -notin $ExpectedSids -or
        $Rule.AccessControlType -ne [System.Security.AccessControl.AccessControlType]::Allow -or
        (($Rule.FileSystemRights -band [System.Security.AccessControl.FileSystemRights]::Modify) -ne [System.Security.AccessControl.FileSystemRights]::Modify) -or
        $Rule.InheritanceFlags -ne (
          [System.Security.AccessControl.InheritanceFlags]::ContainerInherit -bor
          [System.Security.AccessControl.InheritanceFlags]::ObjectInherit
        ) -or
        $Rule.PropagationFlags -ne [System.Security.AccessControl.PropagationFlags]::None) {
      throw "F28 gate ACL contains a rule outside the exact contract."
    }
  }
  foreach ($Sid in $ExpectedSids) {
    if (@($Rules | Where-Object { $_.IdentityReference.Value -ceq $Sid }).Count -ne 1) {
      throw "F28 gate ACL does not contain exactly one rule for $Sid."
    }
  }

  Write-Output "F28_GATE_ACL_INITIALIZATION_VERIFIED"
} finally {
  Remove-Item -LiteralPath $Sandbox -Recurse -Force -ErrorAction SilentlyContinue
}
