# Grouped Matching Quiz Delivery Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Push the exact reviewed grouped-matching implementation, merge it into `main`, and deploy that merged tree to the Windows NUC with rollback and health verification.

**Architecture:** Treat the fully tested feature commit/tree as the release candidate, require GitHub CI before merging, and reverify if the merge changes the tree. Deploy only the exact merged `origin/main` identity through the existing guarded Windows installer at `C:\Services\oms-study-automation-v2`, preserving production data and unrelated files.

**Tech Stack:** Git, GitHub CLI, SSH/SCP, Windows PowerShell 5.1, the repository's `scripts/install-windows.ps1`, and the Study Hub `/health/ready` endpoint.

**Spec:** `docs/superpowers/specs/2026-09-02-grouped-matching-and-gemini-structured-output-design.md`

## Global Constraints

- Connor explicitly authorized push, merge, and NUC deployment on 2026-09-02 after implementation and tests pass.
- Release only an exact commit/tree that passed the grouped-matching implementation plan and fresh read-only review.
- Use branch `codex/grouped-matching-quiz`; never force-push or rewrite `main`.
- Require every required GitHub check to report `pass` for the exact PR head before merge. Merge only with `--match-head-commit` bound to `release_commit`; after merge, bind deployment to the PR's merge-commit OID and stop if `origin/main` is no longer exactly that commit. If the merged tree differs from the tested feature tree, rerun all repository gates against the merged tree before deployment.
- Production root is exactly `C:\Services\oms-study-automation-v2`; scheduled task is exactly `OMS Study Hub V2`; Hub port is exactly `127.0.0.1:8765`.
- Preserve `.env`, the database, artifacts, credentials, and every unrelated tracked/untracked production file. The installer must create its verified rollback backup before installation.
- Stop/restart only the exact current production task and same-root process tree through `scripts/install-windows.ps1`; never manipulate another Hub task or generic Python process.
- Do not call Gemini or another live provider, retry the rejected import, publish quiz content, change provider settings, or write Anki as part of delivery.
- The deployment wrapper is one transaction. Once it fast-forwards the checkout, every failure through final verification restores the bound old checkout/tree and attempts old-runtime health recovery. It certifies database/artifact/exported-task restoration only after applying a verified complete installer backup; absent that proof it throws `rollback incomplete` rather than claiming full old-state recovery. A rollback installer run is recovery, never a second release attempt.
- On any failed identity, CI, preflight, installer, listener, task, worker, or health assertion: stop, preserve diagnostics, perform the bound rollback when mutation began, and do not claim deployment success. Do not auto-retry a release execution.

---

## File structure

- No tracked source file changes are produced by this plan.
- One temporary PowerShell wrapper is created outside the repository, copied to the NUC user's temporary directory, and removed locally/remotely after verification.
- `scripts/install-windows.ps1` remains the authoritative deployment and rollback implementation.

---

### Task 1: Push the exact reviewed feature branch and merge through GitHub

**Files:**
- Inspect only: the committed feature tree and `.github/workflows/ci.yml`

**Interfaces:**
- Consumes: final reviewed `codex/grouped-matching-quiz` commit/tree and passing local verification evidence.
- Produces: the exact PR merge-commit OID, whose checked tree is still exactly `origin/main`, with every required GitHub check passed for `release_commit`.

- [ ] **Step 1: Bind the release candidate identity**

Run from the feature checkout:

```bash
test "$(git branch --show-current)" = "codex/grouped-matching-quiz"
git diff --check
test -z "$(git status --porcelain --untracked-files=no)"
release_commit=$(git rev-parse HEAD)
release_tree=$(git rev-parse 'HEAD^{tree}')
git show --stat --oneline --decorate --no-renames "$release_commit"
git show --name-only --format= "$release_commit"
```

Expected: tracked state is clean and `release_commit`/`release_tree` exactly match the commit reviewed after the final full test run. Unrelated untracked workspace files remain untouched.

- [ ] **Step 2: Fetch without rewriting history and verify ancestry**

```bash
git fetch origin main
git merge-base --is-ancestor origin/main "$release_commit"
```

Expected: both commands succeed. If `origin/main` is not an ancestor, merge `origin/main` into the feature branch normally, rerun the complete implementation verification and fresh review, then bind a new release candidate; do not rebase a shared/pushed branch.

- [ ] **Step 3: Push the feature branch and open the PR**

```bash
git push -u origin codex/grouped-matching-quiz
pr_url=$(gh pr create \
  --base main \
  --head codex/grouped-matching-quiz \
  --title "feat(quiz): support grouped matching questions" \
  --body $'## What\n- preserve matching sets as one grouped review/player interaction\n- keep legacy multiple-choice payloads compatible\n- fix Gemini structured-output MIME enum for arbitrary compatible models\n\n## Testing\n- full Python suite\n- full JavaScript suite\n- Ruff\n- mypy\n- focused mixed-import and provider transport coverage\n\nNo live provider request or real content publication was performed.'
printf '%s\n' "$pr_url"
```

