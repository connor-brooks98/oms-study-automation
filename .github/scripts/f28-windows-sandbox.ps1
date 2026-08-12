[CmdletBinding()]
param(
  [Parameter(Mandatory = $true)]
  [string]$ProjectRoot,
  [Parameter(Mandatory = $true)]
  [string]$ExpectedRevision,
  [Parameter(Mandatory = $true)]
  [string]$ExpectedTree,
  [Parameter(Mandatory = $true)]
  [string]$EvidenceRoot
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
$TaskName = "OMS Study Hub V2"
$ExpectedSchema = 22
$Port = 18765
$SystemPowerShell = Join-Path `
  ([System.Environment]::SystemDirectory) `
  "WindowsPowerShell\v1.0\powershell.exe"
$DataRoot = Join-Path $env:RUNNER_TEMP "OMSStudyHub-F28-Sandbox"
$StudyRoot = Join-Path $env:RUNNER_TEMP "OMSStudyHub-F28-Study"
$StagingRoot = Join-Path $env:RUNNER_TEMP "OMSStudyHub-F28-Staging"
$PromptPath = Join-Path $env:RUNNER_TEMP "OMSStudyHub-F28-Prompt.md"
$DatabasePath = Join-Path $DataRoot "hub.db"
$InstallScript = Join-Path $ProjectRoot "scripts\install-windows.ps1"
$AcceptanceScript = Join-Path $ProjectRoot "scripts\accept-f28-restart.ps1"
$Utf8NoBom = New-Object System.Text.UTF8Encoding($false, $true)
$OriginalTaskLogEnabled = $null
$PrimaryFailure = $null
$RollbackBackup = $null

function Invoke-LoggedNative {
  param(
    [string]$Label,
    [string]$Executable,
    [string[]]$Arguments,
    [string]$LogPath
  )
  Write-Host "Running $Label..."
  $Writer = New-Object System.IO.StreamWriter($LogPath, $false, $Utf8NoBom)
  try {
    $Rendered = @(
      & $Executable @Arguments 2>&1 | ForEach-Object {
        $Line = [string]$_
        $Writer.WriteLine($Line)
        $Writer.Flush()
        Write-Host $Line
        $Line
      }
    )
    $ExitCode = $LASTEXITCODE
  } finally {
    $Writer.Dispose()
  }
  if ($ExitCode -ne 0) {
    throw "$Label failed with exit code $ExitCode."
  }
  return $Rendered
}

function Invoke-BestEffort {
  param([scriptblock]$Action, [string]$Label)
  try {
    & $Action
  } catch {
    [Console]::Error.WriteLine("Evidence capture failed for ${Label}: $($_.Exception.Message)")
  }
}

function Get-ExactHealth {
  $Health = Invoke-RestMethod `
    -Uri "http://127.0.0.1:$Port/health/ready" `
    -TimeoutSec 5
  $WorkerNames = @($Health.workers.PSObject.Properties.Name | Sort-Object)
  $ExpectedWorkers = @("generation_worker", "ingestion_worker", "studio_worker")
  if (
    [string]$Health.status -cne "ok" -or
    [string]$Health.deployment_root -cne $ProjectRoot -or
    [string]$Health.build_revision -cne $ExpectedRevision -or
    [string]$Health.build_tree -cne $ExpectedTree -or
    -not $Health.database_reachable -or
    [int]$Health.schema_version -ne $ExpectedSchema -or
    ($WorkerNames -join ",") -cne (($ExpectedWorkers | Sort-Object) -join ",")
  ) {
    throw "Sandbox readiness differs from the exact candidate contract."
  }
  foreach ($WorkerName in $ExpectedWorkers) {
    $Worker = $Health.workers.$WorkerName
    if (-not $Worker.alive -or [int]$Worker.start_count -ne 1) {
      throw "Sandbox worker $WorkerName is not one healthy generation."
    }
  }
  return $Health
}

try {
  New-Item -ItemType Directory -Force -Path `
    $EvidenceRoot, $DataRoot, $StudyRoot, $StagingRoot | Out-Null

  if (-not (Test-Path -LiteralPath $SystemPowerShell -PathType Leaf)) {
    throw "Windows PowerShell 5.1 executable is missing: $SystemPowerShell"
  }
  if ($ExpectedRevision -notmatch "^[0-9a-f]{40}$") {
    throw "ExpectedRevision must be a lowercase full SHA."
  }
  if ($ExpectedTree -notmatch "^[0-9a-f]{40}$") {
    throw "ExpectedTree must be a lowercase full tree SHA."
  }
  $ProjectRoot = (Resolve-Path -LiteralPath $ProjectRoot).Path
  $ActualRevision = (& git -C $ProjectRoot rev-parse HEAD).Trim()
  $ActualTree = (& git -C $ProjectRoot rev-parse "HEAD^{tree}").Trim()
  if ($LASTEXITCODE -ne 0 -or $ActualRevision -cne $ExpectedRevision -or $ActualTree -cne $ExpectedTree) {
    throw "Sandbox checkout does not match the exact candidate revision/tree."
  }
  $SourceStatus = @(
    & git -C $ProjectRoot status --porcelain=v1 --untracked-files=all -- src scripts pyproject.toml
  )
  if ($LASTEXITCODE -ne 0 -or $SourceStatus.Count -ne 0) {
    throw "Sandbox candidate source paths are not clean."
  }

  [System.IO.File]::WriteAllText(
    $PromptPath,
    "# Sandbox transcript prompt`n`nPreserve every substantive fact.`n",
    $Utf8NoBom
  )
  $PromptHash = (
    Get-FileHash -LiteralPath $PromptPath -Algorithm SHA256
  ).Hash.ToLowerInvariant()
  $DatabaseUrlPath = $DatabasePath.Replace("\", "/")
  $EnvironmentLines = @(
    "OMS_HUB_DATA_DIR=$DataRoot",
    "OMS_HUB_DATABASE_URL=sqlite:///$DatabaseUrlPath",
    "OMS_HUB_TIMEZONE=America/New_York",
    "OMS_HUB_DASHBOARD_HOST=127.0.0.1",
    "OMS_HUB_DASHBOARD_PORT=$Port",
    "OMS_HUB_STUDY_ROOT=$StudyRoot",
    "OMS_HUB_ICLOUD_STAGING_ROOT=$StagingRoot",
    "OMS_HUB_DOCUMENT_PARSER_MODE=legacy",
    "OMS_HUB_TRANSCRIPT_PROMPT_PATH=$PromptPath",
    "OMS_HUB_TRANSCRIPT_PROMPT_SHA256=$PromptHash",
    "OMS_HUB_PUBLIC_HOSTNAME=sandbox.example.com",
    "OMS_HUB_CLOUDFLARE_ACCESS_ISSUER=https://sandbox.cloudflareaccess.com",
    "OMS_HUB_CLOUDFLARE_ACCESS_AUDIENCE=sandbox-audience",
    "OMS_HUB_CLOUDFLARE_ACCESS_ALLOWED_EMAIL=sandbox@example.com",
    "OMS_HUB_ALLOW_LOCAL_ACCESS=true",
    "OMS_HUB_ANKI_ENABLED=false",
    "OMS_HUB_ANKI_CONNECT_URL=http://127.0.0.1:18766"
  )
  [System.IO.File]::WriteAllText(
    (Join-Path $ProjectRoot ".env"),
    (($EnvironmentLines -join "`n") + "`n"),
    $Utf8NoBom
  )

  $VenvPython = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
  & python -m venv (Join-Path $ProjectRoot ".venv")
  if ($LASTEXITCODE -ne 0) { throw "Python 3.12 virtual environment creation failed." }
  & $VenvPython -m pip install --upgrade pip
  if ($LASTEXITCODE -ne 0) { throw "Sandbox pip upgrade failed." }
  & $VenvPython -m pip install -e "${ProjectRoot}[document-processing]"
  if ($LASTEXITCODE -ne 0) { throw "Sandbox editable installation failed." }
  $HubExecutable = Join-Path $ProjectRoot ".venv\Scripts\oms-hub.exe"
  & $HubExecutable validate-config
  if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $DatabasePath -PathType Leaf)) {
    throw "Sandbox database migration/configuration seed failed."
  }

  $TaskLog = Get-WinEvent -ListLog "Microsoft-Windows-TaskScheduler/Operational"
  $OriginalTaskLogEnabled = [bool]$TaskLog.IsEnabled
  & wevtutil.exe sl "Microsoft-Windows-TaskScheduler/Operational" /e:true
  if ($LASTEXITCODE -ne 0) { throw "Task Scheduler Operational logging could not be enabled." }

  $InstallPreview = Invoke-LoggedNative `
    -Label "exact installer preview" `
    -Executable $SystemPowerShell `
    -Arguments @(
      "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", $InstallScript,
      "-ProjectRoot", $ProjectRoot, "-DataRoot", $DataRoot,
      "-WhatIf", '-Confirm:$false'
    ) `
    -LogPath (Join-Path $EvidenceRoot "installer-preview.log")
  if (($InstallPreview -join "`n") -notmatch "install preview complete") {
    throw "Installer preview did not publish its non-mutating completion marker."
  }

  $InstallOutput = Invoke-LoggedNative `
    -Label "exact live sandbox installation" `
    -Executable $SystemPowerShell `
    -Arguments @(
      "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", $InstallScript,
      "-ProjectRoot", $ProjectRoot, "-DataRoot", $DataRoot,
      '-Confirm:$false'
    ) `
    -LogPath (Join-Path $EvidenceRoot "installer-live.log")
  if (($InstallOutput -join "`n") -notmatch "Study Hub V2 install complete") {
    throw "Live installer did not publish its completion marker."
  }
  $Backups = @(Get-ChildItem -LiteralPath (Join-Path $DataRoot "backups") -Directory)
  if ($Backups.Count -ne 1) {
    throw "Expected exactly one fresh sandbox rollback backup; observed $($Backups.Count)."
  }
  $RollbackBackup = $Backups[0].FullName
  $null = Get-ExactHealth

  $AcceptanceBaseArguments = @(
    "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", $AcceptanceScript,
    "-ProjectRoot", $ProjectRoot,
    "-DataRoot", $DataRoot,
    "-TaskName", $TaskName,
    "-ExpectedRevision", $ExpectedRevision,
    "-ExpectedTree", $ExpectedTree,
    "-ExpectedSchema", [string]$ExpectedSchema,
    "-ExpectedBackupPath", $RollbackBackup,
    "-RestartTimeoutSeconds", "240"
  )
  $AcceptancePreview = Invoke-LoggedNative `
    -Label "exact F28 acceptance preview" `
    -Executable $SystemPowerShell `
    -Arguments @($AcceptanceBaseArguments + @("-WhatIf", '-Confirm:$false')) `
    -LogPath (Join-Path $EvidenceRoot "f28-preview.log")
  if (($AcceptancePreview -join "`n") -notmatch "F28_NATIVE_RESTART_PREFLIGHT_COMPLETE") {
    throw "F28 preview did not publish its exact preflight marker."
  }

  $AcceptanceOutput = Invoke-LoggedNative `
    -Label "exact native F28 controlled restart" `
    -Executable $SystemPowerShell `
    -Arguments @($AcceptanceBaseArguments + @('-Confirm:$false')) `
    -LogPath (Join-Path $EvidenceRoot "f28-native.log")
  $ExpectedMarker = "F28_NATIVE_RESTART_$($ExpectedRevision.ToUpperInvariant())_VERIFIED_COMPLETE"
  $EscapedExpectedMarker = [regex]::Escape($ExpectedMarker)
  if (($AcceptanceOutput -join "`n") -notmatch $EscapedExpectedMarker) {
    throw "F28 native sandbox did not publish the exact success marker."
  }
  $SuccessFiles = @(
    Get-ChildItem `
      -LiteralPath (Join-Path $DataRoot "acceptance\f28") `
      -Filter "success-*.json" `
      -File
  )
  if ($SuccessFiles.Count -ne 1) {
    throw "Expected exactly one durable F28 success record; observed $($SuccessFiles.Count)."
  }
  $FinalHealth = Get-ExactHealth
  [System.IO.File]::WriteAllText(
    (Join-Path $EvidenceRoot "final-health.json"),
    (($FinalHealth | ConvertTo-Json -Depth 20) + "`n"),
    $Utf8NoBom
  )
  [System.IO.File]::WriteAllText(
    (Join-Path $EvidenceRoot "verified-marker.txt"),
    ($ExpectedMarker + "`n"),
    $Utf8NoBom
  )
} catch {
  $PrimaryFailure = $_
} finally {
  Invoke-BestEffort -Label "candidate identity" -Action {
    $Identity = [ordered]@{
      expected_revision = $ExpectedRevision
      expected_tree = $ExpectedTree
      actual_revision = (& git -C $ProjectRoot rev-parse HEAD).Trim()
      actual_tree = (& git -C $ProjectRoot rev-parse "HEAD^{tree}").Trim()
      source_status = @(
        & git -C $ProjectRoot status --porcelain=v1 --untracked-files=all -- src scripts pyproject.toml
      )
    }
    [System.IO.File]::WriteAllText(
      (Join-Path $EvidenceRoot "source-identity.json"),
      (($Identity | ConvertTo-Json -Depth 10) + "`n"),
      $Utf8NoBom
    )
  }
  Invoke-BestEffort -Label "scheduled task XML" -Action {
    $TaskXml = Export-ScheduledTask -TaskName $TaskName
    [System.IO.File]::WriteAllText(
      (Join-Path $EvidenceRoot "scheduled-task.xml"),
      $TaskXml,
      $Utf8NoBom
    )
  }
  Invoke-BestEffort -Label "Task Scheduler event log" -Action {
    & wevtutil.exe epl `
      "Microsoft-Windows-TaskScheduler/Operational" `
      (Join-Path $EvidenceRoot "task-scheduler-operational.evtx") `
      /ow:true
    if ($LASTEXITCODE -ne 0) { throw "wevtutil export failed." }
  }
  Invoke-BestEffort -Label "F28 records" -Action {
    $GateDirectory = Join-Path $DataRoot "acceptance\f28"
    if (Test-Path -LiteralPath $GateDirectory) {
      Copy-Item `
        -LiteralPath $GateDirectory `
        -Destination (Join-Path $EvidenceRoot "f28") `
        -Recurse `
        -Force
    }
  }
  Invoke-BestEffort -Label "rollback proof" -Action {
    if ($RollbackBackup -and (Test-Path -LiteralPath $RollbackBackup)) {
      $RollbackEvidence = Join-Path $EvidenceRoot "rollback"
      New-Item -ItemType Directory -Force -Path $RollbackEvidence | Out-Null
      foreach ($Name in @(
        "backup-complete.json",
        "backup-manifest.json",
        "backup-manifest.json.sha256",
        "effective-config.json"
      )) {
        $Path = Join-Path $RollbackBackup $Name
        if (Test-Path -LiteralPath $Path -PathType Leaf) {
          Copy-Item -LiteralPath $Path -Destination $RollbackEvidence -Force
        }
      }
    }
  }
  if ($null -ne $OriginalTaskLogEnabled) {
    Invoke-BestEffort -Label "Task Scheduler log restoration" -Action {
      $EnabledValue = if ($OriginalTaskLogEnabled) { "true" } else { "false" }
      & wevtutil.exe sl "Microsoft-Windows-TaskScheduler/Operational" "/e:$EnabledValue"
      if ($LASTEXITCODE -ne 0) { throw "wevtutil restore failed." }
    }
  }
}

if ($null -ne $PrimaryFailure) {
  throw $PrimaryFailure
}

Write-Host "F28_WINDOWS_SANDBOX_$($ExpectedRevision.ToUpperInvariant())_VERIFIED_COMPLETE"
