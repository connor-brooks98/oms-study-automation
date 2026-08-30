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
. (Join-Path $RepositoryRoot "scripts/task28/private-shadow-common.ps1")
$Commit = (& git -C $RepositoryRoot rev-parse HEAD).Trim()
if ($LASTEXITCODE -ne 0 -or $Commit -notmatch "^[0-9a-f]{40}$") { throw "Harness commit is invalid." }
$Sandbox = Join-Path ([IO.Path]::GetTempPath()) ("oms-task28-composition-{0}" -f [Guid]::NewGuid().ToString("N"))
$Archive = Join-Path $Sandbox "source.tar"
$PartialArchive = Join-Path $Sandbox "partial-source.tar"
$Destination = Join-Path $Sandbox "bundle"
$RunId = "0123456789abcdef0123456789abcdef"
$StateView = Get-Task28StatePaths -RunId $RunId
$State = $StateView.Root
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
    $ContextTask = $Listener.GetContextAsync()
    if (-not $ContextTask.Wait(600000)) { return }
    $Context = $ContextTask.Result
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

function Get-ImmutableSnapshot {
  param([Parameter(Mandatory = $true)][string]$Root)
  $Prefix = $Root.TrimEnd('\', '/') + [IO.Path]::DirectorySeparatorChar
  return @(
    "d|.|$((Get-Item -LiteralPath $Root).LastWriteTimeUtc.Ticks)"
    Get-ChildItem -LiteralPath $Root -Force -Recurse | Sort-Object FullName | ForEach-Object {
      $Relative = $_.FullName.Substring($Prefix.Length).Replace('\', '/')
      if ($_.PSIsContainer) { "d|$Relative|$($_.LastWriteTimeUtc.Ticks)" }
      else { "f|$Relative|$($_.Length)|$((Get-FileHash -LiteralPath $_.FullName -Algorithm SHA256).Hash)|$($_.LastWriteTimeUtc.Ticks)" }
    }
  )
}

function Invoke-Task28BoundScript {
  param(
    [Parameter(Mandatory = $true)][string]$Path,
    [Parameter(Mandatory = $true)][string]$Manifest
  )
  $Process = Start-Process -FilePath (Join-Path $PSHOME "powershell.exe") -ArgumentList @(
    "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
    "-File", ('"{0}"' -f $Path), "-Manifest", ('"{0}"' -f $Manifest)
  ) -NoNewWindow -Wait -PassThru
  return $Process.ExitCode
}

function Assert-StateRejectionCases {
  $Preexisting = Get-Task28StatePaths -RunId ([Guid]::NewGuid().ToString("N"))
  New-Task28ProtectedState -State $Preexisting
  try {
    $Rejected = $false
    try { New-Task28ProtectedState -State $Preexisting } catch { $Rejected = $true }
    if (-not $Rejected) { throw "Preexisting valid state root was accepted." }
  } finally {
    Remove-Item -LiteralPath $Preexisting.Root -Recurse -Force -ErrorAction SilentlyContinue
  }

  $Missing = Get-Task28StatePaths -RunId ([Guid]::NewGuid().ToString("N"))
  New-Task28ProtectedState -State $Missing
  try {
    Remove-Item -LiteralPath $Missing.Scratch -Recurse -Force
    $Rejected = $false
    try { Assert-Task28ProtectedState -State $Missing } catch { $Rejected = $true }
    if (-not $Rejected) { throw "Missing state child was accepted." }
  } finally {
    Remove-Item -LiteralPath $Missing.Root -Recurse -Force -ErrorAction SilentlyContinue
  }

  $Extra = Get-Task28StatePaths -RunId ([Guid]::NewGuid().ToString("N"))
  New-Task28ProtectedState -State $Extra
  try {
    New-Item -ItemType Directory -Path (Join-Path $Extra.Root "extra") | Out-Null
    $Rejected = $false
    try { Assert-Task28ProtectedState -State $Extra } catch { $Rejected = $true }
    if (-not $Rejected) { throw "Extra state child was accepted." }
  } finally {
    Remove-Item -LiteralPath $Extra.Root -Recurse -Force -ErrorAction SilentlyContinue
  }

  $Junction = Get-Task28StatePaths -RunId ([Guid]::NewGuid().ToString("N"))
  $External = Join-Path $Sandbox ("state-external-{0}" -f [Guid]::NewGuid().ToString("N"))
  New-Task28ProtectedState -State $Junction
  New-Item -ItemType Directory -Path $External | Out-Null
  try {
    Remove-Item -LiteralPath $Junction.Diagnostic -Recurse -Force
    New-Item -ItemType Junction -Path $Junction.Diagnostic -Target $External | Out-Null
    $Rejected = $false
    try { Assert-Task28ProtectedState -State $Junction } catch { $Rejected = $true }
    if (-not $Rejected) { throw "State child junction was accepted." }
  } finally {
    Remove-Item -LiteralPath $Junction.Root -Recurse -Force -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath $External -Recurse -Force -ErrorAction SilentlyContinue
  }
}
New-Item -ItemType Directory -Path $Sandbox | Out-Null
try {
  Start-Sleep -Milliseconds 200
  Assert-StateRejectionCases
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
      -TaskName "task28-composition-harness" -RunId $RunId `
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
    $OverlapError = $null
    & $Composition -Mode Stage -SourceArchive $Archive -RepositoryRoot $RepositoryRoot `
      -SourceCommit $Commit -LockedRequirements $Lock -Destination $StateView.Root `
      -TaskName "task28-composition-harness" -RunId $RunId `
      -PythonExecutable $PythonExecutable -HubHealthUrl "http://127.0.0.1:$Port/health"
    if ($LASTEXITCODE -eq 0) { throw "Fixed-state overlap unexpectedly succeeded." }
  } catch { $OverlapError = $_.Exception.Message }
  if ($OverlapError -cne "Immutable and mutable paths must not overlap.") {
    throw "Overlap rejection had the wrong reason."
  }
  if (Test-Path -LiteralPath $StateView.Root) { throw "Overlap rejection created the fixed state root." }
  & $Composition -Mode Stage -SourceArchive $Archive -RepositoryRoot $RepositoryRoot `
    -SourceCommit $Commit -LockedRequirements $Lock -Destination $Destination `
    -TaskName "task28-composition-harness" -RunId $RunId `
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
  $BeforeVerify = Get-ImmutableSnapshot -Root $Destination
  & $Composition -Mode Verify -RepositoryRoot $RepositoryRoot -Manifest $ManifestPath
  if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath (Join-Path $State "evidence"))) {
    throw "Verify did not provision protected mutable state."
  }
  $StateView = Get-Task28StatePaths -RunId $RunId
  if (Test-Path -LiteralPath $StateView.Scratch) { throw "Verify did not remove scratch state." }
  foreach ($Path in @($StateView.Root, $StateView.Evidence, $StateView.Diagnostic)) {
    Assert-Task28ProtectedDirectory -Path $Path -Sid (Get-Task28CurrentSid)
  }
  if (-not [System.Linq.Enumerable]::SequenceEqual([string[]]$BeforeVerify, [string[]](
      Get-ImmutableSnapshot -Root $Destination))) {
    throw "Controller execution changed the immutable bundle."
  }
  $Launcher = Join-Path $Destination "source/scripts/task28/private-shadow-launcher.ps1"
  Remove-Item -LiteralPath (Join-Path $StateView.Evidence "result.json") -Force
  Remove-Item -LiteralPath (Join-Path $StateView.Evidence "status.json") -Force
  New-Item -ItemType Directory -Path $StateView.Scratch | Out-Null
  Protect-Task28Directory -Path $StateView.Scratch -Sid (Get-Task28CurrentSid)
  Assert-Task28ProtectedState -State $StateView
  Remove-Item -LiteralPath "Env:OMS_TASK28_COMPOSITION_VERIFY" -ErrorAction SilentlyContinue
  $LauncherExit = Invoke-Task28BoundScript -Path $Launcher -Manifest $ManifestPath
  if ($LauncherExit -ne 1 -or
      -not (Test-Path -LiteralPath (Join-Path $StateView.Evidence "result.json")) -or
      -not (Test-Path -LiteralPath (Join-Path $StateView.Evidence "status.json")) -or
      (Test-Path -LiteralPath (Join-Path $StateView.Diagnostic "provider-diagnostic.json"))) {
    throw "Named-splat diagnostic binding was not exercised."
  }
  Remove-Item -LiteralPath (Join-Path $State "evidence") -Recurse -Force
  $Controller = Join-Path $Destination "source/scripts/task28/private-shadow-controller.ps1"
  $ControllerExit = Invoke-Task28BoundScript -Path $Controller -Manifest $ManifestPath
  if ($ControllerExit -eq 0) { throw "Dirty state root was repaired or reused." }
  $LauncherExit = Invoke-Task28BoundScript -Path $Launcher -Manifest $ManifestPath
  if ($LauncherExit -eq 0) { throw "Missing state child reached the launcher." }
  if (-not [System.Linq.Enumerable]::SequenceEqual([string[]]$BeforeVerify, [string[]](
      Get-ImmutableSnapshot -Root $Destination))) {
    throw "Dirty-state rejection changed the immutable bundle."
  }
  if (@(Get-ChildItem -LiteralPath $Destination -File -Filter "bundle-manifest.*.json").Count -ne 1) {
    throw "Stage did not create one self-bound immutable file manifest."
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
  Remove-Item -LiteralPath $State -Recurse -Force
  Remove-Item -LiteralPath $Destination -Recurse -Force
  & $Composition -Mode Stage -SourceArchive $Archive -RepositoryRoot $RepositoryRoot `
    -SourceCommit $Commit -LockedRequirements $Lock -Destination $Destination `
    -TaskName "task28-composition-harness" -RunId $RunId `
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
  if (Test-Path -LiteralPath $State) { Remove-Item -LiteralPath $State -Recurse -Force }
}