Expected: one PR targets `main` from the exact feature branch. Do not expose credentials or source-document content in the PR body.

- [ ] **Step 4: Require checks for the exact head, merge, and bind the merge commit**

```bash
pr_number=$(gh pr view "$pr_url" --json number --jq .number)
test "$(gh pr view "$pr_number" --json headRefOid --jq .headRefOid)" = "$release_commit"
gh pr checks "$pr_number" --required --watch
checks_json=$(gh pr checks "$pr_number" --required --json name,bucket)
printf '%s' "$checks_json" | .venv/bin/python -c '
import json, sys
checks = json.load(sys.stdin)
assert checks, "PR has no required checks"
assert all(check["bucket"] == "pass" for check in checks), checks
'
gh pr merge "$pr_number" --merge --match-head-commit "$release_commit"
git fetch origin main
merge_commit=$(gh pr view "$pr_number" --json state,mergedAt,mergeCommit --jq '
  if .state == "MERGED" and .mergedAt != null and .mergeCommit.oid != null
  then .mergeCommit.oid else error("PR did not report a merge commit") end')
test "$(git rev-parse "$merge_commit")" = "$merge_commit"
merged_commit=$(git rev-parse origin/main)
test "$merged_commit" = "$merge_commit"
merged_tree=$(git rev-parse "${merge_commit}^{tree}")
git merge-base --is-ancestor "$release_commit" "$merge_commit"
git show --stat --oneline --decorate --no-renames "$merged_commit"
```

Expected: `gh pr checks --required` reports only `pass` buckets for the PR head exactly equal to `release_commit`; `gh pr merge` accepts that same head with `--match-head-commit`; and `origin/main` still equals the PR's `mergeCommit.oid`. If a check is pending, skipped, cancelled, failing, or missing, the head changed, merge fails, the PR does not report a merge commit, or `origin/main` advanced, stop without deployment.

- [ ] **Step 5: Reverify if integration changed the tree**

If `merged_tree` differs from `release_tree`, create a temporary worktree from the merged commit using the `superpowers:using-git-worktrees` workflow and rerun:

```bash
.venv/bin/pytest
node --test tests/js/*.test.js
.venv/bin/ruff check src tests scripts
.venv/bin/mypy src
git diff --check
```

Obtain another fresh review of `merged_commit`/`merged_tree`. If the trees are identical, record that equality as the evidence binding the existing test/review verdict to the merge commit.

---

### Task 2: Run a read-only NUC preflight against the current production owner

**Files:**
- Inspect only: NUC checkout, scheduled task, listener, and `/health/ready`

**Interfaces:**
- Consumes: exact `merged_commit` and `merged_tree` from Task 1.
- Produces: a JSON-bound old commit/tree, schema, data root, listener PID, and deployment root whose exact `OMS Study Hub V2` task action and same-root process ancestry own the listener; also proves the checkout can fast-forward to `merged_commit` before any NUC mutation.

- [ ] **Step 1: Bind the exact current task, listener process tree, and old identity read-only**

Run a read-only PowerShell command over the existing `nuc` SSH alias that:

