[CmdletBinding()]
param(
  [Parameter(Mandatory = $true)][string]$RepositoryRoot,
  [Parameter(Mandatory = $true)][string]$PythonExecutable
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
$RepositoryRoot = (Get-Item -LiteralPath $RepositoryRoot -Force).FullName
$PythonExecutable = (Get-Item -LiteralPath $PythonExecutable -Force).FullName
$Composition = Join-Path $RepositoryRoot "scripts/task28/private-shadow-composition.ps1"
$Commit = (& git -C $RepositoryRoot rev-parse HEAD).Trim()
if ($LASTEXITCODE -ne 0 -or $Commit -notmatch "^[0-9a-f]{40}$") { throw "Harness commit is invalid." }
$Sandbox = Join-Path ([IO.Path]::GetTempPath()) ("oms-task28-composition-{0}" -f [Guid]::NewGuid().ToString("N"))
$Archive = Join-Path $Sandbox "source.tar"
$PartialArchive = Join-Path $Sandbox "partial-source.tar"
$Destination = Join-Path $Sandbox "bundle"
$State = Join-Path $Sandbox "reserved-state"
$Lock = Join-Path $RepositoryRoot "uv.lock"
$Socket = [Net.Sockets.TcpListener]::new([Net.IPAddress]::Loopback, 0)
$Socket.Start()
$Port = ([Net.IPEndPoint]$Socket.LocalEndpoint).Port
$Socket.Stop()
$HealthJob = Start-Job -ArgumentList $Port -ScriptBlock {
  param([int]$Port)
  $Listener = [Net.HttpListener]::new()
  $Listener.Prefixes.Add("http://127.0.0.1:$Port/")
  $Listener.Start()
  try {
    $Context = $Listener.GetContext()
    $Payload = [Text.Encoding]::UTF8.GetBytes('{"status":"ok"}')
    $Context.Response.StatusCode = 200
    $Context.Response.ContentType = "application/json"
    $Context.Response.ContentLength64 = $Payload.Length
    $Context.Response.OutputStream.Write($Payload, 0, $Payload.Length)
    $Context.Response.Close()
  } finally {
    $Listener.Stop()
  }
}
New-Item -ItemType Directory -Path $Sandbox | Out-Null
try {
  Start-Sleep -Milliseconds 200
  $ControllerCommit = (& git -C $RepositoryRoot log --format=%H --reverse -- `
    scripts/task28/private-shadow-controller.ps1 | Select-Object -First 1).Trim()
  $PartialCommit = (& git -C $RepositoryRoot rev-parse "$ControllerCommit^").Trim()
  & git -C $RepositoryRoot archive --format=tar --prefix=source/ `
    "--add-virtual-file=source/.task28-source-commit:$PartialCommit" `
    "--output=$PartialArchive" $PartialCommit
  if ($LASTEXITCODE -ne 0) { throw "Partial-stage archive creation failed." }
  try {
    & $Composition -Mode Stage -SourceArchive $PartialArchive -RepositoryRoot $RepositoryRoot `
      -SourceCommit $PartialCommit -LockedRequirements $Lock -Destination $Destination `
      -TaskName "task28-composition-harness" -MutableStatePath $State `
      -PythonExecutable $PythonExecutable -HubHealthUrl "http://127.0.0.1:$Port/health"
    if ($LASTEXITCODE -eq 0) { throw "Partial Stage unexpectedly succeeded." }
  } catch {}
  if (Test-Path -LiteralPath $Destination) {
    throw "Failed partial Stage left a final destination."
  }
  & git -C $RepositoryRoot archive --format=tar --prefix=source/ `
    "--add-virtual-file=source/.task28-source-commit:$Commit" "--output=$Archive" $Commit
  if ($LASTEXITCODE -ne 0) { throw "Harness archive creation failed." }
  try {
    & $Composition -Mode Stage -SourceArchive $Archive -RepositoryRoot $RepositoryRoot `
      -SourceCommit $Commit -LockedRequirements $Lock -Destination $Destination `
      -TaskName "task28-composition-harness" -MutableStatePath $Destination `
      -PythonExecutable $PythonExecutable -HubHealthUrl "http://127.0.0.1:$Port/health"
    if ($LASTEXITCODE -eq 0) { throw "Equal immutable and mutable roots unexpectedly succeeded." }
  } catch {}
  if (Test-Path -LiteralPath $Destination) {
    throw "Equal-root rejection created a final destination."
  }
  & $Composition -Mode Stage -SourceArchive $Archive -RepositoryRoot $RepositoryRoot `
    -SourceCommit $Commit -LockedRequirements $Lock -Destination $Destination `
    -TaskName "task28-composition-harness" -MutableStatePath $State `
    -PythonExecutable $PythonExecutable -HubHealthUrl "http://127.0.0.1:$Port/health"
  if ($LASTEXITCODE -ne 0) { throw "First Stage failed." }
  $ManifestPath = @(
    Get-ChildItem -LiteralPath $Destination -File -Filter "run-manifest.*.json"
  )
  if ($ManifestPath.Count -ne 1) { throw "Stage did not create one self-bound run manifest." }
  $ManifestPath = $ManifestPath[0].FullName
  $FirstManifest = [IO.File]::ReadAllBytes($ManifestPath)
  $FirstSourceManifest = [IO.File]::ReadAllBytes((Join-Path $Destination "source-manifest.json"))
  $FirstRuntimeManifest = [IO.File]::ReadAllBytes((Join-Path $Destination "runtime-manifest.json"))
  & $Composition -Mode Verify -RepositoryRoot $RepositoryRoot -Manifest $ManifestPath
  if ($LASTEXITCODE -ne 0 -or (Test-Path -LiteralPath $State)) {
    throw "Verify did not leave the reserved mutable state absent."
  }
  $ExpectedInventory = @("source", "runtime", "source.tar", "source-manifest.json", "runtime-manifest.json", (Split-Path -Leaf $ManifestPath))
  $ActualInventory = @(Get-ChildItem -LiteralPath $Destination -Force | ForEach-Object { $_.Name })
  if ($ExpectedInventory.Count -ne $ActualInventory.Count -or @($ExpectedInventory | Where-Object { $_ -notin $ActualInventory }).Count -ne 0) {
    throw "Stage did not create the exact immutable bundle inventory."
  }
  $TamperedRuntime = Join-Path $Destination "runtime/requirements.lock"
  $OriginalRuntime = [IO.File]::ReadAllBytes($TamperedRuntime)
  [IO.File]::AppendAllText($TamperedRuntime, "tampered")
  $Rejected = $false
  try {
    & $Composition -Mode Verify -RepositoryRoot $RepositoryRoot -Manifest $ManifestPath
    $Rejected = $LASTEXITCODE -ne 0
  } catch { $Rejected = $true }
  if (-not $Rejected) { throw "Runtime bundle modification was not rejected." }
  [IO.File]::WriteAllBytes($TamperedRuntime, $OriginalRuntime)
  $TamperedSource = Join-Path $Destination "source/src/oms_hub/providers/gemini/evidence.py"
  $OriginalSource = [IO.File]::ReadAllBytes($TamperedSource)
  [IO.File]::AppendAllText($TamperedSource, "tampered")
  $Rejected = $false
  try {
    & $Composition -Mode Verify -RepositoryRoot $RepositoryRoot -Manifest $ManifestPath
    $Rejected = $LASTEXITCODE -ne 0
  } catch { $Rejected = $true }
  if (-not $Rejected) { throw "Source bundle modification was not rejected." }
  [IO.File]::WriteAllBytes($TamperedSource, $OriginalSource)
  $Unexpected = Join-Path $Destination "unexpected"
  [IO.File]::WriteAllText($Unexpected, "unexpected")
  $Rejected = $false
  try {
    & $Composition -Mode Verify -RepositoryRoot $RepositoryRoot -Manifest $ManifestPath
    $Rejected = $LASTEXITCODE -ne 0
  } catch { $Rejected = $true }
  if (-not $Rejected) { throw "Unexpected bundle inventory was not rejected." }
  Remove-Item -LiteralPath $Unexpected -Force
  Remove-Item -LiteralPath $Destination -Recurse -Force
  & $Composition -Mode Stage -SourceArchive $Archive -RepositoryRoot $RepositoryRoot `
    -SourceCommit $Commit -LockedRequirements $Lock -Destination $Destination `
    -TaskName "task28-composition-harness" -MutableStatePath $State `
    -PythonExecutable $PythonExecutable -HubHealthUrl "http://127.0.0.1:$Port/health"
  if ($LASTEXITCODE -ne 0) { throw "Second Stage failed." }
  if (-not [System.Linq.Enumerable]::SequenceEqual(
      $FirstManifest, [IO.File]::ReadAllBytes($ManifestPath)
    ) -or -not [System.Linq.Enumerable]::SequenceEqual(
      $FirstSourceManifest, [IO.File]::ReadAllBytes((Join-Path $Destination "source-manifest.json"))
    ) -or -not [System.Linq.Enumerable]::SequenceEqual(
      $FirstRuntimeManifest, [IO.File]::ReadAllBytes((Join-Path $Destination "runtime-manifest.json"))
    )) {
    throw "Stage output was not deterministic."
  }
  Write-Output "TASK28_PRIVATE_SHADOW_COMPOSITION_HARNESS_VERIFIED"
} finally {
  if ($HealthJob) { Remove-Job -Job $HealthJob -Force -ErrorAction SilentlyContinue }
  if (Test-Path -LiteralPath $Sandbox) { Remove-Item -LiteralPath $Sandbox -Recurse -Force }
}
