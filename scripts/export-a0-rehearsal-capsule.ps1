[CmdletBinding()]
param(
    [Parameter(Mandatory)][string]$FrozenRepository,
    [Parameter(Mandatory)][string]$ExpectedFrozenCommit,
    [Parameter(Mandatory)][string]$ExpectedFrozenTree,
    [Parameter(Mandatory)][string]$ToolRepository,
    [Parameter(Mandatory)][string]$ExpectedToolCommit,
    [Parameter(Mandatory)][string]$ExpectedToolTree,
    [Parameter(Mandatory)][string]$Python,
    [Parameter(Mandatory)][string]$A0Data,
    [Parameter(Mandatory)][string]$ExportRoot,
    [Parameter(Mandatory)][string]$JobId
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$Capsule = Join-Path $ExportRoot "A0-rehearsal-capsule-$($ExpectedFrozenCommit.Substring(0, 7))"
$Archive = "$Capsule.zip"
$Summary = Join-Path $ExportRoot "A0-rehearsal-capsule-$($ExpectedFrozenCommit.Substring(0, 7))-export.json"
$StagingToken = [Guid]::NewGuid().ToString('N')
$StagingCapsule = Join-Path $ExportRoot ".A0-rehearsal-capsule-$($ExpectedFrozenCommit.Substring(0, 7))-$StagingToken"
$StagingArchive = "$StagingCapsule.zip"
$StagingSummary = "$StagingCapsule-export.json"
$Database = Join-Path $A0Data "hub.db"
$AnkiRoot = Join-Path $A0Data "anki"

foreach ($Required in @($FrozenRepository, $ToolRepository, $A0Data, $ExportRoot, $Python, $Database, $AnkiRoot)) {
    if (-not (Test-Path -LiteralPath $Required)) {
        throw "Required frozen export input is unavailable: $Required"
    }
}
foreach ($Absent in @($Capsule, $Archive, $Summary)) {
    if (Test-Path -LiteralPath $Absent) {
        throw "Refusing to overwrite prior capsule output: $Absent"
    }
}
foreach ($Absent in @($StagingCapsule, $StagingArchive, $StagingSummary)) {
    if (Test-Path -LiteralPath $Absent) {
        throw "Refusing to reuse generated staging export path: $Absent"
    }
}

function Assert-CleanGitIdentity([string]$Repository, [string]$ExpectedCommit, [string]$ExpectedTree, [string]$Label) {
    $ObservedCommit = (& git -C $Repository rev-parse HEAD).Trim()
    $ObservedTree = (& git -C $Repository rev-parse 'HEAD^{tree}').Trim()
    $StatusRows = @(& git -C $Repository status --porcelain)
    if ($LASTEXITCODE -ne 0 -or $ObservedCommit -ne $ExpectedCommit -or $ObservedTree -ne $ExpectedTree) {
        throw "$Label Git identity does not match the operator-supplied boundary"
    }
    if ($StatusRows.Count -ne 0) { throw "$Label checkout is not clean" }
    return [ordered]@{ commit = $ObservedCommit; tree = $ObservedTree; clean = $true }
}

function Resolve-Directory([string]$Candidate, [string]$Label) {
    $Item = Get-Item -LiteralPath $Candidate
    if (-not $Item.PSIsContainer) { throw "$Label is not a directory: $Candidate" }
    return (Resolve-Path -LiteralPath $Candidate).Path
}

function Assert-ResolvedDescendant([string]$Candidate, [string]$Root, [string]$Label) {
    $ResolvedCandidate = [IO.Path]::GetFullPath($Candidate)
    $ResolvedRoot = [IO.Path]::GetFullPath($Root)
    $Prefix = $ResolvedRoot.TrimEnd([IO.Path]::DirectorySeparatorChar, [IO.Path]::AltDirectorySeparatorChar) + [IO.Path]::DirectorySeparatorChar
    if (-not $ResolvedCandidate.StartsWith($Prefix, [StringComparison]::OrdinalIgnoreCase)) {
        throw "$Label is outside the verified tool implementation source"
    }
    return $ResolvedCandidate
}

$ResolvedFrozenRepository = Resolve-Directory $FrozenRepository "Frozen source-data repository"
$ResolvedToolRepository = Resolve-Directory $ToolRepository "Tool implementation repository"
$ResolvedToolSource = Resolve-Directory (Join-Path $ResolvedToolRepository "src") "Tool implementation source"
$FrozenGit = Assert-CleanGitIdentity $ResolvedFrozenRepository $ExpectedFrozenCommit $ExpectedFrozenTree "Frozen source-data repository"
$ToolGit = Assert-CleanGitIdentity $ResolvedToolRepository $ExpectedToolCommit $ExpectedToolTree "Tool implementation repository"

# Isolated mode ignores ambient PYTHONPATH and the current directory.  The
# verified source is inserted explicitly *after* Git identity verification.
# ``-S`` is intentionally not added: this exporter needs the selected
# interpreter's installed third-party dependencies, while ``-I`` provides the
# ambient-import isolation required at this boundary.
$ImportProbe = 'import hashlib,json,pathlib,sys; source=pathlib.Path(sys.argv[1]).resolve(); sys.path.insert(0,str(source)); import oms_hub; from oms_hub.anki.rehearsal import export; package=pathlib.Path(oms_hub.__file__).resolve(); module=pathlib.Path(export.__file__).resolve(); print(json.dumps({"python":sys.executable,"tool_source":str(source),"oms_hub":str(package),"export":str(module),"oms_hub_sha256":hashlib.sha256(package.read_bytes()).hexdigest(),"export_sha256":hashlib.sha256(module.read_bytes()).hexdigest()},sort_keys=True))'
$ImportOrigin = & $Python -I -c $ImportProbe $ResolvedToolSource
if ($LASTEXITCODE -ne 0) { throw "Trusted isolated Python cannot import the verified tool implementation" }
$ImportOriginDocument = $ImportOrigin | ConvertFrom-Json
$VerifiedPackageOrigin = Assert-ResolvedDescendant ([string]$ImportOriginDocument.oms_hub) $ResolvedToolSource "Trusted Python oms_hub package"
$VerifiedExportOrigin = Assert-ResolvedDescendant ([string]$ImportOriginDocument.export) $ResolvedToolSource "Trusted Python rehearsal exporter"

function Get-DatabaseSidecarSnapshot([string]$Path) {
    $result = [ordered]@{}
    foreach ($Suffix in @('-wal', '-shm')) {
        $Sidecar = "${Path}$Suffix"
        if (Test-Path -LiteralPath $Sidecar -PathType Leaf) {
            $Item = Get-Item -LiteralPath $Sidecar
            $result[$Suffix] = [ordered]@{
                present = $true
                bytes = [int64]$Item.Length
                sha256 = (Get-FileHash -Algorithm SHA256 -LiteralPath $Sidecar).Hash.ToLowerInvariant()
            }
        } else {
            $result[$Suffix] = [ordered]@{ present = $false }
        }
    }
    return $result
}

function Assert-InertDatabaseSidecars($Snapshot) {
    $Wal = $Snapshot['-wal']
    $Shm = $Snapshot['-shm']
    if (-not $Wal -or -not $Shm) { throw "A0 database sidecar snapshot is malformed" }
    if (-not $Wal.present -and -not $Shm.present) { return "absent" }
    if (-not $Wal.present -or -not $Shm.present) {
        throw "A0 database sidecars are not the inert zero-WAL tuple"
    }
    $EmptySha256 = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
    if ($Wal.bytes -ne 0 -or $Wal.sha256 -ne $EmptySha256) {
        throw "A0 database WAL is not inert"
    }
    if ($Shm.bytes -lt 0 -or [string]::IsNullOrWhiteSpace([string]$Shm.sha256)) {
        throw "A0 database SHM sidecar is malformed"
    }
    return "inert_zero_wal_with_shm"
}

function Remove-ReadonlyExportPath([string]$Path, [string]$ExpectedSha256, [bool]$IsDirectory) {
    if (-not (Test-Path -LiteralPath $Path)) { return }
    if ($IsDirectory) {
        $Manifest = Join-Path $Path "capsule.json"
        if (-not (Test-Path -LiteralPath $Manifest -PathType Leaf) -or
            (Get-FileHash -Algorithm SHA256 -LiteralPath $Manifest).Hash.ToLowerInvariant() -cne $ExpectedSha256) {
            throw "refusing to remove unexpected published capsule: $Path"
        }
        $Entries = @(
            Get-Item -LiteralPath $Path -Force
            Get-ChildItem -LiteralPath $Path -Force -Recurse
        )
        foreach ($Entry in $Entries) { $Entry.IsReadOnly = $false }
        Remove-Item -LiteralPath $Path -Force -Recurse
    } else {
        if ((Get-FileHash -Algorithm SHA256 -LiteralPath $Path).Hash.ToLowerInvariant() -cne $ExpectedSha256) {
            throw "refusing to remove unexpected published output: $Path"
        }
        (Get-Item -LiteralPath $Path -Force).IsReadOnly = $false
        Remove-Item -LiteralPath $Path -Force
    }
    if (Test-Path -LiteralPath $Path) { throw "failed to remove newly-created export output: $Path" }
}

function Remove-StagingExportPath([string]$Path) {
    if (-not (Test-Path -LiteralPath $Path)) { return }
    $Entries = @(
        Get-Item -LiteralPath $Path -Force
        if ((Get-Item -LiteralPath $Path -Force).PSIsContainer) {
            Get-ChildItem -LiteralPath $Path -Force -Recurse
        }
    )
    foreach ($Entry in $Entries) { $Entry.IsReadOnly = $false }
    Remove-Item -LiteralPath $Path -Force -Recurse
    if (Test-Path -LiteralPath $Path) { throw "failed to remove staging export output: $Path" }
}

$RunningIsolated = @(
    Get-CimInstance Win32_Process |
        Where-Object {
            ($_.Name -in @('oms-hub.exe', 'python.exe', 'pythonw.exe')) -and
            (($_.ExecutablePath -and $_.ExecutablePath.StartsWith($FrozenRepository, [StringComparison]::OrdinalIgnoreCase)) -or
             ($_.CommandLine -and ($_.CommandLine.IndexOf($Database, [StringComparison]::OrdinalIgnoreCase) -ge 0 -or $_.CommandLine.IndexOf($A0Data, [StringComparison]::OrdinalIgnoreCase) -ge 0)))
        }
)
if ($RunningIsolated.Count -ne 0) {
    $Pids = ($RunningIsolated | ForEach-Object { $_.ProcessId }) -join ', '
    throw "The bounded A0 source process set is not quiescent (PIDs: $Pids); stop it before read-only export"
}
# This wrapper proves bounded A0 process quiescence. The Python exporter cannot
# prove that native boundary; it proves the accepted sidecar tuple stays stable.
$InitialDatabaseSidecars = Get-DatabaseSidecarSnapshot $Database
$DatabaseSidecarState = Assert-InertDatabaseSidecars $InitialDatabaseSidecars

$PublishedCapsule = $false
$PublishedArchive = $false
$PublishedSummary = $false
$ManifestHash = $null
$ArchiveHash = $null
$SummaryHash = $null
try {
    Push-Location $ResolvedToolRepository
    try {
        $RunVerifiedExporter = 'import pathlib,runpy,sys; source=pathlib.Path(sys.argv[1]).resolve(); sys.path.insert(0,str(source)); del sys.argv[1]; runpy.run_module("oms_hub.anki.rehearsal.export",run_name="__main__")'
        & $Python -I -c $RunVerifiedExporter $ResolvedToolSource `
            --repository $FrozenRepository `
            --database $Database `
            --anki-root $AnkiRoot `
            --job-id $JobId `
            --destination $StagingCapsule `
            --source-root "a0data=$A0Data" `
            --source-root "repository=$FrozenRepository" `
            --commit $ExpectedFrozenCommit `
            --tree $ExpectedFrozenTree `
            --expected-companion-count 28258 `
            --expected-semantic-count 28257
        if ($LASTEXITCODE -ne 0) {
            throw "Capsule exporter failed with exit code $LASTEXITCODE"
        }
    } finally {
        Pop-Location
    }

    # Publication starts only after the final immutable source proof. This keeps
    # a rejected staging capsule from blocking a subsequent clean rerun.
    $FinalDatabaseSidecars = Get-DatabaseSidecarSnapshot $Database
    $FinalDatabaseSidecarState = Assert-InertDatabaseSidecars $FinalDatabaseSidecars
    if ($DatabaseSidecarState -ne $FinalDatabaseSidecarState -or
        (($InitialDatabaseSidecars | ConvertTo-Json -Depth 4 -Compress) -cne ($FinalDatabaseSidecars | ConvertTo-Json -Depth 4 -Compress))) {
        throw "A0 database sidecar tuple changed during read-only export"
    }

    $ZipCode = 'from pathlib import Path; import sys; source=Path(sys.argv[1]).resolve(); sys.path.insert(0,str(source)); del sys.argv[1]; from oms_hub.anki.rehearsal.capsule import write_deterministic_capsule_zip; write_deterministic_capsule_zip(Path(sys.argv[1]), Path(sys.argv[2]))'
    & $Python -I -c $ZipCode $ResolvedToolSource $StagingCapsule $StagingArchive
    if ($LASTEXITCODE -ne 0) { throw "Deterministic capsule ZIP write/verification failed" }
    $ArchiveHash = (Get-FileHash -Algorithm SHA256 -LiteralPath $StagingArchive).Hash.ToLowerInvariant()
    $ManifestHash = (Get-FileHash -Algorithm SHA256 -LiteralPath (Join-Path $StagingCapsule "capsule.json")).Hash.ToLowerInvariant()
    $Result = [ordered]@{
        result = "EXPORTED_READ_ONLY_CAPSULE"
        frozen_source_commit = $FrozenGit.commit
        frozen_source_tree = $FrozenGit.tree
        frozen_source_clean = $FrozenGit.clean
        tool_implementation_commit = $ToolGit.commit
        tool_implementation_tree = $ToolGit.tree
        tool_implementation_clean = $ToolGit.clean
        verified_tool_repository = $ResolvedToolRepository
        verified_tool_source = $ResolvedToolSource
        python_import_origin = $ImportOriginDocument
        verified_python_package_origin = $VerifiedPackageOrigin
        verified_python_export_origin = $VerifiedExportOrigin
        database_sidecar_state = $DatabaseSidecarState
        database_sidecars = $InitialDatabaseSidecars
        job_id = $JobId
        capsule_path = $Capsule
        capsule_manifest_sha256 = $ManifestHash
        archive_path = $Archive
        archive_sha256 = $ArchiveHash
        companion_note_count = 28258
        semantic_note_count = 28257
        production_touched = $false
        collection_exported = $false
        credentials_exported = $false
        credentials_export_evidence = "hub.db is a job-scoped allowlisted logical export; source-snapshot.json records this boundary"
    }
    $Result | ConvertTo-Json -Depth 4 | Set-Content -LiteralPath $StagingSummary -Encoding utf8
    $SummaryHash = (Get-FileHash -Algorithm SHA256 -LiteralPath $StagingSummary).Hash.ToLowerInvariant()

    [IO.Directory]::Move($StagingCapsule, $Capsule)
    $PublishedCapsule = $true
    [IO.File]::Move($StagingArchive, $Archive)
    $PublishedArchive = $true
    [IO.File]::Move($StagingSummary, $Summary)
    $PublishedSummary = $true
    $Result | ConvertTo-Json -Depth 4
} catch {
    $PrimaryError = $_
    $CleanupFailures = [Collections.Generic.List[string]]::new()
    foreach ($StagingPath in @($StagingSummary, $StagingArchive, $StagingCapsule)) {
        try { Remove-StagingExportPath $StagingPath } catch { $CleanupFailures.Add($_.Exception.Message) }
    }
    if ($PublishedSummary) {
        try { Remove-ReadonlyExportPath $Summary $SummaryHash $false } catch { $CleanupFailures.Add($_.Exception.Message) }
    }
    if ($PublishedArchive) {
        try { Remove-ReadonlyExportPath $Archive $ArchiveHash $false } catch { $CleanupFailures.Add($_.Exception.Message) }
    }
    if ($PublishedCapsule) {
        try { Remove-ReadonlyExportPath $Capsule $ManifestHash $true } catch { $CleanupFailures.Add($_.Exception.Message) }
    }
    if ($CleanupFailures.Count -ne 0) {
        $PrimaryError.Exception.Data['capsule_cleanup_failure'] = ($CleanupFailures -join '; ')
    }
    throw $PrimaryError
}