```powershell
$ErrorActionPreference = "Stop"
$root = "C:\Services\oms-study-automation-v2"
$taskName = "OMS Study Hub V2"
function Test-UnderRoot([string]$Path) {
  return -not [string]::IsNullOrWhiteSpace($Path) -and $Path.StartsWith(
    $root.TrimEnd("\") + "\", [StringComparison]::OrdinalIgnoreCase
  )
}
function Get-SystemPowerShell {
  return [System.IO.Path]::GetFullPath((Join-Path [System.Environment]::SystemDirectory "WindowsPowerShell\v1.0\powershell.exe"))
}
function Get-PrimaryArguments([string]$StartScript, [string]$DataRoot) {
  return "-NoProfile -ExecutionPolicy Bypass -File `"$StartScript`" -DataRoot `"$DataRoot`" -ActionIndex 0"
}
function Get-RecoveryArguments([string]$RecoveryScript, [string]$DataRoot, [int]$ActionIndex) {
  return "-NoProfile -ExecutionPolicy Bypass -File `"$RecoveryScript`" -DataRoot `"$DataRoot`" -ActionIndex $ActionIndex -DelaySeconds 60"
}
function Get-ExactTaskContract {
  $task = Get-ScheduledTask -TaskName $taskName -ErrorAction Stop
  $actions = @($task.Actions)
  $startScript = Join-Path $root "scripts\start-hub.ps1"
  $recoveryScript = Join-Path $root "scripts\restart-hub-after-failure.ps1"
  $systemPowerShell = Get-SystemPowerShell
  if ($task.State -ne "Running" -or [string]$task.Settings.ExecutionTimeLimit -cne "PT0S" -or $actions.Count -ne 4) { throw "Exact production task contract is not running" }
  $dataMatch = [regex]::Match([string]$actions[0].Arguments, '-DataRoot\s+"([^"]+)"')
  if (-not $dataMatch.Success) { throw "Task primary action has no quoted data root" }
  $dataRoot = $dataMatch.Groups[1].Value
  $expectedIds = @("f28-primary-0", "f28-recovery-1", "f28-recovery-2", "f28-recovery-3")
  foreach ($index in 0..3) {
    $expectedArguments = if ($index -eq 0) { Get-PrimaryArguments $startScript $dataRoot } else { Get-RecoveryArguments $recoveryScript $dataRoot $index }
    $action = $actions[$index]
    if ([string]$action.Id -cne $expectedIds[$index] -or
        -not [string]::Equals(([string]$action.Execute).Trim(), $systemPowerShell, [StringComparison]::OrdinalIgnoreCase) -or
        -not [string]::Equals(([string]$action.Arguments).Trim(), $expectedArguments, [StringComparison]::Ordinal) -or
        -not [string]::Equals(([string]$action.WorkingDirectory).TrimEnd("\"), $root.TrimEnd("\"), [StringComparison]::OrdinalIgnoreCase)) {
      throw "Task action $index differs from the exact F28 contract"
    }
  }
  return [ordered]@{ data_root = $dataRoot; start_script = $startScript; system_powershell = $systemPowerShell; primary_arguments = Get-PrimaryArguments $startScript $dataRoot }
}
function Assert-TaskOwnsListener([int]$ListenerPid, [object]$Contract) {
  $byPid = @{}
  foreach ($process in @(Get-CimInstance Win32_Process -ErrorAction Stop)) {
    $byPid[[int]$process.ProcessId] = $process
  }
  $chain = @()
  $cursorProcessId = $ListenerPid
  while ($byPid.ContainsKey($cursorProcessId)) {
    $process = $byPid[$cursorProcessId]
    $chain += $process
    if ($process.ParentProcessId -eq $cursorProcessId) { break }
    $cursorProcessId = [int]$process.ParentProcessId
  }
  if ($chain.Count -eq 0) {
    throw "Listener process disappeared before task ancestry could be proven"
  }
  if (-not (Test-UnderRoot ([string]$chain[0].ExecutablePath))) {
    throw "Listener executable is outside the bound deployment root"
  }
  if (@($chain | Where-Object {
    [string]::Equals(([string]$_.ExecutablePath).Trim(), $Contract.system_powershell, [StringComparison]::OrdinalIgnoreCase) -and
    ([string]$_.CommandLine).IndexOf($Contract.primary_arguments, [StringComparison]::Ordinal) -ge 0
  }).Count -ne 1) {
    throw "Listener ancestry does not contain the exact task-launched system PowerShell command"
  }
}
$health = Invoke-RestMethod -Uri "http://127.0.0.1:8765/health/ready" -TimeoutSec 5
$listeners = @(Get-NetTCPConnection -LocalAddress 127.0.0.1 -LocalPort 8765 -State Listen)
$head = (git -C $root rev-parse HEAD).Trim()
$tree = (git -C $root rev-parse "HEAD^{tree}").Trim()
$tracked = @(git -C $root status --porcelain --untracked-files=no)
if ($health.status -ne "ok" -or $health.database_reachable -ne $true) { throw "Hub is not healthy" }
if ($listeners.Count -ne 1) { throw "Expected exactly one loopback listener" }
if ($tracked.Count -ne 0) { throw "Production tracked files are dirty" }
if ($health.deployment_root.TrimEnd("\") -ine $root.TrimEnd("\") -or
    $health.build_revision -ne $head -or $health.build_tree -ne $tree) {
  throw "Health identity differs from the bound checkout/root"
}
$taskContract = Get-ExactTaskContract
Assert-TaskOwnsListener -ListenerPid $listeners[0].OwningProcess -Contract $taskContract
$dataRoot = $taskContract.data_root
foreach ($name in @("generation_worker", "ingestion_worker", "studio_worker")) {
  $worker = $health.workers.$name
  if ($worker.alive -ne $true -or $worker.start_count -ne 1) { throw "Worker is not healthy: $name" }
}
[ordered]@{
  head = $head; tree = $tree; schema_version = $health.schema_version
  listener_pid = $listeners[0].OwningProcess; deployment_root = $health.deployment_root
  data_root = $dataRoot
} | ConvertTo-Json -Compress
```

Run the preceding block through `ssh -o BatchMode=yes nuc 'powershell.exe -NoProfile -NonInteractive -Command -'`, capture its sole JSON line in `nuc_preflight`, and bind its fields before continuing:

```bash
nuc_old_commit=$(printf '%s' "$nuc_preflight" | .venv/bin/python -c 'import json,sys; print(json.load(sys.stdin)["head"])')
nuc_old_tree=$(printf '%s' "$nuc_preflight" | .venv/bin/python -c 'import json,sys; print(json.load(sys.stdin)["tree"])')
nuc_old_listener_pid=$(printf '%s' "$nuc_preflight" | .venv/bin/python -c 'import json,sys; print(json.load(sys.stdin)["listener_pid"])')
nuc_schema_version=$(printf '%s' "$nuc_preflight" | .venv/bin/python -c 'import json,sys; print(json.load(sys.stdin)["schema_version"])')
nuc_data_root=$(printf '%s' "$nuc_preflight" | .venv/bin/python -c 'import json,sys; print(json.load(sys.stdin)["data_root"])')
```

Expected: current NUC checkout and health identities match, `deployment_root` is exactly `$root`, exactly one loopback listener belongs to the exact four-action `OMS Study Hub V2` task and its same-root `start-hub.ps1` ancestry, all three workers are alive once, and tracked production files are clean. Any mismatch stops before checkout mutation.

- [ ] **Step 2: Fetch and prove a fast-forward without changing the checkout**

Run from the Mac checkout, where `merged_commit` is still the exact Task 1 value:

```bash
nuc_remote_commit=$(ssh -o BatchMode=yes nuc 'powershell.exe -NoProfile -Command "git -C C:\Services\oms-study-automation-v2 fetch origin main; if ($LASTEXITCODE -ne 0) { throw \"NUC fetch failed\" }; (git -C C:\Services\oms-study-automation-v2 rev-parse origin/main).Trim()"' | tr -d '\r' | tail -1)
test "$nuc_remote_commit" = "$merged_commit"
ssh -o BatchMode=yes nuc 'powershell.exe -NoProfile -Command "git -C C:\Services\oms-study-automation-v2 merge-base --is-ancestor HEAD origin/main; if ($LASTEXITCODE -ne 0) { throw \"Production checkout cannot fast-forward\" }"'
```

Expected: `origin/main` on the NUC is the exact merged commit and current production is its ancestor. The bound `nuc_old_commit`, `nuc_old_tree`, and `nuc_old_listener_pid` are inputs to—not values rediscovered by—the mutating wrapper.

---

### Task 3: Deploy the exact merged tree through the guarded installer

**Files:**
- Read: `scripts/install-windows.ps1`
- Create temporarily: a deployment wrapper under a `mktemp -d` directory and `C:\Users\conbr\AppData\Local\Temp\oms-grouped-matching-deploy.ps1`

**Interfaces:**
- Consumes: exact merged commit/tree plus the Task 2-bound old commit, old tree, old listener PID, schema, and data root.
- Produces: either a NUC checkout and `/health/ready` bound to the exact merged identity, or—after any post-fast-forward failure—old checkout/runtime health plus either a certified old database/artifacts/task restoration from a verified complete backup or an explicit `rollback incomplete` failure.

- [ ] **Step 1: Create a hash-bound temporary wrapper**

Create the exact temporary target, then use `apply_patch` to write the PowerShell wrapper there:

```bash
deploy_tmp_dir=$(mktemp -d)
deployment_wrapper="$deploy_tmp_dir/oms-grouped-matching-deploy.ps1"
```

The wrapper must perform these operations in order:

```powershell
[CmdletBinding()]
param(
  [Parameter(Mandatory = $true)][string]$ExpectedOldCommit,
  [Parameter(Mandatory = $true)][string]$ExpectedOldTree,
  [Parameter(Mandatory = $true)][int]$ExpectedOldListenerPid,
  [Parameter(Mandatory = $true)][string]$ExpectedMergedCommit,
  [Parameter(Mandatory = $true)][string]$ExpectedMergedTree,
  [Parameter(Mandatory = $true)][int]$ExpectedSchemaVersion,
  [Parameter(Mandatory = $true)][string]$ExpectedDataRoot
)

$ErrorActionPreference = "Stop"
$root = "C:\Services\oms-study-automation-v2"
$taskName = "OMS Study Hub V2"
$installer = Join-Path $root "scripts\install-windows.ps1"
$backupRoot = Join-Path $ExpectedDataRoot "backups"
$quarantineRoot = Join-Path $ExpectedDataRoot "failed-release-quarantine"
Set-Location -LiteralPath $root

function Assert-Native([string]$Operation) {
  if ($LASTEXITCODE -ne 0) { throw "$Operation failed with exit $LASTEXITCODE" }
}
function Get-Tree { return (git -C $root rev-parse "HEAD^{tree}").Trim() }
function Test-UnderRoot([string]$Path) {
  return -not [string]::IsNullOrWhiteSpace($Path) -and $Path.StartsWith(
    $root.TrimEnd("\") + "\", [StringComparison]::OrdinalIgnoreCase
  )
}
function Get-SystemPowerShell {
  return [System.IO.Path]::GetFullPath((Join-Path [System.Environment]::SystemDirectory "WindowsPowerShell\v1.0\powershell.exe"))
}
function Get-PrimaryArguments([string]$StartScript) {
  return "-NoProfile -ExecutionPolicy Bypass -File `"$StartScript`" -DataRoot `"$ExpectedDataRoot`" -ActionIndex 0"
}
function Get-RecoveryArguments([string]$RecoveryScript, [int]$ActionIndex) {
  return "-NoProfile -ExecutionPolicy Bypass -File `"$RecoveryScript`" -DataRoot `"$ExpectedDataRoot`" -ActionIndex $ActionIndex -DelaySeconds 60"
}
function Get-ExactTaskContract {
  $task = Get-ScheduledTask -TaskName $taskName -ErrorAction Stop
  $actions = @($task.Actions)
  $startScript = Join-Path $root "scripts\start-hub.ps1"
  $recoveryScript = Join-Path $root "scripts\restart-hub-after-failure.ps1"
  $systemPowerShell = Get-SystemPowerShell
  if ($task.State -ne "Running" -or [string]$task.Settings.ExecutionTimeLimit -cne "PT0S" -or $actions.Count -ne 4) { throw "Exact production task contract is not running" }
  $expectedIds = @("f28-primary-0", "f28-recovery-1", "f28-recovery-2", "f28-recovery-3")
  foreach ($index in 0..3) {
    $expectedArguments = if ($index -eq 0) { Get-PrimaryArguments $startScript } else { Get-RecoveryArguments $recoveryScript $index }
    $action = $actions[$index]
    if ([string]$action.Id -cne $expectedIds[$index] -or
        -not [string]::Equals(([string]$action.Execute).Trim(), $systemPowerShell, [StringComparison]::OrdinalIgnoreCase) -or
        -not [string]::Equals(([string]$action.Arguments).Trim(), $expectedArguments, [StringComparison]::Ordinal) -or
        -not [string]::Equals(([string]$action.WorkingDirectory).TrimEnd("\"), $root.TrimEnd("\"), [StringComparison]::OrdinalIgnoreCase)) {
      throw "Task action $index differs from the exact F28 contract"
    }
  }
  return [ordered]@{ system_powershell = $systemPowerShell; primary_arguments = Get-PrimaryArguments $startScript }
}
function Assert-ListenerDescendsFromTask([int]$ListenerPid, [object]$Contract) {
  $byProcessId = @{}
  foreach ($process in @(Get-CimInstance Win32_Process -ErrorAction Stop)) { $byProcessId[[int]$process.ProcessId] = $process }
  $chain = @(); $cursorProcessId = $ListenerPid
  while ($byProcessId.ContainsKey($cursorProcessId)) {
    $process = $byProcessId[$cursorProcessId]; $chain += $process
    if ($process.ParentProcessId -eq $cursorProcessId) { break }
    $cursorProcessId = [int]$process.ParentProcessId
  }
  if ($chain.Count -eq 0 -or -not (Test-UnderRoot ([string]$chain[0].ExecutablePath))) { throw "Listener is absent or outside the bound Hub runtime" }
  if (@($chain | Where-Object {
    [string]::Equals(([string]$_.ExecutablePath).Trim(), $Contract.system_powershell, [StringComparison]::OrdinalIgnoreCase) -and
    ([string]$_.CommandLine).IndexOf($Contract.primary_arguments, [StringComparison]::Ordinal) -ge 0
  }).Count -ne 1) { throw "Listener ancestry lacks the exact task-launched system PowerShell command" }
}
function Assert-TaskAndListener([string]$Commit, [string]$Tree, [int]$NotListenerPid = 0) {
  $health = Invoke-RestMethod -Uri "http://127.0.0.1:8765/health/ready" -TimeoutSec 5
  $listeners = @(Get-NetTCPConnection -LocalAddress 127.0.0.1 -LocalPort 8765 -State Listen)
  if ($health.status -ne "ok" -or $health.database_reachable -ne $true -or
      [int]$health.schema_version -ne $ExpectedSchemaVersion -or
      $health.deployment_root.TrimEnd("\") -ine $root.TrimEnd("\") -or
      $health.build_revision -ne $Commit -or $health.build_tree -ne $Tree) { throw "Health does not match the bound root/build/schema" }
  if ($listeners.Count -ne 1 -or ($NotListenerPid -ne 0 -and $listeners[0].OwningProcess -eq $NotListenerPid)) { throw "Listener count or replacement identity is wrong" }
  $contract = Get-ExactTaskContract
  Assert-ListenerDescendsFromTask -ListenerPid $listeners[0].OwningProcess -Contract $contract
  foreach ($name in @("generation_worker", "ingestion_worker", "studio_worker")) {
    $worker = $health.workers.$name
    if ($worker.alive -ne $true -or $worker.start_count -ne 1) { throw "Worker check failed: $name" }
  }
  return $listeners[0].OwningProcess
}
function Wait-Healthy([string]$Commit, [string]$Tree, [int]$NotListenerPid = 0) {
  $deadline = (Get-Date).AddSeconds(90)
  do { try { return Assert-TaskAndListener $Commit $Tree $NotListenerPid } catch { $last = $_ }; Start-Sleep -Seconds 1 } while ((Get-Date) -lt $deadline)
  throw "Hub did not become healthy for $Commit/$Tree: $($last.Exception.Message)"
}
function Get-NewCompleteBackup([string[]]$Before, [switch]$AllowIncomplete) {
  $after = @(Get-ChildItem -LiteralPath $backupRoot -Directory -ErrorAction Stop | Select-Object -ExpandProperty FullName)
  $new = @($after | Where-Object { $_ -notin $Before })
  if ($new.Count -eq 0) { return $null }
  if ($new.Count -ne 1) { throw "Expected exactly one installer backup, found $($new.Count)" }
  $backup = $new[0]
  $complete = Join-Path $backup "backup-complete.json"
  $manifest = Join-Path $backup "backup-manifest.json"
  if (-not (Test-Path -LiteralPath $complete -PathType Leaf) -or -not (Test-Path -LiteralPath $manifest -PathType Leaf)) {
    if ($AllowIncomplete) { return $null }
    throw "Installer created no complete rollback backup"
  }
  $checksum = (Get-Content -LiteralPath "$manifest.sha256" -Raw).Split()[0]
  if ((Get-FileHash -LiteralPath $manifest -Algorithm SHA256).Hash.ToLowerInvariant() -cne $checksum.ToLowerInvariant()) { throw "Rollback manifest checksum mismatch" }
  return $backup
}
function Get-TaskHubProcesses {
  $processes = @(Get-CimInstance Win32_Process -ErrorAction Stop)
  $children = @{}
  foreach ($process in $processes) {
    $key = [string]$process.ParentProcessId
    if (-not $children.ContainsKey($key)) { $children[$key] = @() }
    $children[$key] += $process
  }
  $selected = @(); $seen = @{}; $pending = @()
  foreach ($process in $processes) {
    if ([string]$process.Name -ieq "oms-hub.exe" -and (Test-UnderRoot ([string]$process.ExecutablePath))) { $pending += $process }
  }
  while ($pending.Count -gt 0) {
    $process = $pending[0]; $pending = @($pending | Select-Object -Skip 1)
    $id = [int]$process.ProcessId
    if ($seen.ContainsKey($id)) { continue }
    $seen[$id] = $true; $selected += $process
    $childKey = [string]$id
    if ($children.ContainsKey($childKey)) { $pending += $children[$childKey] }
  }
  foreach ($process in $processes) {
    if ([string]$process.Name -ieq "python.exe" -and (Test-UnderRoot ([string]$process.ExecutablePath)) -and
        ([string]$process.CommandLine) -match "(?i)oms[-_]hub" -and -not $seen.ContainsKey([int]$process.ProcessId)) {
      $selected += $process
    }
  }
  return @($selected)
}
function Stop-SameRootRuntime {
  Stop-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
  $deadline = (Get-Date).AddSeconds(30); $clearObservations = 0
  do {
    $remaining = @(Get-TaskHubProcesses)
    if ($remaining.Count -eq 0) {
      $clearObservations += 1
      if ($clearObservations -ge 2) { return }
    } else { $clearObservations = 0 }
    Start-Sleep -Milliseconds 500
  } while ((Get-Date) -lt $deadline)
  $details = $remaining | ForEach-Object { "$($_.ProcessId):$($_.ExecutablePath)" }
  throw "Exact OMS Study Hub task runtime did not stop; refusing to alter data while it remains: $($details -join '; ')"
}
function Move-ToQuarantine([string]$Path, [string]$Label) {
  if (-not (Test-Path -LiteralPath $Path)) { return $null }
  New-Item -ItemType Directory -Force -Path $quarantineRoot | Out-Null
  $target = Join-Path $quarantineRoot ("{0}-{1}" -f $Label, [guid]::NewGuid().ToString("N"))
  Move-Item -LiteralPath $Path -Destination $target -ErrorAction Stop
  Write-Host "Quarantined failed-release $Label at $target"
  return $target
}
function Restore-OldState([AllowNull()][string]$Backup) {
  Stop-SameRootRuntime
  if (@(git -C $root status --porcelain --untracked-files=no).Count -ne 0) { throw "Recovery checkout is not clean; refusing to discard tracked changes" }
  git -C $root switch --detach $ExpectedOldCommit
  Assert-Native "Old checkout restoration"
  if ((git -C $root rev-parse HEAD).Trim() -ne $ExpectedOldCommit -or (Get-Tree) -ne $ExpectedOldTree) { throw "Old checkout/tree restoration failed" }
  $quarantinedDatabase = $null; $quarantinedArtifacts = $null; $dataTaskCertified = $false
  if ($Backup) {
    $config = Get-Content -LiteralPath (Join-Path $Backup "effective-config.json") -Raw | ConvertFrom-Json
    $manifest = Get-Content -LiteralPath (Join-Path $Backup "backup-manifest.json") -Raw | ConvertFrom-Json
    if ($config.project_root.TrimEnd("\") -ine $root.TrimEnd("\") -or $config.data_root.TrimEnd("\") -ine $ExpectedDataRoot.TrimEnd("\") -or -not $manifest.database.backed_up) { throw "Rollback backup is not bound to the production root/data" }
    $databaseBackup = Join-Path $Backup $manifest.database.backup_path
    if (-not (Test-Path -LiteralPath $databaseBackup -PathType Leaf)) { throw "Rollback database is missing" }
    $quarantinedDatabase = Move-ToQuarantine $config.database_path "database"
    Copy-Item -LiteralPath $databaseBackup -Destination $config.database_path -Force
    $artifactPath = Join-Path $ExpectedDataRoot "artifacts"
    $quarantinedArtifacts = Move-ToQuarantine $artifactPath "artifacts"
    $artifactBackup = Join-Path $Backup "artifacts"
    if (Test-Path -LiteralPath $artifactBackup) { Copy-Item -LiteralPath $artifactBackup -Destination $artifactPath -Recurse -Force }
    $taskBackup = Join-Path $Backup $config.scheduled_task.xml
    if (-not $config.scheduled_task.existed -or -not (Test-Path -LiteralPath $taskBackup -PathType Leaf) -or
        (Get-FileHash -LiteralPath $taskBackup -Algorithm SHA256).Hash.ToLowerInvariant() -cne $config.scheduled_task.sha256) { throw "Rollback task backup is invalid" }
    Register-ScheduledTask -TaskName $taskName -Xml (Get-Content -LiteralPath $taskBackup -Raw) -Force -ErrorAction Stop | Out-Null
    $dataTaskCertified = $true
  }
  & $installer -ProjectRoot $root -DataRoot $ExpectedDataRoot
  if ($LASTEXITCODE -ne 0) { throw "Old-runtime installer recovery failed" }
  Wait-Healthy $ExpectedOldCommit $ExpectedOldTree | Out-Null
  return [ordered]@{ database = $quarantinedDatabase; artifacts = $quarantinedArtifacts; data_task_certified = $dataTaskCertified }
}

if ((git rev-parse HEAD).Trim() -ne $ExpectedOldCommit -or (Get-Tree) -ne $ExpectedOldTree) { throw "Pre-deploy checkout changed" }
if (@(git status --porcelain --untracked-files=no).Count -ne 0) { throw "Tracked production files are dirty" }
$untrackedBefore = @(git ls-files --others --exclude-standard | Sort-Object)
if ((Wait-Healthy $ExpectedOldCommit $ExpectedOldTree) -ne $ExpectedOldListenerPid) { throw "Preflight listener PID changed" }
$backupsBefore = @(Get-ChildItem -LiteralPath $backupRoot -Directory -ErrorAction Stop | Select-Object -ExpandProperty FullName)
$mutated = $false
$rollbackBackup = $null
try {
  $mutated = $true
  git merge --ff-only origin/main
  Assert-Native "Checkout fast-forward"
  if ((git rev-parse HEAD).Trim() -ne $ExpectedMergedCommit -or (Get-Tree) -ne $ExpectedMergedTree) { throw "Wrong deploy revision/tree" }
  & $installer -WhatIf -ProjectRoot $root -DataRoot $ExpectedDataRoot
  if ($LASTEXITCODE -ne 0) { throw "Installer preflight failed" }
  $process = Start-Process -FilePath "powershell.exe" -ArgumentList @("-NoProfile", "-ExecutionPolicy", "Bypass", "-File", $installer, "-ProjectRoot", $root, "-DataRoot", $ExpectedDataRoot) -WorkingDirectory $root -NoNewWindow -PassThru
  $sawDown = $false; $maxListeners = 1
  while (-not $process.HasExited) {
    $count = @(Get-NetTCPConnection -LocalAddress 127.0.0.1 -LocalPort 8765 -State Listen -ErrorAction SilentlyContinue).Count
    if ($count -eq 0) { $sawDown = $true }; if ($count -gt $maxListeners) { $maxListeners = $count }
    Start-Sleep -Milliseconds 250; $process.Refresh()
  }
  if ($process.ExitCode -ne 0) { throw "Installer failed with exit $($process.ExitCode)" }
  if (-not $sawDown -or $maxListeners -gt 1) { throw "Listener transition assertion failed" }
  $rollbackBackup = Get-NewCompleteBackup $backupsBefore
  if (-not $rollbackBackup) { throw "Installer produced no rollback backup" }
  $newPid = Wait-Healthy $ExpectedMergedCommit $ExpectedMergedTree $ExpectedOldListenerPid
  $untrackedAfter = @(git ls-files --others --exclude-standard | Sort-Object)
  if (@(Compare-Object $untrackedBefore $untrackedAfter).Count -ne 0) { throw "Untracked production files changed" }
  $player = (Invoke-WebRequest -UseBasicParsing -Uri "http://127.0.0.1:8765/public/quizzes/assets/player.js" -TimeoutSec 5).Content
  if (-not $player.Contains("selectedChoiceIds")) { throw "Grouped matching player asset is stale" }
  [ordered]@{ head = $ExpectedMergedCommit; tree = $ExpectedMergedTree; listener_pid = $newPid; rollback_backup = $rollbackBackup } | ConvertTo-Json -Compress
} catch {
  $releaseFailure = $_
  $backupFailure = $null; $backupState = "verified complete installer backup"
  if (-not $rollbackBackup) {
    try { $rollbackBackup = Get-NewCompleteBackup $backupsBefore -AllowIncomplete }
    catch { $backupFailure = $_; [Console]::Error.WriteLine("Rollback backup discovery failed: $($_.Exception.Message)") }
    if ($rollbackBackup) { $backupState = "verified complete installer backup at $rollbackBackup" }
    elseif ($backupFailure) { $backupState = "backup discovery/checksum failure: $($backupFailure.Exception.Message)" }
    else { $backupState = "no verified complete installer backup was available" }
  }
  if ($mutated) {
    try {
      $quarantine = Restore-OldState $rollbackBackup
      if (-not $quarantine.data_task_certified) {
        throw "rollback incomplete: old runtime health recovered, but old database/artifacts/exported task state was not restored or certified ($backupState)"
      }
      [Console]::Error.WriteLine("Release rolled back with verified old data/task state. Failed-release database quarantine: $($quarantine.database); artifacts quarantine: $($quarantine.artifacts)")
    } catch {
      throw "Release failed: $($releaseFailure.Exception.Message); backup state: $backupState; rollback failed or incomplete: $($_.Exception.Message)"
    }
  }
  throw $releaseFailure
}
```

Pass the seven bound old/new values as wrapper parameters; do not embed credentials or `.env` contents. Recovery performs a clean-tree, detached checkout of the old commit without rewriting history. With a verified complete backup, it moves live database/artifact paths to unique `failed-release-quarantine` paths, restores backup copies and the exported task, and certifies full old-state recovery. Without that backup, it still attempts old-runtime health recovery but exits with `rollback incomplete`; it never claims database, artifact, or exported-task restoration.

- [ ] **Step 2: Transfer and execute once**

```bash
scp -q "$deployment_wrapper" 'nuc:C:/Users/conbr/AppData/Local/Temp/oms-grouped-matching-deploy.ps1'
ssh -o BatchMode=yes nuc "powershell.exe -NoProfile -ExecutionPolicy Bypass -File C:\\Users\\conbr\\AppData\\Local\\Temp\\oms-grouped-matching-deploy.ps1 -ExpectedOldCommit $nuc_old_commit -ExpectedOldTree $nuc_old_tree -ExpectedOldListenerPid $nuc_old_listener_pid -ExpectedMergedCommit $merged_commit -ExpectedMergedTree $merged_tree -ExpectedSchemaVersion $nuc_schema_version -ExpectedDataRoot \"$nuc_data_root\""
```

Expected: one release execution only. Success reports the exact merged HEAD/tree, a changed listener PID, one listener, a running task, unchanged schema, database reachability, all workers alive once, the player marker, and the complete rollback-backup path. A release failure after checkout mutation exits nonzero only after old runtime health is attempted; it certifies old task/data restoration only when the verified complete backup was applied, otherwise reports `rollback incomplete`. Do not retry automatically.

- [ ] **Step 3: Remove temporary wrappers and independently verify final state**

Remove only the exact local temporary directory and exact remote wrapper after execution:

```bash
ssh -o BatchMode=yes nuc 'powershell.exe -NoProfile -Command "Remove-Item -LiteralPath C:\Users\conbr\AppData\Local\Temp\oms-grouped-matching-deploy.ps1 -Force"'
rm -- "$deployment_wrapper"
rmdir -- "$deploy_tmp_dir"
```

Before local removal, assert both variables are non-empty and that `deployment_wrapper` is the expected leaf inside the exact directory returned by `mktemp`; never substitute a broad directory. Then run a separate read-only verifier that repeats the HEAD/tree, tracked-clean, `/health/ready`, deployment-root, database, schema, listener-count, task-state, worker, player-asset, and remote-temp-absence assertions.

Expected: the independent verifier passes against the same merged commit/tree. Record the rollback-backup path printed by `install-windows.ps1`. The final report must distinguish local tests, GitHub CI, merge identity, and live NUC health; it must not claim that Gemini extraction or real matching-content import was exercised live.
