Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

function Assert-Task28NoReparsePath {
  param([Parameter(Mandatory = $true)][string]$Path)
  $Cursor = [IO.Path]::GetFullPath($Path)
  while ($true) {
    if (Test-Path -LiteralPath $Cursor) {
      if ((Get-Item -LiteralPath $Cursor -Force).Attributes -band [IO.FileAttributes]::ReparsePoint) {
        throw "Path must not cross a reparse point."
      }
    }
    $Parent = Split-Path -Parent $Cursor
    if ([string]::IsNullOrEmpty($Parent) -or $Parent -ceq $Cursor) { return }
    $Cursor = $Parent
  }
}

function Test-Task28FullyQualifiedPath {
  param([Parameter(Mandatory = $true)][string]$Path)
  if ([string]::IsNullOrWhiteSpace($Path)) { return $false }
  if ([IO.Path]::DirectorySeparatorChar -eq '/' -and $Path.StartsWith('/')) { return $true }
  return $Path -match '^[A-Za-z]:[\\/]' -or $Path -match '^\\\\[^\\/]+[\\/][^\\/]+'
}

function Resolve-Task28ExistingPath {
  param(
    [Parameter(Mandatory = $true)][string]$Path,
    [Parameter(Mandatory = $true)][ValidateSet("Leaf", "Container")][string]$Type
  )
  if (-not (Test-Task28FullyQualifiedPath -Path $Path) -or -not (Test-Path -LiteralPath $Path -PathType $Type)) {
    throw "Expected an absolute existing $Type path."
  }
  $Resolved = [IO.Path]::GetFullPath((Get-Item -LiteralPath $Path -Force).FullName)
  Assert-Task28NoReparsePath -Path $Resolved
  return $Resolved
}

