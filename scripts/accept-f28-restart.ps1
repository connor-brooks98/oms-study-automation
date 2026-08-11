[CmdletBinding(SupportsShouldProcess, ConfirmImpact = "High")]
param(
  [string]$ProjectRoot = "C:\Services\oms-study-automation-v2",
  [string]$DataRoot = "C:\ProgramData\OMSStudyHub",
  [string]$TaskName = "OMS Study Hub V2",
  [Parameter(Mandatory = $true)]
  [string]$ExpectedRevision,
  [Parameter(Mandatory = $true)]
  [string]$ExpectedTree,
  [int]$ExpectedSchema = 22,
  [Parameter(Mandatory = $true)]
  [string]$ExpectedBackupPath,
  [int]$ArmTimeoutSeconds = 30,
  [int]$RestartTimeoutSeconds = 150
)

$ErrorActionPreference = "Stop"
$ExpectedWorkers = @("generation_worker", "ingestion_worker", "studio_worker")
$FixedExitCode = 75
$Utf8NoBom = New-Object System.Text.UTF8Encoding($false)
$FireWritten = $false
$Nonce = ([guid]::NewGuid()).ToString("D").ToLowerInvariant()

function Assert-NotReparsePoint {
  param([string]$Path, [string]$Label)
  $Item = Get-Item -LiteralPath $Path -Force -ErrorAction Stop
  if (($Item.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
    throw "$Label must not be a reparse point: $Path"
  }
}

function Write-AtomicJson {
  param([string]$Path, [object]$Value)
  if (Test-Path -LiteralPath $Path) {
    throw "F28 atomic JSON destination already exists: $Path"
  }
  $Directory = Split-Path -Parent $Path
  $Temporary = Join-Path $Directory (".{0}.{1}.tmp" -f (
    Split-Path -Leaf $Path
  ), [guid]::NewGuid())
  $Payload = $Value | ConvertTo-Json -Depth 20 -Compress
  [System.IO.File]::WriteAllText($Temporary, $Payload + "`n", $Utf8NoBom)
  try {
    # File.Move targets this exact path and never treats an existing directory
    # as a container. It also refuses to overwrite an existing file.
    [System.IO.File]::Move($Temporary, $Path)
  } finally {
    if (Test-Path -LiteralPath $Temporary -PathType Leaf) {
      Remove-Item -LiteralPath $Temporary -Force -ErrorAction SilentlyContinue
    }
  }
  if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
    throw "F28 atomic JSON publication did not create the exact destination file."
  }
  $Published = [System.IO.File]::ReadAllText($Path, $Utf8NoBom)
  if ($Published -cne ($Payload + "`n")) {
    throw "F28 atomic JSON publication differs from the requested payload."
  }
}

function Test-JsonInteger {
  param([AllowNull()][object]$Value)
  if ($null -eq $Value) { return $false }
  return (
    $Value -is [sbyte] -or
    $Value -is [byte] -or
    $Value -is [short] -or
    $Value -is [ushort] -or
    $Value -is [int] -or
    $Value -is [uint] -or
    $Value -is [long] -or
    $Value -is [ulong]
  )
}

function Test-ExactJsonInteger {
  param([AllowNull()][object]$Value, [long]$Expected)
  return (
    (Test-JsonInteger -Value $Value) -and
    [decimal]$Value -eq [decimal]$Expected
  )
}

function ConvertTo-UtcIso8601 {
  param([AllowNull()][object]$Value)
  if ($null -eq $Value) { return $null }
  return ([datetime]$Value).ToUniversalTime().ToString("o")
}

function Write-F28Diagnostic {
  param([string]$Message)
  try {
    [Console]::Error.WriteLine($Message)
  } catch {
    # Diagnostics must never replace the acceptance failure being preserved.
  }
}

