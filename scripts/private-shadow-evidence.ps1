$script:PrivateShadowParseExit = 51
$script:PrivateShadowValidationExit = 52
$script:PrivateShadowWriteExit = 53
$script:PrivateShadowUtf8 = [System.Text.UTF8Encoding]::new($false, $true)

function New-PrivateShadowEvidenceResult {
  param(
    [int]$ExitCode,
    [string]$Stage,
    [bool]$EvidenceUsable
  )
  [pscustomobject]@{
    ExitCode = $ExitCode
    Stage = $Stage
    EvidenceUsable = $EvidenceUsable
  }
}

function Assert-PrivateShadowKeys {
  param(
    [Parameter(Mandatory = $true)][object]$Value,
    [Parameter(Mandatory = $true)][string[]]$Expected
  )
  $Actual = @($Value.PSObject.Properties.Name | Sort-Object)
  $Wanted = @($Expected | Sort-Object)
  if (($Actual -join "`n") -cne ($Wanted -join "`n")) {
    throw "Private-shadow evidence key set was invalid."
  }
}

function Assert-PrivateShadowInteger {
  param(
    [Parameter(Mandatory = $true)][object]$Value,
    [int64]$Minimum,
    [int64]$Maximum
  )
  $IntegerTypes = @(
    "System.Byte", "System.SByte", "System.Int16", "System.UInt16",
    "System.Int32", "System.UInt32", "System.Int64", "System.UInt64"
  )
  if ($null -eq $Value -or $Value.GetType().FullName -notin $IntegerTypes -or
      [int64]$Value -lt $Minimum -or [int64]$Value -gt $Maximum) {
    throw "Private-shadow evidence integer was invalid."
  }
}

function Assert-PrivateShadowStates {
  param([Parameter(Mandatory = $true)][object[]]$States)
  if ($States.Count -lt 1 -or $States.Count -gt 32 -or
      @($States | Where-Object {$_ -isnot [string]}).Count -ne 0) {
    throw "Private-shadow operation states were invalid."
  }
  $Fixed = @(
    "prior_operator_state_empty", "store_created", "positive_query_complete",
    "wrong_scope_query_complete", "file_reconciliation_empty",
    "store_reconciliation_empty", "private_shadow_failed"
  )
  foreach ($State in $States) {
    if ($State -in $Fixed) {
      continue
    }
    if ($State -notmatch '^((inputs_(uploaded|imported))|((documents|files|stores)_delete_attempted)):(0|[1-9][0-9]{0,4})$' -or
        [int]($State -replace '^.*:', '') -gt 10000) {
      throw "Private-shadow operation state was not allowlisted."
    }
  }
}