function Assert-Task28UnderRoot {
  param(
    [Parameter(Mandatory = $true)][string]$Path,
    [Parameter(Mandatory = $true)][string]$Root,
    [switch]$RequireDescendant
  )
  $CanonicalPath = [IO.Path]::GetFullPath($Path)
  $CanonicalRoot = [IO.Path]::GetFullPath($Root)
  if ([string]::Equals($CanonicalPath, $CanonicalRoot, [StringComparison]::OrdinalIgnoreCase)) {
    if ($RequireDescendant) { throw "Path must be a descendant of its expected root." }
    return
  }
  $Prefix = $CanonicalRoot.TrimEnd('\', '/') + [IO.Path]::DirectorySeparatorChar
  if (-not $CanonicalPath.StartsWith($Prefix, [StringComparison]::OrdinalIgnoreCase)) {
    throw "Path escaped its expected root."
  }
}

function Test-Task28DescendantPath {
  param([Parameter(Mandatory = $true)][string]$Path, [Parameter(Mandatory = $true)][string]$Root)
  try {
    Assert-Task28UnderRoot -Path $Path -Root $Root -RequireDescendant
    return $true
  } catch { return $false }
}

function Assert-Task28PropertyNames {
  param(
    [Parameter(Mandatory = $true)][object]$Value,
    [Parameter(Mandatory = $true)][string[]]$Expected,
    [Parameter(Mandatory = $true)][string]$Label
  )
  if ($null -eq $Value) { throw "$Label is unavailable." }
  $Actual = @($Value.PSObject.Properties | ForEach-Object { $_.Name })
  if ($Actual.Count -ne $Expected.Count -or @($Expected | Where-Object { $_ -cnotin $Actual }).Count -ne 0) {
    throw "$Label has an invalid schema."
  }
}

function Assert-Task28Sha256 {
  param([Parameter(Mandatory = $true)][object]$Value, [Parameter(Mandatory = $true)][string]$Label)
  if ($Value -isnot [string] -or $Value -cnotmatch "^[0-9a-f]{64}$") {
    throw "$Label must be a lowercase SHA-256."
  }
}

function Assert-Task28NonNegativeInt64 {
  param([Parameter(Mandatory = $true)][object]$Value, [Parameter(Mandatory = $true)][string]$Label)
  if (($Value -isnot [int64] -and $Value -isnot [int]) -or [int64]$Value -lt 0) {
    throw "$Label must be a non-negative int64."
  }
  return [int64]$Value
}

function Get-Task28CanonicalRelativePath {
  param([Parameter(Mandatory = $true)][object]$Value)
  if ($Value -isnot [string] -or [string]::IsNullOrWhiteSpace($Value) -or
      $Value.Contains('\') -or $Value.StartsWith("/") -or $Value -match "^[A-Za-z]:") {
    throw "Manifest path is not a normalized POSIX-relative path."
  }
  $Segments = $Value.Split("/")
  if (@($Segments | Where-Object { [string]::IsNullOrEmpty($_) -or $_ -in @(".", "..") }).Count -ne 0) {
    throw "Manifest path is not a normalized POSIX-relative path."
  }
  return $Value
}

function Get-Task28FileRows {
  param([Parameter(Mandatory = $true)][string]$Root)
  $Root = Resolve-Task28ExistingPath -Path $Root -Type Container
  $Prefix = $Root.TrimEnd('\', '/') + [IO.Path]::DirectorySeparatorChar
  $Items = @(Get-ChildItem -LiteralPath $Root -Force -Recurse)
  if (@($Items | Where-Object { $_.Attributes -band [IO.FileAttributes]::ReparsePoint }).Count -ne 0) {
    throw "Immutable bundle contains a reparse point."
  }
  return @($Items | Where-Object { -not $_.PSIsContainer } | Sort-Object FullName | ForEach-Object {
    Assert-Task28NoReparsePath -Path $_.FullName
    [ordered]@{
      path = $_.FullName.Substring($Prefix.Length).Replace('\', '/')
      sha256 = (Get-FileHash -LiteralPath $_.FullName -Algorithm SHA256).Hash.ToLowerInvariant()
      size = [int64]$_.Length
    }
  })
}

function Assert-Task28FileHash {
  param(
    [Parameter(Mandatory = $true)][string]$Path,
    [Parameter(Mandatory = $true)][object]$Expected,
    [Parameter(Mandatory = $true)][string]$Label
  )
  Assert-Task28Sha256 -Value $Expected -Label $Label
  $Path = Resolve-Task28ExistingPath -Path $Path -Type Leaf
  $Actual = (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToLowerInvariant()
  if (-not [string]::Equals($Actual, [string]$Expected, [StringComparison]::OrdinalIgnoreCase)) {
    throw "$Label hash differs."
  }
}

function Assert-ManifestEquality {
  param(
    [Parameter(Mandatory = $true)][string]$Root,
    [Parameter(Mandatory = $true)][string]$ManifestPath,
    [string]$ExcludedRelativePath
  )
  $Root = Resolve-Task28ExistingPath -Path $Root -Type Container
  $ManifestPath = Resolve-Task28ExistingPath -Path $ManifestPath -Type Leaf
  $Manifest = [IO.File]::ReadAllText($ManifestPath, [Text.UTF8Encoding]::new($false, $true)) | ConvertFrom-Json
  if (-not [string]::IsNullOrWhiteSpace($ExcludedRelativePath)) {
    $ExcludedRelativePath = Get-Task28CanonicalRelativePath -Value $ExcludedRelativePath
  }
  Assert-Task28PropertyNames -Value $Manifest -Expected @("schema_version", "files") -Label "Manifest"
  if (($Manifest.schema_version -isnot [int64] -and $Manifest.schema_version -isnot [int]) -or
      $Manifest.schema_version -ne 1 -or $Manifest.files -isnot [System.Collections.IEnumerable]) {
    throw "Manifest has an invalid schema."
  }
  $ExpectedByPath = [Collections.Generic.Dictionary[string, object]]::new([StringComparer]::OrdinalIgnoreCase)
  foreach ($Row in @($Manifest.files)) {
    Assert-Task28PropertyNames -Value $Row -Expected @("path", "sha256", "size") -Label "Manifest row"
    $RelativePath = Get-Task28CanonicalRelativePath -Value $Row.path
    if ($RelativePath -ceq $ExcludedRelativePath) { throw "Manifest must not include its self-bound file." }
    Assert-Task28Sha256 -Value $Row.sha256 -Label "Manifest row hash"
    $Size = Assert-Task28NonNegativeInt64 -Value $Row.size -Label "Manifest row size"
    if ($ExpectedByPath.ContainsKey($RelativePath)) {
      throw "Manifest contains a case-insensitive duplicate path."
    }
    $ExpectedByPath.Add($RelativePath, [pscustomobject]@{path=$RelativePath; sha256=[string]$Row.sha256; size=$Size})
    $FilePath = [IO.Path]::GetFullPath((Join-Path $Root $RelativePath))
    Assert-Task28UnderRoot -Path $FilePath -Root $Root -RequireDescendant
    Resolve-Task28ExistingPath -Path $FilePath -Type Leaf | Out-Null
  }
  $ActualByPath = [Collections.Generic.Dictionary[string, object]]::new([StringComparer]::OrdinalIgnoreCase)
  foreach ($Row in @(Get-Task28FileRows -Root $Root)) {
    if ($Row.path -ceq $ExcludedRelativePath) { continue }
    if ($ActualByPath.ContainsKey([string]$Row.path)) { throw "Root contains a case-insensitive duplicate path." }
    $ActualByPath.Add([string]$Row.path, $Row)
  }
  if ($ExpectedByPath.Count -ne $ActualByPath.Count) { throw "Manifest file count differs." }
  foreach ($RelativePath in $ExpectedByPath.Keys) {
    $Expected = $ExpectedByPath[$RelativePath]
    if (-not $ActualByPath.ContainsKey($RelativePath)) { throw "Manifest does not exactly match its root." }
    $Actual = $ActualByPath[$RelativePath]
    if (-not [string]::Equals([string]$Expected.sha256, [string]$Actual.sha256, [StringComparison]::OrdinalIgnoreCase) -or
        [int64]$Expected.size -ne [int64]$Actual.size) {
      throw "Manifest does not exactly match its root."
    }
  }
  return @($ExpectedByPath.Values | Sort-Object path)
}

function Get-Task28StatePaths {
  param([Parameter(Mandatory = $true)][string]$RunId)
  if ($RunId -cnotmatch "^[0-9a-f]{32}$") { throw "Run ID is invalid." }
  $LocalAppData = [Environment]::GetFolderPath([Environment+SpecialFolder]::LocalApplicationData)
  if ([string]::IsNullOrWhiteSpace($LocalAppData)) { throw "Local application data root is unavailable." }
  $Parent = [IO.Path]::GetFullPath((Join-Path $LocalAppData "Temp\\oms-task28-runs"))
  Assert-Task28NoReparsePath -Path $Parent
  $Root = [IO.Path]::GetFullPath((Join-Path $Parent $RunId))
  return [pscustomobject]@{
    Parent=$Parent; Root=$Root; Evidence=(Join-Path $Root "evidence");
    Scratch=(Join-Path $Root "scratch"); Diagnostic=(Join-Path $Root "diagnostic")
  }
}

function Get-Task28CurrentSid {
  $Identity = & whoami.exe /user /fo csv /nh | ConvertFrom-Csv -Header Name,Sid
  if ($LASTEXITCODE -ne 0 -or $Identity.Sid -notmatch '^S-1(?:-\d+)+$') {
    throw "Current Windows SID was unavailable."
  }
  return [string]$Identity.Sid
}

function Assert-Task28ProtectedDirectory {
  param([Parameter(Mandatory = $true)][string]$Path, [Parameter(Mandatory = $true)][string]$Sid)
  $Path = Resolve-Task28ExistingPath -Path $Path -Type Container
  $Acl = Get-Acl -LiteralPath $Path
  $Rules = @($Acl.GetAccessRules($true, $false, [System.Security.Principal.SecurityIdentifier]))
  $FullControl = [System.Security.AccessControl.FileSystemRights]::FullControl
  $Inheritance = [System.Security.AccessControl.InheritanceFlags]::ContainerInherit -bor
    [System.Security.AccessControl.InheritanceFlags]::ObjectInherit
  if (-not $Acl.AreAccessRulesProtected -or $Rules.Count -ne 1 -or
      $Rules[0].IdentityReference.Value -cne $Sid -or $Rules[0].IsInherited -or
      $Rules[0].AccessControlType -ne [System.Security.AccessControl.AccessControlType]::Allow -or
      ($Rules[0].FileSystemRights -band $FullControl) -ne $FullControl -or
      ($Rules[0].InheritanceFlags -band $Inheritance) -ne $Inheritance -or
      $Rules[0].PropagationFlags -ne [System.Security.AccessControl.PropagationFlags]::None) {
    throw "Task28 state DACL was not current-user-only."
  }
  & icacls.exe $Path /verify | Out-Null
  if ($LASTEXITCODE -ne 0) { throw "Task28 state DACL verification failed." }
}

function Protect-Task28Directory {
  param([Parameter(Mandatory = $true)][string]$Path, [Parameter(Mandatory = $true)][string]$Sid)
  $Directory = Get-Item -LiteralPath $Path -Force
  $Acl = $Directory.GetAccessControl([System.Security.AccessControl.AccessControlSections]::Access)
  $Acl.SetAccessRuleProtection($true, $false)
  foreach ($ExistingRule in @($Acl.GetAccessRules(
    $true, $true, [System.Security.Principal.SecurityIdentifier]
  ))) {
    $Acl.RemoveAccessRuleSpecific($ExistingRule)
  }
  $Inheritance = [System.Security.AccessControl.InheritanceFlags]::ContainerInherit -bor
    [System.Security.AccessControl.InheritanceFlags]::ObjectInherit
  $Rule = [System.Security.AccessControl.FileSystemAccessRule]::new(
    [System.Security.Principal.SecurityIdentifier]::new($Sid),
    [System.Security.AccessControl.FileSystemRights]::FullControl,
    $Inheritance,
    [System.Security.AccessControl.PropagationFlags]::None,
    [System.Security.AccessControl.AccessControlType]::Allow
  )
  $Acl.AddAccessRule($Rule) | Out-Null
  $Directory.SetAccessControl($Acl)
  Assert-Task28ProtectedDirectory -Path $Path -Sid $Sid
}

function New-Task28ProtectedState {
  param([Parameter(Mandatory = $true)][object]$State)
  if (Test-Path -LiteralPath $State.Root) { throw "Task28 state root must be absent." }
  Assert-Task28NoReparsePath -Path $State.Parent
  New-Item -ItemType Directory -Path $State.Parent -Force | Out-Null
  $Sibling = Join-Path $State.Parent (".{0}.stage-{1}" -f (Split-Path -Leaf $State.Root), [Guid]::NewGuid().ToString("N"))
  if (Test-Path -LiteralPath $Sibling) { throw "Task28 state staging root already exists." }
  $Sid = Get-Task28CurrentSid
  New-Item -ItemType Directory -Path $Sibling -ErrorAction Stop | Out-Null
  try {
    Protect-Task28Directory -Path $Sibling -Sid $Sid
    foreach ($Name in @("evidence", "scratch", "diagnostic")) {
      $Child = Join-Path $Sibling $Name
      New-Item -ItemType Directory -Path $Child -ErrorAction Stop | Out-Null
      Protect-Task28Directory -Path $Child -Sid $Sid
    }
    [IO.Directory]::Move($Sibling, $State.Root)
    $Sibling = $null
  } finally {
    if ($Sibling -and (Test-Path -LiteralPath $Sibling)) {
      Remove-Item -LiteralPath $Sibling -Recurse -Force -ErrorAction SilentlyContinue
    }
  }
  Assert-Task28ProtectedState -State $State
}

function Assert-Task28ProtectedState {
  param([Parameter(Mandatory = $true)][object]$State)
  $Sid = Get-Task28CurrentSid
  Assert-Task28StateInventory -State $State
  foreach ($Path in @($State.Root, $State.Evidence, $State.Scratch, $State.Diagnostic)) {
    Assert-Task28ProtectedDirectory -Path $Path -Sid $Sid
  }
}

function Assert-Task28StateInventory {
  param([Parameter(Mandatory = $true)][object]$State)
  $Root = Resolve-Task28ExistingPath -Path $State.Root -Type Container
  $ExpectedNames = @("evidence", "scratch", "diagnostic")
  $Items = @(Get-ChildItem -LiteralPath $Root -Force)
  $ActualNames = @($Items | ForEach-Object { $_.Name })
  if ($ActualNames.Count -ne $ExpectedNames.Count -or
      @($ExpectedNames | Where-Object { $_ -cnotin $ActualNames }).Count -ne 0 -or
      @($Items | Where-Object { -not $_.PSIsContainer }).Count -ne 0) {
    throw "Task28 state root has an invalid child inventory."
  }
  foreach ($Path in @($State.Evidence, $State.Scratch, $State.Diagnostic)) {
    Resolve-Task28ExistingPath -Path $Path -Type Container | Out-Null
  }
}

function Read-BoundRunManifest {
  param([Parameter(Mandatory = $true)][string]$Path)
  $Path = Resolve-Task28ExistingPath -Path $Path -Type Leaf
  $Hash = (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToLowerInvariant()
  $Leaf = Split-Path -Leaf $Path
  if ($Leaf -cnotmatch "^run-manifest\.([0-9a-f]{64})\.json$" -or
      -not [string]::Equals($Leaf, "run-manifest.$Hash.json", [StringComparison]::Ordinal)) {
    throw "Run manifest filename is not content-bound."
  }
  $Value = [IO.File]::ReadAllText($Path, [Text.UTF8Encoding]::new($false, $true)) | ConvertFrom-Json
  Assert-Task28PropertyNames -Value $Value -Expected @(
    "schema_version", "source", "runtime", "allowed_task_name", "immutable_bundle_path", "run_id", "mutable_state_path",
    "controller_sha256", "launcher_sha256", "common_sha256", "entrypoint_sha256", "wrapper_sha256",
    "evidence_sha256", "smoke_sha256", "python_executable", "hub_health_url", "authorization_count"
  ) -Label "Run manifest"
  Assert-Task28PropertyNames -Value $Value.source -Expected @("commit", "tree", "archive_sha256", "manifest_sha256") -Label "Run source"
  Assert-Task28PropertyNames -Value $Value.runtime -Expected @("lock_sha256", "manifest_sha256") -Label "Run runtime"
  if (($Value.schema_version -isnot [int64] -and $Value.schema_version -isnot [int]) -or
      $Value.schema_version -ne 1 -or
      ($Value.authorization_count -isnot [int64] -and $Value.authorization_count -isnot [int]) -or
      $Value.authorization_count -ne 0 -or $Value.source.commit -isnot [string] -or $Value.source.commit -cnotmatch "^[0-9a-f]{40}$" -or
      $Value.source.tree -isnot [string] -or $Value.source.tree -cnotmatch "^[0-9a-f]{40}$" -or
      $Value.allowed_task_name -isnot [string] -or $Value.allowed_task_name -cnotmatch "^[A-Za-z0-9._-]{1,120}$" -or
      $Value.run_id -isnot [string] -or $Value.run_id -cnotmatch "^[0-9a-f]{32}$") {
    throw "Run manifest is invalid."
  }
  foreach ($Name in @("archive_sha256", "manifest_sha256")) { Assert-Task28Sha256 -Value $Value.source.$Name -Label "Run source $Name" }
  foreach ($Name in @("lock_sha256", "manifest_sha256")) { Assert-Task28Sha256 -Value $Value.runtime.$Name -Label "Run runtime $Name" }
  foreach ($Name in @("controller_sha256", "launcher_sha256", "common_sha256", "entrypoint_sha256", "wrapper_sha256", "evidence_sha256", "smoke_sha256")) {
    Assert-Task28Sha256 -Value $Value.$Name -Label "Run $Name"
  }
  foreach ($Name in @("immutable_bundle_path", "mutable_state_path", "python_executable")) {
    if ($Value.$Name -isnot [string] -or -not (Test-Task28FullyQualifiedPath -Path ([string]$Value.$Name))) { throw "Run $Name must be absolute." }
    $Value.$Name = [IO.Path]::GetFullPath([string]$Value.$Name)
  }
  $State = Get-Task28StatePaths -RunId ([string]$Value.run_id)
  if (-not [string]::Equals([string]$Value.mutable_state_path, $State.Root, [StringComparison]::OrdinalIgnoreCase)) {
    throw "Run mutable state path does not match its fixed run ID."
  }
  Resolve-Task28ExistingPath -Path ([string]$Value.python_executable) -Type Leaf | Out-Null
  if ($Value.hub_health_url -isnot [string] -or [string]::IsNullOrWhiteSpace($Value.hub_health_url)) { throw "Run health URL is invalid." }
  try { $HealthUri = [Uri]$Value.hub_health_url } catch { throw "Run health URL is invalid." }
  if ($HealthUri.Scheme -notin @("http", "https") -or $HealthUri.Host -notin @("127.0.0.1", "localhost") -or
      $HealthUri.AbsolutePath -cne "/health" -or -not [string]::IsNullOrEmpty($HealthUri.Query)) {
    throw "Run health URL is invalid."
  }
  return [pscustomobject]@{Path=$Path; Value=$Value}
}

function Assert-ImmutableBundle {
  param([Parameter(Mandatory = $true)][object]$Run)
  if ($null -eq $Run.Path -or $null -eq $Run.Value) { throw "Bound run is invalid." }
  $Bundle = Resolve-Task28ExistingPath -Path ([string]$Run.Value.immutable_bundle_path) -Type Container
  $ManifestParent = [IO.Path]::GetFullPath((Split-Path -Parent ([string]$Run.Path)))
  if (-not [string]::Equals($ManifestParent, $Bundle, [StringComparison]::OrdinalIgnoreCase)) {
    throw "Run manifest is not bound to its immutable bundle."
  }
  $Source = Join-Path $Bundle "source"
  $Runtime = Join-Path $Bundle "runtime"
  $SourceManifest = Join-Path $Bundle "source-manifest.json"
  $RuntimeManifest = Join-Path $Bundle "runtime-manifest.json"
  Assert-Task28FileHash -Path (Join-Path $Bundle "source.tar") -Expected $Run.Value.source.archive_sha256 -Label "Source archive"
  Assert-Task28FileHash -Path $SourceManifest -Expected $Run.Value.source.manifest_sha256 -Label "Source manifest"
  Assert-Task28FileHash -Path $RuntimeManifest -Expected $Run.Value.runtime.manifest_sha256 -Label "Runtime manifest"
  Assert-ManifestEquality -Root $Source -ManifestPath $SourceManifest | Out-Null
  Assert-ManifestEquality -Root $Runtime -ManifestPath $RuntimeManifest | Out-Null
  Assert-Task28FileHash -Path (Join-Path $Runtime "requirements.lock") -Expected $Run.Value.runtime.lock_sha256 -Label "Runtime requirements"
  foreach ($Pair in @(
      @("source/scripts/task28/private-shadow-controller.ps1", $Run.Value.controller_sha256, "Controller"),
      @("source/scripts/task28/private-shadow-launcher.ps1", $Run.Value.launcher_sha256, "Launcher"),
      @("source/scripts/task28/private-shadow-common.ps1", $Run.Value.common_sha256, "Common validator"),
      @("source/scripts/private-shadow-operator-entry.py", $Run.Value.entrypoint_sha256, "Entrypoint"),
      @("source/scripts/run-private-shadow-evidence.ps1", $Run.Value.wrapper_sha256, "Wrapper"),
      @("source/src/oms_hub/providers/gemini/evidence.py", $Run.Value.evidence_sha256, "Evidence"),
      @("source/scripts/run-gemini-contract-smoke.py", $Run.Value.smoke_sha256, "Smoke runner")
    )) {
    Assert-Task28FileHash -Path (Join-Path $Bundle $Pair[0]) -Expected $Pair[1] -Label $Pair[2]
  }
  $BundleManifest = @(
    Get-ChildItem -LiteralPath $Bundle -Force -File -Filter "bundle-manifest.*.json"
  )
  if ($BundleManifest.Count -ne 1) { throw "Immutable bundle has no unique self-bound file manifest." }
  $BundleManifest = $BundleManifest[0]
  $BundleHash = (Get-FileHash -LiteralPath $BundleManifest.FullName -Algorithm SHA256).Hash.ToLowerInvariant()
  if (-not [string]::Equals($BundleManifest.Name, "bundle-manifest.$BundleHash.json", [StringComparison]::Ordinal)) {
    throw "Immutable bundle manifest filename is not content-bound."
  }
  Assert-ManifestEquality -Root $Bundle -ManifestPath $BundleManifest.FullName -ExcludedRelativePath $BundleManifest.Name | Out-Null
  return $Bundle
}
