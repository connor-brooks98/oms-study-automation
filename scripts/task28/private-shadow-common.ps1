Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

function Get-Task28FileRows {
  param([Parameter(Mandatory = $true)][string]$Root)
  $Root = [IO.Path]::GetFullPath($Root)
  $Prefix = $Root.TrimEnd("\\", "/") + [IO.Path]::DirectorySeparatorChar
  $Items = @(Get-ChildItem -LiteralPath $Root -Force -Recurse)
  if (@($Items | Where-Object { $_.Attributes -band [IO.FileAttributes]::ReparsePoint }).Count) {
    throw "Immutable bundle contains a reparse point."
  }
  return @($Items | Where-Object { -not $_.PSIsContainer } | Sort-Object FullName | ForEach-Object {
    [ordered]@{path=$_.FullName.Substring($Prefix.Length).Replace("\\", "/"); sha256=(Get-FileHash -LiteralPath $_.FullName -Algorithm SHA256).Hash.ToLowerInvariant(); size=[int64]$_.Length}
  })
}

function Read-BoundRunManifest {
  param([Parameter(Mandatory = $true)][string]$Path)
  if (-not [IO.Path]::IsPathFullyQualified($Path) -or -not (Test-Path -LiteralPath $Path -PathType Leaf)) { throw "Run manifest is unavailable." }
  $Path = (Get-Item -LiteralPath $Path -Force).FullName
  $Hash = (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToLowerInvariant()
  if ((Split-Path -Leaf $Path) -cne "run-manifest.$Hash.json") { throw "Run manifest filename is not content-bound." }
  $Value = Get-Content -LiteralPath $Path -Raw | ConvertFrom-Json
  if ($Value.schema_version -ne 1 -or $Value.authorization_count -ne 0) { throw "Run manifest is invalid." }
  return [pscustomobject]@{Path=$Path; Value=$Value}
}

function Assert-ImmutableBundle {
  param([Parameter(Mandatory = $true)][string]$Bundle)
  $Bundle = [IO.Path]::GetFullPath($Bundle)
  $Source = Join-Path $Bundle "source"
  $Manifest = Join-Path $Bundle "source-manifest.json"
  if (-not (Test-Path -LiteralPath $Source -PathType Container) -or -not (Test-Path -LiteralPath $Manifest -PathType Leaf)) { throw "Immutable source is unavailable." }
  $Expected = @((Get-Content -LiteralPath $Manifest -Raw | ConvertFrom-Json).files)
  $Actual = @(Get-Task28FileRows -Root $Source)
  if (($Expected | ConvertTo-Json -Compress -Depth 5) -cne ($Actual | ConvertTo-Json -Compress -Depth 5)) { throw "Immutable source manifest differs." }
}
