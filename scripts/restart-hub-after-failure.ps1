[CmdletBinding()]
param(
  [Parameter(Mandatory = $true)]
  [string]$DataRoot,
  [Parameter(Mandatory = $true)]
  [ValidateRange(1, 3)]
  [int]$ActionIndex,
  [ValidateRange(1, 60)]
  [int]$DelaySeconds = 60
)

# This wrapper is deliberately not a general restart mechanism.  Scheduler
# owns invocation; a fresh, single-use F28 authorization is its only permit.
$ErrorActionPreference = "Stop"
$ProjectRoot = (Resolve-Path -LiteralPath (Split-Path -Parent $PSScriptRoot)).Path
$ResolvedDataRoot = (Resolve-Path -LiteralPath $DataRoot).Path
$GateDirectory = Join-Path $ResolvedDataRoot "acceptance\f28"

function Test-JsonInteger {
  param([AllowNull()][object]$Value)
  return $null -ne $Value -and ($Value -is [System.SByte] -or $Value -is [System.Byte] -or
    $Value -is [System.Int16] -or $Value -is [System.UInt16] -or $Value -is [System.Int32] -or
    $Value -is [System.UInt32] -or $Value -is [System.Int64] -or $Value -is [System.UInt64])
}

function Get-LeafItem {
  param([string]$Path, [string]$Label)
  $Item = Get-Item -LiteralPath $Path -Force -ErrorAction Stop
  if ($Item.PSIsContainer -or ($Item.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
    throw "F28 $Label must be a non-reparse-point leaf."
  }
  return $Item
}

function Assert-NoReparseAncestors {
  param([string]$Path, [string]$Label)
  $Current = (Get-Item -LiteralPath $Path -Force -ErrorAction Stop).FullName
  while ($true) {
    $Item = Get-Item -LiteralPath $Current -Force -ErrorAction Stop
    if (($Item.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
      throw "F28 $Label contains a reparse point: $Current"
    }
    $Parent = Split-Path -Parent $Current
    if (-not $Parent -or $Parent -ceq $Current) { return }
    $Current = $Parent
  }
}

function Get-FileSha256 {
  param([string]$Path)
  return (Get-FileHash -LiteralPath $Path -Algorithm SHA256 -ErrorAction Stop).Hash.ToLowerInvariant()
}

function Get-BytesSha256 {
  param([byte[]]$Bytes)
  $Hasher = [System.Security.Cryptography.SHA256]::Create()
  try { return ([BitConverter]::ToString($Hasher.ComputeHash($Bytes))).Replace("-", "").ToLowerInvariant() }
  finally { $Hasher.Dispose() }
}

function Get-StrictUtcTimestamp {
  param([object]$Value, [string]$Label)
  $Parsed = [datetime]::MinValue
  if (-not [datetime]::TryParseExact([string]$Value, "o", [System.Globalization.CultureInfo]::InvariantCulture, [System.Globalization.DateTimeStyles]::RoundtripKind, [ref]$Parsed) -or $Parsed.Kind -ne [System.DateTimeKind]::Utc) {
    throw "F28 $Label must be an exact UTC round-trip timestamp."
  }
  return $Parsed
}

function Get-F28SystemPowerShell {
  $SystemDirectory = [System.Environment]::SystemDirectory
  if ([string]::IsNullOrWhiteSpace($SystemDirectory)) { throw "Windows system directory is unavailable." }
  $PowerShellPath = [System.IO.Path]::GetFullPath(
    (Join-Path $SystemDirectory "WindowsPowerShell\v1.0\powershell.exe")
  )
  if (-not (Test-Path -LiteralPath $PowerShellPath -PathType Leaf)) {
    throw "Pinned Windows PowerShell executable is missing: $PowerShellPath"
  }
  $Item = Get-Item -LiteralPath $PowerShellPath -Force -ErrorAction Stop
  if ($Item.PSIsContainer -or ($Item.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
    throw "Pinned Windows PowerShell executable must be a non-reparse-point leaf."
  }
  return $PowerShellPath
}

function Assert-ExactRuntimeIdentity {
  $Git = Get-Command git.exe -ErrorAction SilentlyContinue
  if (-not $Git) { $Git = Get-Command git -ErrorAction Stop }
  $Revision = ([string]@(& $Git.Source -C $ProjectRoot rev-parse HEAD)[0]).Trim().ToLowerInvariant()
  if ($LASTEXITCODE -ne 0) { throw "F28 recovery cannot resolve source revision." }
  $Tree = ([string]@(& $Git.Source -C $ProjectRoot rev-parse "HEAD^{tree}")[0]).Trim().ToLowerInvariant()
  if ($LASTEXITCODE -ne 0) { throw "F28 recovery cannot resolve source tree." }
  $Status = @(& $Git.Source -C $ProjectRoot status --porcelain=v1 --untracked-files=all -- src scripts pyproject.toml)
  if ($LASTEXITCODE -ne 0 -or $Status.Count -ne 0) { throw "F28 recovery source is not clean." }
  if ($Revision -notmatch '^[0-9a-f]{40}$' -or $Tree -notmatch '^[0-9a-f]{40}$') {
    throw "F28 recovery source identity is not exact."
  }
  return [pscustomobject]@{ revision = $Revision; tree = $Tree }
}

function Test-SameRootHubRuntime {
  param([string]$ExpectedRoot)
  $Prefix = $ExpectedRoot.TrimEnd("\") + "\"
  return @(
    Get-CimInstance Win32_Process | Where-Object {
      [string]$_.Name -ieq "oms-hub.exe" -and
      ([string]$_.ExecutablePath).StartsWith($Prefix, [System.StringComparison]::OrdinalIgnoreCase)
    }
  ).Count -gt 0
}

try {
  Assert-NoReparseAncestors -Path $ProjectRoot -Label "project root"
  Assert-NoReparseAncestors -Path $ResolvedDataRoot -Label "data root"
  Assert-NoReparseAncestors -Path $GateDirectory -Label "gate directory"
  $Gate = Get-Item -LiteralPath $GateDirectory -Force -ErrorAction Stop
  if (-not $Gate.PSIsContainer -or ($Gate.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
    throw "F28 gate directory is unsafe."
  }
  $Matches = @(
    Get-ChildItem -LiteralPath $GateDirectory -File -Force | Where-Object {
      $_.Name -match ('^recovery-authorized-([0-9a-f]{{8}}-[0-9a-f]{{4}}-[0-9a-f]{{4}}-[0-9a-f]{{4}}-[0-9a-f]{{12}})-{0}\.json$' -f $ActionIndex)
    }
  )
  if ($Matches.Count -ne 1) { throw "F28 recovery requires exactly one authorization for action $ActionIndex." }
  $AuthorizationPath = $Matches[0].FullName
  Assert-NoReparseAncestors -Path $AuthorizationPath -Label "authorization"
  Get-LeafItem -Path $AuthorizationPath -Label "authorization" | Out-Null
  $RawAuthorization = [System.IO.File]::ReadAllBytes($AuthorizationPath)
  $AuthorizationSha256 = Get-BytesSha256 -Bytes $RawAuthorization
  $Authorization = (New-Object System.Text.UTF8Encoding($false, $true)).GetString($RawAuthorization) | ConvertFrom-Json
  $Nonce = [string]$Authorization.nonce
  $Guid = [guid]::Empty
  if (-not [guid]::TryParseExact($Nonce, "D", [ref]$Guid) -or $Nonce -cne $Guid.ToString("D")) { throw "F28 recovery nonce is not canonical." }
  $Identity = Assert-ExactRuntimeIdentity
  $ExpectedEvidence = "launcher-exit-$Nonce.json"
  $ExpectedConsumed = "recovery-consumed-$Nonce-$ActionIndex.json"
  $Expires = Get-StrictUtcTimestamp -Value $Authorization.expires_at -Label "authorization expires_at"
  $AuthorizedAt = Get-StrictUtcTimestamp -Value $Authorization.authorized_at -Label "authorization authorized_at"
  $Now = (Get-Date).ToUniversalTime()
  if ($Expires -le $AuthorizedAt -or $Expires -gt $AuthorizedAt.AddMinutes(5) -or $Now -lt $AuthorizedAt.AddSeconds(-2) -or $Now -gt $Expires) { throw "F28 recovery authorization time window is invalid." }
  if (-not (Test-JsonInteger $Authorization.schema_version) -or [decimal]$Authorization.schema_version -ne 1 -or
      -not (Test-JsonInteger $Authorization.expected_schema) -or [decimal]$Authorization.expected_schema -le 0 -or
      -not (Test-JsonInteger $Authorization.exit_code) -or [decimal]$Authorization.exit_code -ne 75 -or
      -not (Test-JsonInteger $Authorization.predecessor_action_index) -or [decimal]$Authorization.predecessor_action_index -ne ($ActionIndex - 1) -or
      -not (Test-JsonInteger $Authorization.next_action_index) -or [decimal]$Authorization.next_action_index -ne $ActionIndex -or
      [string]$Authorization.expected_revision -cne $Identity.revision -or [string]$Authorization.expected_tree -cne $Identity.tree -or
      [string]$Authorization.predecessor_evidence -cne $ExpectedEvidence -or
      [string]$Authorization.predecessor_evidence_sha256 -notmatch '^[0-9a-f]{64}$') { throw "F28 recovery authorization does not bind this direct predecessor." }
  $EvidencePath = Join-Path $GateDirectory $ExpectedEvidence
  Assert-NoReparseAncestors -Path $EvidencePath -Label "predecessor evidence"
  Get-LeafItem -Path $EvidencePath -Label "predecessor evidence" | Out-Null
  if ((Get-FileSha256 $EvidencePath) -cne [string]$Authorization.predecessor_evidence_sha256) { throw "F28 recovery predecessor evidence hash mismatch." }
  $RawEvidence = [System.IO.File]::ReadAllBytes($EvidencePath)
  $Evidence = (New-Object System.Text.UTF8Encoding($false, $true)).GetString($RawEvidence) | ConvertFrom-Json
  if (-not (Test-JsonInteger $Evidence.schema_version) -or [decimal]$Evidence.schema_version -ne 1 -or
      -not (Test-JsonInteger $Evidence.exit_code) -or [decimal]$Evidence.exit_code -ne 75 -or
      -not (Test-JsonInteger $Evidence.expected_schema) -or [decimal]$Evidence.expected_schema -ne [decimal]$Authorization.expected_schema -or
      -not (Test-JsonInteger $Evidence.action_index) -or [decimal]$Evidence.action_index -ne ($ActionIndex - 1) -or
      [string]$Evidence.nonce -cne $Nonce -or [string]$Evidence.expected_revision -cne $Identity.revision -or [string]$Evidence.expected_tree -cne $Identity.tree) { throw "F28 recovery evidence is invalid." }
  $ConsumedPath = Join-Path $GateDirectory $ExpectedConsumed
  if (Test-Path -LiteralPath $ConsumedPath) { throw "F28 recovery authorization was already consumed." }
  [System.IO.File]::Move($AuthorizationPath, $ConsumedPath)
  Assert-NoReparseAncestors -Path $ConsumedPath -Label "consumed authorization"
  Get-LeafItem -Path $ConsumedPath -Label "consumed authorization" | Out-Null
  $ConsumedBytes = [System.IO.File]::ReadAllBytes($ConsumedPath)
  if ((Get-FileSha256 $ConsumedPath) -cne $AuthorizationSha256) { throw "F28 consumed authorization hash differs from the authorized record." }
  if ([System.BitConverter]::ToString($ConsumedBytes) -cne [System.BitConverter]::ToString($RawAuthorization)) { throw "F28 consumed authorization bytes differ from the authorized record." }
  Write-Host "F28 recovery action $ActionIndex authorized; waiting $DelaySeconds seconds."
  Start-Sleep -Seconds $DelaySeconds
  if ((Get-FileSha256 $ConsumedPath) -cne $AuthorizationSha256 -or [System.BitConverter]::ToString([System.IO.File]::ReadAllBytes($ConsumedPath)) -cne [System.BitConverter]::ToString($RawAuthorization)) { throw "F28 consumed authorization changed during delay." }
  $Identity = Assert-ExactRuntimeIdentity
  if ($Identity.revision -cne [string]$Authorization.expected_revision -or $Identity.tree -cne [string]$Authorization.expected_tree) { throw "F28 source identity changed during recovery delay." }
  if ((Get-FileSha256 $EvidencePath) -cne [string]$Authorization.predecessor_evidence_sha256) { throw "F28 predecessor evidence changed during delay." }
  if (Test-SameRootHubRuntime -ExpectedRoot $ProjectRoot) { Write-Host "F28 recovery no-op: same-root Hub is already running."; exit 0 }
  $PowerShell = Get-F28SystemPowerShell
  $StartScript = Join-Path $ProjectRoot "scripts\start-hub.ps1"
  & $PowerShell -NoProfile -ExecutionPolicy Bypass -File $StartScript -DataRoot $ResolvedDataRoot -ActionIndex $ActionIndex
  $ExitCode = $LASTEXITCODE
  exit $ExitCode
} catch {
  [Console]::Error.WriteLine("F28 recovery action $ActionIndex denied: $($_.Exception.Message)")
  exit 1
}