function Assert-PrivateShadowCommonRecord {
  param([Parameter(Mandatory = $true)][object]$Record)
  if ($Record.status -notin @("passed", "blocked") -or
      $Record.source_revision_hash -notmatch '^[0-9a-f]{64}$') {
    throw "Private-shadow evidence header was invalid."
  }
  $DocumentTypes = @($Record.document_types)
  $SortedTypes = @($DocumentTypes | Sort-Object -Unique)
  if ($DocumentTypes.Count -lt 1 -or $DocumentTypes.Count -gt 4 -or
      @($DocumentTypes | Where-Object {
          $_ -isnot [string] -or $_ -notin @("image", "markdown", "pdf", "pptx")
        }).Count -ne 0 -or
      ($DocumentTypes -join "`n") -cne ($SortedTypes -join "`n")) {
    throw "Private-shadow document types were invalid."
  }
  Assert-PrivateShadowInteger -Value $Record.page_count -Minimum 1 -Maximum 10000
  Assert-PrivateShadowInteger -Value $Record.slide_count -Minimum 1 -Maximum 10000
  Assert-PrivateShadowKeys -Value $Record.byte_usage -Expected @("index_inputs")
  Assert-PrivateShadowInteger `
    -Value $Record.byte_usage.index_inputs -Minimum 1 -Maximum 1099511627776
  Assert-PrivateShadowStates -States @($Record.provider_operation_states)
  if (@($Record.warnings | Where-Object {$_ -isnot [string]}).Count -ne 0) {
    throw "Private-shadow warnings were invalid."
  }
}

function Assert-PrivateShadowRecord {
  param(
    [Parameter(Mandatory = $true)][object]$Record,
    [Parameter(Mandatory = $true)][int]$ProcessExitCode
  )
  $SuccessKeys = @(
    "status", "source_revision_hash", "document_types", "page_count",
    "slide_count", "provider_operation_states", "citation_resolution_rate",
    "duration_ms", "byte_usage", "token_usage", "warnings"
  )
  $FailureKeys = @(
    "status", "source_revision_hash", "document_types", "page_count",
    "slide_count", "provider_operation_states", "byte_usage", "failure_stage",
    "provider_cleanup_outcome", "provider_reconciliation_outcome", "warnings"
  )
  if ($Record.status -ceq "passed") {
    Assert-PrivateShadowKeys -Value $Record -Expected $SuccessKeys
  } elseif ($Record.status -ceq "blocked") {
    Assert-PrivateShadowKeys -Value $Record -Expected $FailureKeys
  } else {
    throw "Private-shadow status was invalid."
  }
  Assert-PrivateShadowCommonRecord -Record $Record
  $States = @($Record.provider_operation_states)
  $Warnings = @($Record.warnings)
  if ($Record.status -ceq "passed") {
    $RateType = $Record.citation_resolution_rate.GetType().FullName
    if ($ProcessExitCode -ne 0 -or $Warnings.Count -ne 0 -or
        $States[-1] -ceq "private_shadow_failed" -or
        $States -notcontains "file_reconciliation_empty" -or
        $States -notcontains "store_reconciliation_empty" -or
        $RateType -notin @(
          "System.Decimal", "System.Double", "System.Single",
          "System.Int32", "System.Int64"
        ) -or [decimal]$Record.citation_resolution_rate -ne 1.0) {
      throw "Passed private-shadow evidence was inconsistent."
    }
    Assert-PrivateShadowInteger -Value $Record.duration_ms -Minimum 0 -Maximum 86400000
    Assert-PrivateShadowKeys -Value $Record.token_usage -Expected @("input", "output")
    Assert-PrivateShadowInteger `
      -Value $Record.token_usage.input -Minimum 0 -Maximum 1000000000
    Assert-PrivateShadowInteger `
      -Value $Record.token_usage.output -Minimum 0 -Maximum 1000000000
    return
  }

  $FailureStages = @(
    "prior_state_check", "create_store", "upload_input", "import_input",
    "wait_for_import", "positive_query", "positive_validation",
    "negative_query", "negative_validation", "cleanup", "unknown"
  )
  $AllowedWarnings = @(
    "citation_document_identity_unavailable", "citation_document_uri_absent",
    "citation_document_uri_invalid", "citation_excerpt_invalid",
    "citation_excerpt_absent", "citation_excerpt_mismatch",
    "citation_excerpt_unavailable", "citation_file_absent",
    "citation_file_invalid", "citation_metadata_invalid",
    "citation_metadata_absent", "citation_annotations_absent",
    "citation_annotations_invalid", "citation_content_absent",
    "citation_content_invalid", "citation_page_invalid", "citation_page_absent",
    "citation_page_mismatch", "citation_scope_mismatch", "citation_steps_absent",
    "citation_steps_invalid", "citation_wrong_document", "citation_wrong_file",
    "negative_answer_invalid", "positive_answer_invalid",
    "positive_answer_missing_marker", "positive_answer_unsupported",
    "positive_citation_missing", "positive_citation_unresolved",
    "private_cleanup_failed", "private_cleanup_unknown",
    "private_citation_unresolved", "private_reconciliation_failed",
    "private_shadow_failed", "private_usage_invalid",
    "private_wrong_scope_retrieved", "structured_output_invalid",
    "structured_output_absent", "structured_output_unavailable",
    "usage_input_absent", "usage_input_invalid", "usage_output_absent",
    "usage_output_invalid", "usage_count_invalid"
  )
  if ($ProcessExitCode -eq 0 -or $States[-1] -cne "private_shadow_failed" -or
      $Record.failure_stage -notin $FailureStages -or
      $Record.provider_cleanup_outcome -notin @("complete", "failed", "unknown") -or
      $Record.provider_reconciliation_outcome -notin @("empty", "not_empty", "unknown") -or
      $Warnings.Count -lt 1 -or $Warnings.Count -gt 2 -or
      @($Warnings | Where-Object {$_ -notin $AllowedWarnings}).Count -ne 0 -or
      @($Warnings | Sort-Object -Unique).Count -ne $Warnings.Count) {
    throw "Blocked private-shadow evidence was invalid."
  }
  if ($Record.provider_cleanup_outcome -ceq "complete" -and
      ($Warnings -contains "private_cleanup_failed" -or
       $Warnings -contains "private_cleanup_unknown")) {
    throw "Completed private-shadow cleanup carried a cleanup warning."
  }
  if ($Record.provider_cleanup_outcome -ceq "failed" -and
      $Warnings -notcontains "private_cleanup_failed") {
    throw "Failed private-shadow cleanup lacked its fixed warning."
  }
  if ($Record.provider_cleanup_outcome -ceq "unknown" -and
      $Warnings -notcontains "private_cleanup_unknown") {
    throw "Unknown private-shadow cleanup lacked its fixed warning."
  }
  if ($Record.provider_reconciliation_outcome -ceq "not_empty" -and
      $Record.provider_cleanup_outcome -cne "failed") {
    throw "Nonempty private-shadow reconciliation was inconsistent."
  }
  if ($Record.failure_stage -ceq "cleanup" -and
      $Warnings[0] -cne "private_cleanup_failed") {
    throw "Cleanup-only private-shadow failure lost primary precedence."
  }
}

function ConvertTo-PrivateShadowOrderedRecord {
  param([Parameter(Mandatory = $true)][object]$Record)
  $Common = [ordered]@{
    status = $Record.status
    source_revision_hash = $Record.source_revision_hash
    document_types = @($Record.document_types)
    page_count = $Record.page_count
    slide_count = $Record.slide_count
    provider_operation_states = @($Record.provider_operation_states)
  }
  if ($Record.status -ceq "passed") {
    $Common.citation_resolution_rate = $Record.citation_resolution_rate
    $Common.duration_ms = $Record.duration_ms
    $Common.byte_usage = [ordered]@{index_inputs=$Record.byte_usage.index_inputs}
    $Common.token_usage = [ordered]@{
      input = $Record.token_usage.input
      output = $Record.token_usage.output
    }
  } else {
    $Common.byte_usage = [ordered]@{index_inputs=$Record.byte_usage.index_inputs}
    $Common.failure_stage = $Record.failure_stage
    $Common.provider_cleanup_outcome = $Record.provider_cleanup_outcome
    $Common.provider_reconciliation_outcome = $Record.provider_reconciliation_outcome
  }
  $Common.warnings = @($Record.warnings)
  return $Common
}

function Write-PrivateShadowSafeResult {
  param(
    [Parameter(Mandatory = $true)][string]$Path,
    [Parameter(Mandatory = $true)][object]$Record
  )
  if (Test-Path -LiteralPath $Path) {
    throw "Private-shadow safe result destination already exists."
  }
  $Parent = Split-Path -Parent $Path
  if (-not (Test-Path -LiteralPath $Parent -PathType Container)) {
    throw "Private-shadow safe result parent does not exist."
  }
  $Temporary = Join-Path $Parent (".{0}.tmp" -f [Guid]::NewGuid().ToString("N"))
  try {
    $Payload = ConvertTo-PrivateShadowOrderedRecord -Record $Record |
      ConvertTo-Json -Compress -Depth 5
    [System.IO.File]::WriteAllText(
      $Temporary, $Payload + "`n", $script:PrivateShadowUtf8
    )
    [System.IO.File]::Move($Temporary, $Path)
  } finally {
    Remove-Item -LiteralPath $Temporary -Force -ErrorAction SilentlyContinue
  }
}