function Remove-F28TemporaryDatabaseSnapshots {
  param([string[]]$Paths, [switch]$BestEffort)
  foreach ($Path in $Paths) {
    if ($BestEffort) {
      if (Test-Path -LiteralPath $Path -PathType Leaf) {
        Remove-Item `
          -LiteralPath $Path `
          -Force `
          -ErrorAction SilentlyContinue
      }
      continue
    }
    if (Test-Path -LiteralPath $Path) {
      if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        throw "F28 temporary database snapshot is not a regular file: $Path"
      }
      Remove-Item -LiteralPath $Path -Force -ErrorAction Stop
    }
    if (Test-Path -LiteralPath $Path) {
      throw "F28 temporary database snapshot cleanup failed: $Path"
    }
  }
}

function Get-DotEnvValue {
  param([string]$Name, [string]$Path)
  if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) { return $null }
  $EscapedName = [regex]::Escape($Name)
  $Line = Get-Content -LiteralPath $Path | Where-Object {
    $_ -match "^\s*(?:export\s+)?${EscapedName}\s*="
  } | Select-Object -Last 1
  if (-not $Line) { return $null }
  $Value = (
    [string]$Line -replace "^\s*(?:export\s+)?${EscapedName}\s*=\s*", ""
  ).Trim()
  if ($Value.StartsWith('"') -or $Value.StartsWith("'")) {
    $Quote = [string]$Value[0]
    $Closing = $Value.IndexOf($Quote, 1)
    if ($Closing -lt 1) { throw "Unterminated $Name value in $Path" }
    $Trailing = $Value.Substring($Closing + 1).Trim()
    if ($Trailing -and -not $Trailing.StartsWith("#")) {
      throw "Unsupported trailing content for $Name in $Path"
    }
    return $Value.Substring(1, $Closing - 1)
  }
  return ([regex]::Replace($Value, "\s+#.*$", "")).Trim()
}

function Resolve-DatabasePath {
  param([string]$DatabaseUrl, [string]$Root)
  if (-not $DatabaseUrl.StartsWith(
    "sqlite:///", [System.StringComparison]::OrdinalIgnoreCase
  )) {
    throw "F28 acceptance supports only the installed SQLite database."
  }
  $Raw = $DatabaseUrl.Substring("sqlite:///".Length).Replace("/", "\")
  if ($Raw -eq ":memory:") { throw "F28 cannot certify an in-memory database." }
  if (-not [System.IO.Path]::IsPathRooted($Raw)) { $Raw = Join-Path $Root $Raw }
  return [System.IO.Path]::GetFullPath($Raw)
}

function Get-SourceIdentity {
  param([string]$Root)
  $Git = Get-Command git.exe -ErrorAction SilentlyContinue
  if (-not $Git) { $Git = Get-Command git -ErrorAction Stop }
  $Revision = @(& $Git.Source -C $Root rev-parse HEAD 2>$null)
  if ($LASTEXITCODE -ne 0 -or -not $Revision) { throw "Cannot resolve source revision." }
  $Tree = @(& $Git.Source -C $Root rev-parse "HEAD^{tree}" 2>$null)
  if ($LASTEXITCODE -ne 0 -or -not $Tree) { throw "Cannot resolve source tree." }
  $Status = @(
    & $Git.Source -C $Root status --porcelain=v1 --untracked-files=all -- src scripts pyproject.toml 2>$null
  )
  if ($LASTEXITCODE -ne 0 -or $Status.Count -gt 0) {
    throw "Installed runtime source is not clean."
  }
  return [ordered]@{
    revision = ([string]$Revision[0]).Trim().ToLowerInvariant()
    tree = ([string]$Tree[0]).Trim().ToLowerInvariant()
  }
}

function Get-TaskContract {
  param(
    [string]$Name,
    [string]$Root,
    [string]$EffectiveDataRoot
  )
  $Task = Get-ScheduledTask -TaskName $Name -ErrorAction Stop
  $Actions = @($Task.Actions)
  if ($Actions.Count -ne 1) { throw "Scheduled task must have exactly one action." }
  $StartScript = Join-Path $Root "scripts\start-hub.ps1"
  $ExpectedArguments = "-NoProfile -ExecutionPolicy Bypass -File `"$StartScript`" -DataRoot `"$EffectiveDataRoot`""
  if (-not [string]::Equals(
    ([string]$Actions[0].Arguments).Trim(),
    $ExpectedArguments,
    [System.StringComparison]::OrdinalIgnoreCase
  )) { throw "Scheduled task action differs from the installed F28 launcher contract." }
  if (-not [string]::Equals(
    ([string]$Actions[0].WorkingDirectory).TrimEnd("\"),
    $Root.TrimEnd("\"),
    [System.StringComparison]::OrdinalIgnoreCase
  )) { throw "Scheduled task working directory differs from the installed source." }
  if ([System.IO.Path]::GetFileName([string]$Actions[0].Execute) -ine "powershell.exe") {
    throw "Scheduled task action does not use Windows PowerShell."
  }
  if ([int]$Task.Settings.RestartCount -lt 3) {
    throw "Scheduled task RestartCount must be at least three."
  }
  if (-not $Task.Settings.RestartInterval) {
    throw "Scheduled task RestartInterval is missing."
  }
  $TaskXml = Export-ScheduledTask -TaskName $Name
  return [ordered]@{
    full_task_name = "{0}{1}" -f [string]$Task.TaskPath, [string]$Task.TaskName
    action = [string]$Actions[0].Execute
    arguments = [string]$Actions[0].Arguments
    principal = [string]$Task.Principal.UserId
    restart_count = [int]$Task.Settings.RestartCount
    restart_interval = [string]$Task.Settings.RestartInterval
    xml_sha256 = (Get-StringSha256 -Value $TaskXml)
  }
}

function Assert-GateAcl {
  param([string]$Path, [string]$TaskIdentity)
  $TaskSid = (
    [System.Security.Principal.NTAccount]::new($TaskIdentity)
  ).Translate([System.Security.Principal.SecurityIdentifier]).Value
  $Allowed = @($TaskSid, "S-1-5-18", "S-1-5-32-544")
  $Acl = Get-Acl -LiteralPath $Path
  if (-not $Acl.AreAccessRulesProtected) {
    throw "F28 gate directory ACL still inherits permissions."
  }
  foreach ($Rule in $Acl.Access) {
    $Sid = $Rule.IdentityReference.Translate(
      [System.Security.Principal.SecurityIdentifier]
    ).Value
    if ($Rule.AccessControlType -ne "Allow" -or $Sid -notin $Allowed) {
      throw "F28 gate directory contains an unexpected ACL entry: $Sid"
    }
  }
  foreach ($Sid in $Allowed) {
    $Found = @($Acl.Access | Where-Object {
      $_.IdentityReference.Translate(
        [System.Security.Principal.SecurityIdentifier]
      ).Value -eq $Sid
    })
    if ($Found.Count -eq 0) { throw "F28 gate directory ACL is missing $Sid" }
  }
}

function Get-StringSha256 {
  param([string]$Value)
  $Bytes = [System.Text.Encoding]::UTF8.GetBytes($Value)
  $Hasher = [System.Security.Cryptography.SHA256]::Create()
  try { return ([BitConverter]::ToString($Hasher.ComputeHash($Bytes))).Replace("-", "").ToLowerInvariant() }
  finally { $Hasher.Dispose() }
}

function Get-FileManifest {
  param([string]$Root, [string]$ExcludeRoot = "")
  if (-not (Test-Path -LiteralPath $Root -PathType Container)) { return @() }
  $Prefix = $Root.TrimEnd("\") + "\"
  return @(
    Get-ChildItem -LiteralPath $Root -File -Recurse -Force | Where-Object {
      -not $ExcludeRoot -or -not $_.FullName.StartsWith(
        $ExcludeRoot.TrimEnd("\") + "\",
        [System.StringComparison]::OrdinalIgnoreCase
      )
    } | Sort-Object FullName | ForEach-Object {
      [ordered]@{
        path = $_.FullName.Substring($Prefix.Length).Replace("\", "/")
        sha256 = (Get-FileHash -LiteralPath $_.FullName -Algorithm SHA256).Hash.ToLowerInvariant()
        size = $_.Length
      }
    }
  )
}

function Get-PdfManifest {
  param([string[]]$Roots, [string]$ExcludeRoot)
  $Rows = [System.Collections.Generic.List[object]]::new()
  foreach ($Root in $Roots) {
    if (-not (Test-Path -LiteralPath $Root -PathType Container)) { continue }
    foreach ($File in Get-ChildItem -LiteralPath $Root -File -Recurse -Force -Filter "*.pdf") {
      if ($File.FullName.StartsWith(
        $ExcludeRoot.TrimEnd("\") + "\",
        [System.StringComparison]::OrdinalIgnoreCase
      )) { continue }
      $Rows.Add([ordered]@{
        path = $File.FullName
        sha256 = (Get-FileHash -LiteralPath $File.FullName -Algorithm SHA256).Hash.ToLowerInvariant()
        size = $File.Length
      })
    }
  }
  return @($Rows | Sort-Object path)
}

function Assert-VerifiedRollbackBackup {
  param([string]$Path)
  $ManifestPath = Join-Path $Path "backup-manifest.json"
  $SidecarPath = "$ManifestPath.sha256"
  $CompletePath = Join-Path $Path "backup-complete.json"
  foreach ($Required in @($ManifestPath, $SidecarPath, $CompletePath)) {
    if (-not (Test-Path -LiteralPath $Required -PathType Leaf)) {
      throw "Rollback backup is missing required evidence: $Required"
    }
  }
  $ManifestHash = (Get-FileHash -LiteralPath $ManifestPath -Algorithm SHA256).Hash.ToLowerInvariant()
  $Sidecar = (Get-Content -LiteralPath $SidecarPath -Raw).Trim()
  if ($Sidecar -cne "$ManifestHash  backup-manifest.json") {
    throw "Rollback backup manifest sidecar does not match."
  }
  $Complete = Get-Content -LiteralPath $CompletePath -Raw | ConvertFrom-Json
  if (
    [string]$Complete.status -cne "complete" -or
    [string]$Complete.manifest_sha256 -cne $ManifestHash
  ) { throw "Rollback backup completion marker does not match its manifest." }
  $Manifest = Get-Content -LiteralPath $ManifestPath -Raw | ConvertFrom-Json
  $Prefix = $Path.TrimEnd("\") + "\"
  foreach ($Member in @($Manifest.files)) {
    $Relative = ([string]$Member.path).Replace("/", "\")
    $Resolved = [System.IO.Path]::GetFullPath((Join-Path $Path $Relative))
    if (-not $Resolved.StartsWith(
      $Prefix, [System.StringComparison]::OrdinalIgnoreCase
    )) { throw "Rollback backup manifest member escapes its root: $Relative" }
    if (-not (Test-Path -LiteralPath $Resolved -PathType Leaf)) {
      throw "Rollback backup manifest member is missing: $Relative"
    }
    $ActualHash = (Get-FileHash -LiteralPath $Resolved -Algorithm SHA256).Hash.ToLowerInvariant()
    $ActualSize = (Get-Item -LiteralPath $Resolved -Force).Length
    if (
      $ActualHash -cne [string]$Member.sha256 -or
      $ActualSize -ne [long]$Member.size
    ) { throw "Rollback backup manifest member differs: $Relative" }
  }
}

function Get-RuntimeEvidence {
  param([string]$Root)
  $Processes = @(Get-CimInstance Win32_Process)
  $Children = @{}
  foreach ($Process in $Processes) {
    $Key = [string]$Process.ParentProcessId
    if (-not $Children.ContainsKey($Key)) {
      $Children[$Key] = [System.Collections.Generic.List[object]]::new()
    }
    $Children[$Key].Add($Process)
  }
  $Prefix = $Root.TrimEnd("\") + "\"
  $Seeds = @($Processes | Where-Object {
    [string]$_.Name -ieq "oms-hub.exe" -and
    [string]$_.ExecutablePath -and
    ([string]$_.ExecutablePath).StartsWith(
      $Prefix, [System.StringComparison]::OrdinalIgnoreCase
    )
  })
  if ($Seeds.Count -ne 1) {
    throw "Expected exactly one same-root oms-hub.exe; found $($Seeds.Count)."
  }
  $Pending = [System.Collections.Generic.Queue[object]]::new()
  $Pending.Enqueue($Seeds[0])
  $Seen = [System.Collections.Generic.HashSet[int]]::new()
  $Result = [System.Collections.Generic.List[object]]::new()
  while ($Pending.Count -gt 0) {
    $Process = $Pending.Dequeue()
    if (-not $Seen.Add([int]$Process.ProcessId)) { continue }
    $Result.Add([ordered]@{
      pid = [int]$Process.ProcessId
      parent_pid = [int]$Process.ParentProcessId
      name = [string]$Process.Name
      executable_path = [string]$Process.ExecutablePath
      creation_date = (ConvertTo-UtcIso8601 -Value $Process.CreationDate)
    })
    $Key = [string]$Process.ProcessId
    if ($Children.ContainsKey($Key)) {
      foreach ($Child in $Children[$Key]) { $Pending.Enqueue($Child) }
    }
  }
  $Snapshot = @($Processes | ForEach-Object {
    [ordered]@{
      pid = [int]$_.ProcessId
      parent_pid = [int]$_.ParentProcessId
      name = [string]$_.Name
      executable_path = [string]$_.ExecutablePath
      creation_date = (ConvertTo-UtcIso8601 -Value $_.CreationDate)
    }
  })
  return [ordered]@{
    runtime = @($Result)
    process_snapshot = $Snapshot
  }
}

function Get-TaskSchedulerEventCursor {
  $RecordIds = @(
    Get-WinEvent -LogName "Microsoft-Windows-TaskScheduler/Operational" -ErrorAction Stop |
      ForEach-Object {
        if ($null -eq $_.RecordId) {
          throw "Task Scheduler operational EventRecordID cursor could not be captured."
        }
        [long]$_.RecordId
      }
  )
  if ($RecordIds.Count -eq 0) {
    return [long]0
  }
  return [long](($RecordIds | Measure-Object -Maximum).Maximum)
}

function Get-TaskSchedulerXmlEvidence {
  param([long]$Cursor)
  return @(
    Get-WinEvent -LogName "Microsoft-Windows-TaskScheduler/Operational" -ErrorAction Stop |
      Where-Object { [long]$_.RecordId -gt $Cursor } |
      Sort-Object RecordId |
      ForEach-Object {
        [ordered]@{
          event_record_id = [long]$_.RecordId
          xml = $_.ToXml()
        }
      }
  )
}

function Get-ReadyHealth {
  param([int]$Port)
  return Invoke-RestMethod -Uri "http://127.0.0.1:$Port/health/ready" -TimeoutSec 3
}

function Assert-ExactHealth {
  param([object]$Health)
  if (
    [string]$Health.status -cne "ok" -or
    $Health.database_reachable -ne $true -or
    [string]$Health.build_revision -cne $ExpectedRevision -or
    [string]$Health.build_tree -cne $ExpectedTree -or
    -not (Test-ExactJsonInteger -Value $Health.schema_version -Expected $ExpectedSchema)
  ) { throw "Readiness identity or schema differs from the approved runtime." }
  $WorkerNames = @($Health.workers.PSObject.Properties.Name | Sort-Object)
  if (($WorkerNames -join "|") -cne (($ExpectedWorkers | Sort-Object) -join "|")) {
    throw "Readiness does not expose exactly the three approved workers."
  }
  foreach ($Name in $ExpectedWorkers) {
    $Worker = $Health.workers.$Name
    if (
      $Worker.alive -ne $true -or
      -not (Test-ExactJsonInteger -Value $Worker.start_count -Expected 1) -or
      $null -ne $Worker.active_work_age_seconds
    ) { throw "Worker $Name is not one healthy idle generation." }
  }
}

function Wait-ForFile {
  param([string]$Path, [datetime]$Deadline, [string]$Label)
  while ((Get-Date).ToUniversalTime() -lt $Deadline) {
    if (Test-Path -LiteralPath $Path -PathType Leaf) { return }
    Start-Sleep -Milliseconds 250
  }
  throw "Timed out waiting for $Label at $Path"
}

function New-DatabaseSnapshotHash {
  param([string]$Destination)
  $Python = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
  $BackupHelper = Join-Path $ProjectRoot "scripts\backup-sqlite.py"
  & $Python $BackupHelper --source $DatabasePath --destination $Destination
  if ($LASTEXITCODE -ne 0) { throw "backup-sqlite.py snapshot failed." }
  return (Get-FileHash -LiteralPath $Destination -Algorithm SHA256).Hash.ToLowerInvariant()
}

if ($ExpectedRevision -notmatch "^[0-9a-f]{40}$") {
  throw "ExpectedRevision must be the exact lowercase full SHA."
}
if ($ExpectedTree -notmatch "^[0-9a-f]{40}$") {
  throw "ExpectedTree must be the exact lowercase full tree SHA."
}
if ($ExpectedSchema -le 0 -or $ArmTimeoutSeconds -le 0 -or $RestartTimeoutSeconds -le 0) {
  throw "Schema and timeout values must be positive."
}

$ProjectRoot = (Resolve-Path -LiteralPath $ProjectRoot).Path
$DataRoot = (Resolve-Path -LiteralPath $DataRoot).Path
$ExpectedBackupPath = (Resolve-Path -LiteralPath $ExpectedBackupPath).Path
$GateDirectory = Join-Path $DataRoot "acceptance\f28"
$BackupRoot = Join-Path $DataRoot "backups"
foreach ($Boundary in @(
  $ProjectRoot,
  $DataRoot,
  (Join-Path $DataRoot "acceptance"),
  $GateDirectory,
  $BackupRoot,
  $ExpectedBackupPath
)) {
  Assert-NotReparsePoint -Path $Boundary -Label "F28 path"
}
$ExpectedGate = [System.IO.Path]::GetFullPath((Join-Path $DataRoot "acceptance\f28"))
if (-not [string]::Equals(
  (Resolve-Path -LiteralPath $GateDirectory).Path,
  $ExpectedGate,
  [System.StringComparison]::OrdinalIgnoreCase
)) { throw "F28 gate directory escapes the effective data root." }
$ExpectedBackupPrefix = (Resolve-Path -LiteralPath $BackupRoot).Path.TrimEnd("\") + "\"
if (-not $ExpectedBackupPath.StartsWith(
  $ExpectedBackupPrefix, [System.StringComparison]::OrdinalIgnoreCase
)) { throw "Rollback backup must be a dated member beneath the effective backup root." }

$SourceBefore = Get-SourceIdentity -Root $ProjectRoot
if (
  $SourceBefore.revision -cne $ExpectedRevision -or
  $SourceBefore.tree -cne $ExpectedTree
) { throw "Installed source does not match the exact approved revision and tree." }
$TaskBefore = Get-TaskContract -Name $TaskName -Root $ProjectRoot -EffectiveDataRoot $DataRoot
Assert-GateAcl -Path $GateDirectory -TaskIdentity $TaskBefore.principal
$TaskLog = Get-WinEvent -ListLog "Microsoft-Windows-TaskScheduler/Operational"
if (-not $TaskLog.IsEnabled) { throw "Task Scheduler operational logging is disabled." }
$EnvPath = Join-Path $ProjectRoot ".env"
if (-not (Test-Path -LiteralPath $EnvPath -PathType Leaf)) { throw "Installed .env is missing." }
Assert-NotReparsePoint -Path $EnvPath -Label "Installed .env"
$EnvHashBefore = (Get-FileHash -LiteralPath $EnvPath -Algorithm SHA256).Hash.ToLowerInvariant()
Assert-VerifiedRollbackBackup -Path $ExpectedBackupPath
$BackupBefore = Get-FileManifest -Root $ExpectedBackupPath
if ($BackupBefore.Count -eq 0) { throw "Verified rollback backup is empty." }
$PdfBefore = Get-PdfManifest -Roots @($ProjectRoot, $DataRoot) -ExcludeRoot $GateDirectory
$PortValue = Get-DotEnvValue -Name "OMS_HUB_DASHBOARD_PORT" -Path $EnvPath
$Port = if ($PortValue) { [int]$PortValue } else { 8787 }
$DatabaseUrl = Get-DotEnvValue -Name "OMS_HUB_DATABASE_URL" -Path $EnvPath
if (-not $DatabaseUrl) { $DatabaseUrl = "sqlite:///C:/ProgramData/OMSStudyHub/hub.db" }
$DatabasePath = Resolve-DatabasePath -DatabaseUrl $DatabaseUrl -Root $ProjectRoot
if (-not (Test-Path -LiteralPath $DatabasePath -PathType Leaf)) { throw "Live database is missing." }
Assert-NotReparsePoint -Path $DatabasePath -Label "Live database"
$OldRuntimeEvidence = Get-RuntimeEvidence -Root $ProjectRoot
$OldProcesses = @($OldRuntimeEvidence.runtime)
$HealthBefore = Get-ReadyHealth -Port $Port
Assert-ExactHealth -Health $HealthBefore

$Preflight = [ordered]@{
  marker = "F28_NATIVE_RESTART_PREFLIGHT_COMPLETE"
  nonce = $Nonce
  source = $SourceBefore
  task = $TaskBefore
  old_processes = $OldProcesses
  database_path = $DatabasePath
  env_sha256 = $EnvHashBefore
  rollback_backup = $ExpectedBackupPath
  rollback_backup_members = $BackupBefore.Count
  protected_pdfs = $PdfBefore.Count
}

if (-not $PSCmdlet.ShouldProcess(
  $TaskName,
  "Execute bounded F28 native exit 75 and Task Scheduler restart acceptance"
)) {
  $Preflight | ConvertTo-Json -Depth 20
  return
}

$RequestPath = Join-Path $GateDirectory "request.json"
$ActivePath = Join-Path $GateDirectory "active.json"
if ((Test-Path -LiteralPath $RequestPath) -or (Test-Path -LiteralPath $ActivePath)) {
  throw "Another F28 request is already pending or active."
}
$ArmedPath = Join-Path $GateDirectory "armed-$Nonce.json"
$FirePath = Join-Path $GateDirectory "fire-$Nonce.json"
$ServerExitPath = Join-Path $GateDirectory "server-exit-$Nonce.json"
$ConsumedLauncherServerExitPath = Join-Path $GateDirectory "consumed-launcher-server-exit-$Nonce.json"
$LauncherExitPath = Join-Path $GateDirectory "launcher-exit-$Nonce.json"
$ConsumedPath = Join-Path $GateDirectory "consumed-$Nonce.json"
$FinalizedPath = Join-Path $GateDirectory "finalized-$Nonce.json"
$DatabaseBeforePath = Join-Path $GateDirectory ".database-before-$Nonce.db"
$DatabaseAfterPath = Join-Path $GateDirectory ".database-after-$Nonce.db"
$DatabaseSnapshotsCleaned = $false
$SchedulerEvidencePath = Join-Path $GateDirectory "scheduler-evidence-$Nonce.json"
$SchedulerProofPath = Join-Path $GateDirectory "scheduler-proof-$Nonce.json"
$PythonExecutable = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $PythonExecutable -PathType Leaf)) {
  throw "F28 scheduler evidence verifier Python executable is missing."
}

try {
  $RequestIssuedAt = (Get-Date).ToUniversalTime()
  $Request = [ordered]@{
    schema_version = 1
    nonce = $Nonce
    expected_revision = $ExpectedRevision
    expected_tree = $ExpectedTree
    expected_schema = $ExpectedSchema
    exit_code = $FixedExitCode
    expires_at = $RequestIssuedAt.AddMinutes(5).ToString("o")
  }
  Write-AtomicJson -Path $RequestPath -Value $Request
  Wait-ForFile `
    -Path $ArmedPath `
    -Deadline $RequestIssuedAt.AddSeconds($ArmTimeoutSeconds) `
    -Label "worker-quiesced arm record"
  $Armed = Get-Content -LiteralPath $ArmedPath -Raw | ConvertFrom-Json
  if (
    -not (Test-ExactJsonInteger -Value $Armed.schema_version -Expected 1) -or
    [string]$Armed.nonce -cne $Nonce -or
    -not (Test-ExactJsonInteger -Value $Armed.exit_code -Expected $FixedExitCode) -or
    [string]$Armed.expected_revision -cne $ExpectedRevision -or
    [string]$Armed.expected_tree -cne $ExpectedTree -or
    -not (Test-ExactJsonInteger -Value $Armed.expected_schema -Expected $ExpectedSchema)
  ) { throw "Armed record differs from the exact request." }
  Assert-ExactHealth -Health $Armed.health
  $DatabaseHashBefore = New-DatabaseSnapshotHash -Destination $DatabaseBeforePath
  $ArmedHash = (Get-FileHash -LiteralPath $ArmedPath -Algorithm SHA256).Hash.ToLowerInvariant()
  $TaskSchedulerCursor = Get-TaskSchedulerEventCursor
  Write-AtomicJson -Path $FirePath -Value ([ordered]@{
    schema_version = 1
    nonce = $Nonce
    armed_sha256 = $ArmedHash
  })
  $FireWritten = $true

  $RestartDeadline = (Get-Date).ToUniversalTime().AddSeconds($RestartTimeoutSeconds)
  Wait-ForFile -Path $ServerExitPath -Deadline $RestartDeadline -Label "server exit record"
  Wait-ForFile `
    -Path $ConsumedLauncherServerExitPath `
    -Deadline $RestartDeadline `
    -Label "consumed launcher server exit record"
  Wait-ForFile -Path $LauncherExitPath -Deadline $RestartDeadline -Label "launcher exit record"
  Wait-ForFile -Path $FinalizedPath -Deadline $RestartDeadline -Label "finalized exit record"
  if (Test-Path -LiteralPath $ActivePath) {
    throw "F28 active state remains after server finalization."
  }
  if (-not (Test-Path -LiteralPath $ConsumedPath -PathType Leaf)) {
    throw "F28 consumed request record is missing after server finalization."
  }
  $ServerExit = Get-Content -LiteralPath $ServerExitPath -Raw | ConvertFrom-Json
  $ConsumedLauncherServerExit = Get-Content -LiteralPath $ConsumedLauncherServerExitPath -Raw | ConvertFrom-Json
  $LauncherExit = Get-Content -LiteralPath $LauncherExitPath -Raw | ConvertFrom-Json
  $Consumed = Get-Content -LiteralPath $ConsumedPath -Raw | ConvertFrom-Json
  $Finalized = Get-Content -LiteralPath $FinalizedPath -Raw | ConvertFrom-Json
  foreach ($Record in @($ServerExit, $ConsumedLauncherServerExit, $LauncherExit)) {
    if (
      -not (Test-ExactJsonInteger -Value $Record.schema_version -Expected 1) -or
      [string]$Record.nonce -cne $Nonce -or
      -not (Test-ExactJsonInteger -Value $Record.exit_code -Expected $FixedExitCode) -or
      [string]$Record.expected_revision -cne $ExpectedRevision -or
      [string]$Record.expected_tree -cne $ExpectedTree -or
      -not (Test-ExactJsonInteger -Value $Record.expected_schema -Expected $ExpectedSchema)
    ) { throw "Native exit evidence differs from the exact F28 request." }
  }
  if (
    -not (Test-ExactJsonInteger -Value $Consumed.schema_version -Expected 1) -or
    [string]$Consumed.nonce -cne $Nonce -or
    -not (Test-ExactJsonInteger -Value $Consumed.exit_code -Expected $FixedExitCode) -or
    [string]$Consumed.expected_revision -cne $ExpectedRevision -or
    [string]$Consumed.expected_tree -cne $ExpectedTree -or
    -not (Test-ExactJsonInteger -Value $Consumed.expected_schema -Expected $ExpectedSchema)
  ) { throw "Consumed request record differs from the exact F28 request." }
  $ServerExitHash = (Get-FileHash -LiteralPath $ServerExitPath -Algorithm SHA256).Hash.ToLowerInvariant()
  $ConsumedLauncherServerExitHash = (
    Get-FileHash -LiteralPath $ConsumedLauncherServerExitPath -Algorithm SHA256
  ).Hash.ToLowerInvariant()
  $ConsumedHash = (Get-FileHash -LiteralPath $ConsumedPath -Algorithm SHA256).Hash.ToLowerInvariant()
  if (
    [string]$LauncherExit.claimed_latest_server_exit -cne "consumed-launcher-server-exit-$Nonce.json" -or
    [string]$LauncherExit.claimed_latest_server_exit_sha256 -cne $ConsumedLauncherServerExitHash -or
    $ConsumedLauncherServerExitHash -cne [string]$Finalized.latest_server_exit_sha256 -or
    $ConsumedLauncherServerExitHash -cne $ServerExitHash
  ) { throw "Launcher evidence does not cryptographically bind the finalized server exit record." }
  if (
    -not (Test-ExactJsonInteger -Value $Finalized.schema_version -Expected 1) -or
    [string]$Finalized.nonce -cne $Nonce -or
    -not (Test-ExactJsonInteger -Value $Finalized.exit_code -Expected $FixedExitCode) -or
    [string]$Finalized.expected_revision -cne $ExpectedRevision -or
    [string]$Finalized.expected_tree -cne $ExpectedTree -or
    -not (Test-ExactJsonInteger -Value $Finalized.expected_schema -Expected $ExpectedSchema) -or
    [string]$Finalized.consumed -cne "consumed-$Nonce.json" -or
    [string]$Finalized.consumed_sha256 -cne $ConsumedHash -or
    [string]$Finalized.server_exit_sha256 -cne $ServerExitHash -or
    [string]$Finalized.latest_server_exit_sha256 -cne $ConsumedLauncherServerExitHash
  ) { throw "Finalized exit record does not prove exact consumed state." }
  $OldPids = @($OldProcesses | ForEach-Object { [int]$_.pid })
  if (
    -not (Test-JsonInteger -Value $ServerExit.server_pid) -or
    [long]$ServerExit.server_pid -notin $OldPids
  ) {
    throw "Server exit evidence does not identify the recorded old runtime tree."
  }

  $ObservedLastResults = [System.Collections.Generic.List[int]]::new()
  $ReplacementProcesses = $null
  $ReplacementProcessSnapshot = $null
  $HealthAfter = $null
  while ((Get-Date).ToUniversalTime() -lt $RestartDeadline) {
    $TaskInfo = Get-ScheduledTaskInfo -TaskName $TaskName
    $ObservedLastResults.Add([int]$TaskInfo.LastTaskResult)
    try {
      $CandidateRuntimeEvidence = Get-RuntimeEvidence -Root $ProjectRoot
      $CandidateProcesses = @($CandidateRuntimeEvidence.runtime)
      $NewPids = @($CandidateProcesses | ForEach-Object { [int]$_.pid })
      if (@($NewPids | Where-Object { $_ -in $OldPids }).Count -eq 0) {
        $CandidateHealth = Get-ReadyHealth -Port $Port
        Assert-ExactHealth -Health $CandidateHealth
        $ReplacementProcesses = $CandidateProcesses
        $ReplacementProcessSnapshot = @($CandidateRuntimeEvidence.process_snapshot)
        $HealthAfter = $CandidateHealth
        break
      }
    } catch {
      # Expected while Task Scheduler is between the failed action and restart.
    }
    Start-Sleep -Milliseconds 250
  }
  if (
    $null -eq $ReplacementProcesses -or
    $null -eq $ReplacementProcessSnapshot -or
    $null -eq $HealthAfter
  ) {
    throw "Task Scheduler did not restore a new exact healthy runtime before timeout."
  }
  if ($FixedExitCode -notin @($ObservedLastResults)) {
    throw "Task Scheduler LastTaskResult never exposed native exit code 75."
  }
  $ReplacementHubProcesses = @($ReplacementProcesses | Where-Object {
    [string]$_.name -ieq "oms-hub.exe"
  })
  if ($ReplacementHubProcesses.Count -ne 1) {
    throw "Replacement runtime does not expose exactly one oms-hub.exe PID."
  }
  $TaskSchedulerEvents = Get-TaskSchedulerXmlEvidence -Cursor $TaskSchedulerCursor
  if ($TaskSchedulerEvents.Count -eq 0) { throw "No Task Scheduler operational XML evidence was recorded." }
  # The typed verifier rejects Event 110/manual launch and Event 107/trigger launch,
  # then correlates Event 129 CreatedTaskProcess XML to the replacement process tree.
  Write-AtomicJson -Path $SchedulerEvidencePath -Value ([ordered]@{
    schema_version = 1
    nonce = $Nonce
    expected_revision = $ExpectedRevision
    expected_tree = $ExpectedTree
    cursor_event_record_id = $TaskSchedulerCursor
    events = $TaskSchedulerEvents
    process_snapshot = $ReplacementProcessSnapshot
  })
  & $PythonExecutable -m oms_hub.task_scheduler_evidence `
    --input $SchedulerEvidencePath `
    --output $SchedulerProofPath `
    --full-task-name $TaskBefore.full_task_name `
    --action-path $TaskBefore.action `
    --replacement-hub-pid ([int]$ReplacementHubProcesses[0].pid)
  if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $SchedulerProofPath -PathType Leaf)) {
    throw "Task Scheduler XML evidence did not prove automatic failure restart."
  }
  $SchedulerProof = Get-Content -LiteralPath $SchedulerProofPath -Raw | ConvertFrom-Json

  $DatabaseHashAfter = New-DatabaseSnapshotHash -Destination $DatabaseAfterPath
  if ($DatabaseHashAfter -cne $DatabaseHashBefore) { throw "Live database changed during F28 acceptance." }
  $SourceAfter = Get-SourceIdentity -Root $ProjectRoot
  if (
    $SourceAfter.revision -cne $SourceBefore.revision -or
    $SourceAfter.tree -cne $SourceBefore.tree
  ) { throw "Installed source identity changed during F28 acceptance." }
  $EnvHashAfter = (Get-FileHash -LiteralPath $EnvPath -Algorithm SHA256).Hash.ToLowerInvariant()
  if ($EnvHashAfter -cne $EnvHashBefore) { throw ".env changed during F28 acceptance." }
  $BackupAfter = Get-FileManifest -Root $ExpectedBackupPath
  if ((($BackupAfter | ConvertTo-Json -Depth 10 -Compress)) -cne (($BackupBefore | ConvertTo-Json -Depth 10 -Compress))) {
    throw "Rollback backup changed during F28 acceptance."
  }
  $PdfAfter = Get-PdfManifest -Roots @($ProjectRoot, $DataRoot) -ExcludeRoot $GateDirectory
  if ((($PdfAfter | ConvertTo-Json -Depth 10 -Compress)) -cne (($PdfBefore | ConvertTo-Json -Depth 10 -Compress))) {
    throw "Protected PDFs changed during F28 acceptance."
  }
  $TaskAfter = Get-TaskContract -Name $TaskName -Root $ProjectRoot -EffectiveDataRoot $DataRoot
  if ($TaskAfter.xml_sha256 -cne $TaskBefore.xml_sha256) {
    throw "Scheduled task configuration changed during F28 acceptance."
  }

  # Mandatory cleanup completes before durable success proof or stdout output.
  Remove-F28TemporaryDatabaseSnapshots `
    -Paths @($DatabaseBeforePath, $DatabaseAfterPath)
  $DatabaseSnapshotsCleaned = $true

  $Result = [ordered]@{
    marker = "F28_NATIVE_RESTART_$($ExpectedRevision.ToUpperInvariant())_VERIFIED_COMPLETE"
    nonce = $Nonce
    exit_code = $FixedExitCode
    source = $SourceAfter
    schema_version = $ExpectedSchema
    old_processes = $OldProcesses
    replacement_processes = $ReplacementProcesses
    workers = $HealthAfter.workers
    task_last_results = @($ObservedLastResults)
    scheduler = $SchedulerProof
    database_sha256 = $DatabaseHashAfter
    env_sha256 = $EnvHashAfter
    rollback_backup = $ExpectedBackupPath
    protected_pdf_count = $PdfAfter.Count
    completed_at = (Get-Date).ToUniversalTime().ToString("o")
  }
  $SuccessPath = Join-Path $GateDirectory "success-$Nonce.json"
  Write-AtomicJson -Path $SuccessPath -Value $Result
  $Result | ConvertTo-Json -Depth 20
} catch {
  $Failure = $_
  if (-not $FireWritten -and (Test-Path -LiteralPath $RequestPath -PathType Leaf)) {
    # Retain the unclaimed request as evidence; never delete acceptance records.
    try {
      $FailedBeforeFirePath = Join-Path $GateDirectory "failed-before-fire-$Nonce.json"
      if (Test-Path -LiteralPath $FailedBeforeFirePath) {
        throw "F28 pre-fire failure evidence destination already exists."
      }
      [System.IO.File]::Move($RequestPath, $FailedBeforeFirePath)
    } catch {
      Write-F28Diagnostic -Message (
        "F28 pre-fire request archival failed; rethrowing the original acceptance failure."
      )
    }
  }
  if ($FireWritten) {
    try {
      try {
        $RecoveryHealth = Get-ReadyHealth -Port $Port
        Assert-ExactHealth -Health $RecoveryHealth
      } catch {
        # RECOVERY ONLY: request the unchanged registered task when automatic
        # restart failed. Never stop, kill, register, or rewrite the task.
        Start-ScheduledTask -TaskName $TaskName
        $RecoveryDeadline = (Get-Date).ToUniversalTime().AddSeconds($RestartTimeoutSeconds)
        do {
          Start-Sleep -Seconds 1
          try {
            $RecoveryHealth = Get-ReadyHealth -Port $Port
            Assert-ExactHealth -Health $RecoveryHealth
            break
          } catch {
            if ((Get-Date).ToUniversalTime() -ge $RecoveryDeadline) {
              Write-F28Diagnostic -Message "F28 recovery could not restore exact readiness."
              break
            }
          }
        } while ($true)
      }
    } catch {
      Write-F28Diagnostic -Message (
        "F28 recovery attempt failed; rethrowing the original acceptance failure."
      )
    }
  }
  throw $Failure
} finally {
  if (-not $DatabaseSnapshotsCleaned) {
    try {
      Remove-F28TemporaryDatabaseSnapshots `
        -Paths @($DatabaseBeforePath, $DatabaseAfterPath) `
        -BestEffort
    } catch {
      # Best-effort cleanup must never replace the primary acceptance failure.
    }
  }
}
