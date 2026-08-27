Set-StrictMode -Version Latest

$script:ReconciliationParseExit = 41
$script:ReconciliationValidationExit = 42
$script:ReconciliationWriteExit = 43
$script:Utf8NoBom = [System.Text.UTF8Encoding]::new($false, $true)

function Write-ReconciliationStage {
  param(
    [Parameter(Mandatory = $true)][string]$Path,
    [Parameter(Mandatory = $true)][string]$Stage
  )
  $Payload = [ordered]@{schema_version=1;stage=$Stage} | ConvertTo-Json -Compress
  [System.IO.File]::WriteAllText($Path, $Payload + "`n", $script:Utf8NoBom)
}

function New-ReconciliationEvidenceResult {
  param(
    [int]$ExitCode,
    [string]$Stage,
    [bool]$EvidenceUsable,
    [bool]$RetainRaw,
    [int]$PrefixLineCount = 0
  )
  [pscustomobject]@{
    ExitCode = $ExitCode
    Stage = $Stage
    EvidenceUsable = $EvidenceUsable
    RetainRaw = $RetainRaw
    PrefixLineCount = $PrefixLineCount
  }
}

function Assert-ReconciliationKeys {
  param(
    [Parameter(Mandatory = $true)][object]$Value,
    [Parameter(Mandatory = $true)][string[]]$Expected
  )
  $Actual = @($Value.PSObject.Properties.Name | Sort-Object)
  $Wanted = @($Expected | Sort-Object)
  if (($Actual -join "`n") -cne ($Wanted -join "`n")) {
    throw "Reconciliation evidence key set was invalid."
  }
}

function Assert-ReconciliationCountMap {
  param(
    [Parameter(Mandatory = $true)][object]$Value,
    [Parameter(Mandatory = $true)][string[]]$Keys
  )
  Assert-ReconciliationKeys -Value $Value -Expected $Keys
  $IntegerTypes = @(
    "System.Byte", "System.SByte", "System.Int16", "System.UInt16",
    "System.Int32", "System.UInt32", "System.Int64", "System.UInt64"
  )
  foreach ($Key in $Keys) {
    $Count = $Value.$Key
    if ($Count.GetType().FullName -notin $IntegerTypes -or
        [int64]$Count -lt 0 -or [int64]$Count -gt 1000) {
      throw "Reconciliation evidence count was invalid."
    }
  }
}

function Assert-ReconciliationRecord {
  param([Parameter(Mandatory = $true)][object]$Record)
  Assert-ReconciliationKeys -Value $Record -Expected @(
    "schema_version", "status", "provider_operation_states", "inspected_counts",
    "matched_counts", "delete_attempt_counts", "remaining_counts",
    "provider_cleanup_complete", "warnings"
  )
  if ($Record.schema_version.GetType().FullName -notin @("System.Int32", "System.Int64") -or
      $Record.schema_version -ne 1 -or
      $Record.status -notin @("passed", "blocked") -or
      $Record.provider_cleanup_complete -isnot [bool]) {
    throw "Reconciliation evidence header was invalid."
  }
  Assert-ReconciliationCountMap -Value $Record.inspected_counts `
    -Keys @("stores", "files", "documents")
  Assert-ReconciliationCountMap -Value $Record.matched_counts `
    -Keys @("stores", "files", "documents")
  Assert-ReconciliationCountMap -Value $Record.delete_attempt_counts `
    -Keys @("stores", "files", "documents")
  Assert-ReconciliationCountMap -Value $Record.remaining_counts -Keys @(
    "stores", "files", "documents", "stores_inspected", "files_inspected",
    "documents_inspected"
  )
  $States = @($Record.provider_operation_states)
  $Warnings = @($Record.warnings)
  if (@($States | Where-Object {$_ -isnot [string]}).Count -ne 0 -or
      @($Warnings | Where-Object {$_ -isnot [string]}).Count -ne 0) {
    throw "Reconciliation evidence arrays were invalid."
  }
  $PassedStates = @("inventory_complete", "deletes_attempted", "reconciliation_empty")
  $BlockedStates = @("inventory_complete", "deletes_attempted", "reconciliation_incomplete")
  $AllowedWarnings = @(
    "provider_reconciliation_scope_exceeded",
    "provider_reconciliation_contract_invalid",
    "provider_reconciliation_incomplete",
    "provider_reconciliation_sdk_mismatch",
    "provider_reconciliation_model_mismatch",
    "provider_reconciliation_failed"
  )
  if ($Record.status -ceq "passed") {
    if (-not $Record.provider_cleanup_complete -or $Warnings.Count -ne 0 -or
        ($States -join "`n") -cne ($PassedStates -join "`n")) {
      throw "Passed reconciliation evidence was inconsistent."
    }
  } elseif ($Record.provider_cleanup_complete -or $Warnings.Count -ne 1 -or
            $Warnings[0] -notin $AllowedWarnings -or
            (($States -join "`n") -cne ($BlockedStates -join "`n") -and
             ($States -join "`n") -cne "inventory_failed")) {
    throw "Blocked reconciliation evidence was inconsistent."
  }
}