function Convert-PrivateShadowEvidence {
  [CmdletBinding()]
  param(
    [Parameter(Mandatory = $true)][string]$RawStdoutPath,
    [Parameter(Mandatory = $true)][string]$SafeResultPath,
    [Parameter(Mandatory = $true)][int]$ProcessExitCode
  )
  try {
    $Raw = [System.IO.File]::ReadAllText(
      $RawStdoutPath, $script:PrivateShadowUtf8
    ).TrimEnd("`r", "`n")
    if ([string]::IsNullOrWhiteSpace($Raw) -or $Raw -match "`r|`n") {
      throw "Private-shadow stdout was not one JSON record."
    }
    $Record = $Raw | ConvertFrom-Json -ErrorAction Stop
  } catch {
    return New-PrivateShadowEvidenceResult `
      -ExitCode $script:PrivateShadowParseExit -Stage "parse" -EvidenceUsable $false
  }
  try {
    Assert-PrivateShadowRecord -Record $Record -ProcessExitCode $ProcessExitCode
  } catch {
    return New-PrivateShadowEvidenceResult `
      -ExitCode $script:PrivateShadowValidationExit `
      -Stage "validation" -EvidenceUsable $false
  }
  try {
    Write-PrivateShadowSafeResult -Path $SafeResultPath -Record $Record
  } catch {
    return New-PrivateShadowEvidenceResult `
      -ExitCode $script:PrivateShadowWriteExit `
      -Stage "safe_result_write" -EvidenceUsable $false
  }
  return New-PrivateShadowEvidenceResult `
    -ExitCode 0 -Stage "complete" -EvidenceUsable $true
}