function Write-ReconciliationSafeResult {
  param(
    [Parameter(Mandatory = $true)][string]$Path,
    [Parameter(Mandatory = $true)][object]$Record,
    [Parameter(Mandatory = $true)][int]$PrefixLineCount
  )
  if (Test-Path -LiteralPath $Path) {
    throw "Safe result destination must not already exist."
  }
  $Parent = Split-Path -Parent $Path
  if (-not (Test-Path -LiteralPath $Parent -PathType Container)) {
    throw "Safe result parent does not exist."
  }
  $Warnings = if ($PrefixLineCount -gt 0) {@("unexpected_stdout_prefix")} else {@()}
  $Envelope = [ordered]@{
    schema_version = 1
    evidence_usable = $true
    wrapper_stage = "complete"
    stdout_prefix_line_count = $PrefixLineCount
    wrapper_warnings = $Warnings
    operator_result = $Record
  }
  $Temporary = Join-Path $Parent (".{0}.tmp" -f [Guid]::NewGuid().ToString("N"))
  try {
    $Payload = $Envelope | ConvertTo-Json -Compress -Depth 8
    $Stream = [System.IO.File]::Open(
      $Temporary,
      [System.IO.FileMode]::CreateNew,
      [System.IO.FileAccess]::Write,
      [System.IO.FileShare]::None
    )
    try {
      $Bytes = $script:Utf8NoBom.GetBytes($Payload + "`n")
      $Stream.Write($Bytes, 0, $Bytes.Length)
      $Stream.Flush($true)
    } finally {
      $Stream.Dispose()
    }
    [System.IO.File]::Move($Temporary, $Path)
  } finally {
    Remove-Item -LiteralPath $Temporary -Force -ErrorAction SilentlyContinue
  }
}

function Convert-GeminiReconciliationEvidence {
  [CmdletBinding()]
  param(
    [Parameter(Mandatory = $true)][string]$RawStdoutPath,
    [Parameter(Mandatory = $true)][string]$SafeResultPath,
    [Parameter(Mandatory = $true)][string]$StageMarkerPath
  )

  try {
    Write-ReconciliationStage -Path $StageMarkerPath -Stage "parse"
    $Raw = [System.IO.File]::ReadAllText($RawStdoutPath, $script:Utf8NoBom)
    $Trimmed = $Raw.TrimEnd("`r", "`n")
    if ([string]::IsNullOrWhiteSpace($Trimmed)) {
      throw "Reconciliation stdout was empty."
    }
    $Lines = @($Trimmed -split "`r?`n")
    $PrefixLineCount = [Math]::Max(0, $Lines.Count - 1)
    $Record = $Lines[-1] | ConvertFrom-Json -ErrorAction Stop
  } catch {
    return New-ReconciliationEvidenceResult `
      -ExitCode $script:ReconciliationParseExit -Stage "parse" `
      -EvidenceUsable $false -RetainRaw $true
  }

  try {
    Write-ReconciliationStage -Path $StageMarkerPath -Stage "validation"
    Assert-ReconciliationRecord -Record $Record
  } catch {
    return New-ReconciliationEvidenceResult `
      -ExitCode $script:ReconciliationValidationExit -Stage "validation" `
      -EvidenceUsable $false -RetainRaw $true -PrefixLineCount $PrefixLineCount
  }

  try {
    Write-ReconciliationStage -Path $StageMarkerPath -Stage "safe_result_write"
    Write-ReconciliationSafeResult `
      -Path $SafeResultPath -Record $Record -PrefixLineCount $PrefixLineCount
  } catch {
    return New-ReconciliationEvidenceResult `
      -ExitCode $script:ReconciliationWriteExit -Stage "safe_result_write" `
      -EvidenceUsable $false -RetainRaw $true -PrefixLineCount $PrefixLineCount
  }

  return New-ReconciliationEvidenceResult `
    -ExitCode 0 -Stage "complete" -EvidenceUsable $true `
    -RetainRaw ($PrefixLineCount -gt 0) -PrefixLineCount $PrefixLineCount
}
