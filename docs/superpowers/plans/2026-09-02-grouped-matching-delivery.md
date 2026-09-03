# Grouped Matching Quiz Delivery Plan

**Goal:** push the exact reviewed feature, merge it into `main`, and make the
Windows NUC serve that exact merge. This is a single release attempt. A failure
after checkout mutation is rolled back once and is never retried automatically.

## Constraints

- Branch: `codex/grouped-matching-quiz`; never force-push, rebase, or rewrite
  `main`.
- Production root/task/port: `C:\Services\oms-study-automation-v2`,
  `OMS Study Hub V2`, and `127.0.0.1:8765`.
- Preserve tracked, untracked, and ignored files. Bind `.env` only by
  SHA-256, never contents. The only expected ignored changes are `.venv` and
  new data-root backup/quarantine directories.
- Do not call a provider, import/publish quiz content, change provider
  settings, or write Anki.
- SQLite is WAL-mode; rollback quarantines `hub.db`, `hub.db-wal`, and
  `hub.db-shm` before restoring the online SQLite backup.

## 1. Bind, push, and merge

```bash
test "$(git branch --show-current)" = codex/grouped-matching-quiz
git diff --check
test -z "$(git status --porcelain --untracked-files=no)"
release_commit=$(git rev-parse HEAD)
release_tree=$(git rev-parse 'HEAD^{tree}')
git fetch origin main
git merge-base --is-ancestor origin/main "$release_commit"
git push -u origin codex/grouped-matching-quiz
pr_url=$(gh pr create --base main --head codex/grouped-matching-quiz \
  --title "feat(quiz): support grouped matching questions" \
  --body $'Grouped matching interaction and provider-compatible Gemini MIME fix.\n\nLocal Python, JavaScript, Ruff, and mypy checks passed. No live provider or content publication.')
pr_number=$(gh pr view "$pr_url" --json number --jq .number)
test "$(gh pr view "$pr_number" --json headRefOid --jq .headRefOid)" = "$release_commit"
```

There are no branch-protection/ruleset required checks in this repository, so
`gh pr checks --required` is impossible and must not be used. Bind one
bounded exact `pull_request` run of workflow `CI`:

```bash
ci_run_id=
for attempt in $(seq 1 30); do
  ci_run_id=$(gh run list --workflow CI --branch codex/grouped-matching-quiz \
    --event pull_request --limit 100 \
    --json databaseId,workflowName,headBranch,headSha,event \
    | .venv/bin/python -c '
import json, sys
r = [x for x in json.load(sys.stdin) if x["workflowName"] == "CI"
     and x["event"] == "pull_request"
     and x["headBranch"] == "codex/grouped-matching-quiz"
     and x["headSha"] == sys.argv[1]]
assert len(r) == 1, r
print(r[0]["databaseId"])
' "$release_commit") && break
  sleep 10
done
test -n "$ci_run_id"
gh run watch "$ci_run_id" --exit-status
gh run view "$ci_run_id" --json workflowName,event,headBranch,headSha,status,conclusion,jobs \
  | .venv/bin/python -c '
import json,sys
x=json.load(sys.stdin)
want={"Python (lint, types, tests)","JavaScript tests",
      "Windows Python 3.12 document processors",
      "Windows rehearsal source/preflight"}
assert (x["workflowName"],x["event"],x["headBranch"],x["headSha"]) == ("CI","pull_request","codex/grouped-matching-quiz",sys.argv[1]), x
assert x["status"] == "completed" and x["conclusion"] == "success", x
assert {j["name"] for j in x["jobs"]} == want, x["jobs"]
assert all(j["status"] == "completed" and j["conclusion"] == "success" for j in x["jobs"]), x["jobs"]
' "$release_commit"
gh pr merge "$pr_number" --merge --match-head-commit "$release_commit"
git fetch origin main
merge_commit=$(gh pr view "$pr_number" --json state,mergedAt,mergeCommit --jq 'if .state == "MERGED" and .mergedAt != null and .mergeCommit.oid != null then .mergeCommit.oid else error("missing merge OID") end')
test "$(git rev-parse origin/main)" = "$merge_commit"
merged_tree=$(git rev-parse "$merge_commit^{tree}")
git merge-base --is-ancestor "$release_commit" "$merge_commit"
```

If `merged_tree` differs from `release_tree`, rerun the full Python,
JavaScript, Ruff, mypy, and diff-check gates plus fresh review in a temporary
worktree at `merge_commit`. Otherwise record tree equality as the
test/review binding. Any CI/head/job/merge/main mismatch stops before NUC work.

## 2. Read-only NUC preflight

Create a temporary PowerShell 5.1 preflight script with `apply_patch` (never
inside the repository), copy it to a unique temporary leaf, capture its sole
JSON output, then remove it. It must fail closed and bind:

- old HEAD/tree and `health.schema_version`;
- listener PID, creation date, executable path, and CIM `GetOwner` identity;
- exact task actions, task principal, task logon type, and SHA-256 of
  `Export-ScheduledTask` UTF-8-no-BOM XML;
- current Windows deployment identity;
- effective data root, effective SQLite database path, configured port, and
  SHA-256 of `.env`;
- non-reparse root, `.env`, data-root, and database paths;
- health revision/tree/root/database, exactly one loopback listener, and all
  three workers alive with `start_count == 1`;
- clean tracked checkout.

Use the installer’s setting rules exactly: process environment first, then the
last matching `.env` assignment, then defaults
`OMS_HUB_DATA_DIR=C:\ProgramData\OMSStudyHub`,
`OMS_HUB_DATABASE_URL=sqlite:///C:/ProgramData/OMSStudyHub/hub.db`, and
`OMS_HUB_DASHBOARD_PORT=8787`. Reject non-`sqlite:///` database URLs,
missing/unterminated values, and an effective port other than 8765.

The preflight task assertion must require the exact current F28 four-action
contract: system `WindowsPowerShell\v1.0\powershell.exe`, working directory
equal to root, IDs `f28-primary-0`, `f28-recovery-1`,
`f28-recovery-2`, `f28-recovery-3`, primary
`start-hub.ps1 ... -ActionIndex 0`, and recovery
`restart-hub-after-failure.ps1 ... -ActionIndex N -DelaySeconds 60`.
It must also require task state `Running` and `ExecutionTimeLimit=PT0S`.

Before mutation, fetch only on the NUC and require
`origin/main == merge_commit` and
`git merge-base --is-ancestor HEAD origin/main`. Enumerate every release
path with `git diff --name-only old_commit merge_commit`; for each, reject
both:

```powershell
git -C $root ls-files --others --exclude-standard -- $path
git -C $root ls-files --others --ignored --exclude-standard -- $path
```

This collision gate preserves ignored/untracked files Git could overwrite.
There is no release-path exemption for `.venv` or backups; those are allowed
only as expected post-installer changes outside the release-path set.

## 3. Exact release wrapper

Create with `apply_patch`:

```bash
deploy_tmp_dir=$(mktemp -d)
release_nonce=$(uuidgen | tr '[:upper:]' '[:lower:]')
deployment_wrapper="$deploy_tmp_dir/oms-grouped-matching-deploy.ps1"
nuc_temp_root='C:\Users\conbr\AppData\Local\Temp'
remote_wrapper="$nuc_temp_root\oms-grouped-matching-deploy-$release_nonce.ps1"
```

The wrapper takes the complete Task 2 binding:

```powershell
[CmdletBinding()]
param(
 [Parameter(Mandatory=$true)][string]$ExpectedWrapperSha256,
 [Parameter(Mandatory=$true)][string]$ExpectedOldCommit,
 [Parameter(Mandatory=$true)][string]$ExpectedOldTree,
 [Parameter(Mandatory=$true)][int]$ExpectedSchemaVersion,
 [Parameter(Mandatory=$true)][int]$ExpectedOldListenerPid,
 [Parameter(Mandatory=$true)][string]$ExpectedOldListenerCreationDate,
 [Parameter(Mandatory=$true)][string]$ExpectedProcessIdentity,
 [Parameter(Mandatory=$true)][string]$ExpectedOldTaskXmlSha256,
 [Parameter(Mandatory=$true)][string]$ExpectedTaskPrincipal,
 [Parameter(Mandatory=$true)][string]$ExpectedTaskLogonType,
 [Parameter(Mandatory=$true)][string]$ExpectedDeploymentIdentity,
 [Parameter(Mandatory=$true)][string]$ExpectedDataRoot,
 [Parameter(Mandatory=$true)][string]$ExpectedDatabasePath,
 [Parameter(Mandatory=$true)][int]$ExpectedPort,
 [Parameter(Mandatory=$true)][string]$ExpectedEnvSha256,
 [Parameter(Mandatory=$true)][string]$ExpectedMergedCommit,
 [Parameter(Mandatory=$true)][string]$ExpectedMergedTree
)
```

The following functions are required in that wrapper. They are deliberately
small copies of the installer/F28 checks, so no new deployment framework is
introduced.

```powershell
$ErrorActionPreference = "Stop"
$root = "C:\Services\oms-study-automation-v2"
$taskName = "OMS Study Hub V2"
$installer = Join-Path $root "scripts\install-windows.ps1"
$envPath = Join-Path $root ".env"
$backupRoot = Join-Path $ExpectedDataRoot "backups"
$quarantineRoot = Join-Path $ExpectedDataRoot "failed-release-quarantine"
function Assert-Native($name) { if ($LASTEXITCODE -ne 0) { throw "$name failed: $LASTEXITCODE" } }
function Get-Hash($path) { (Get-FileHash -LiteralPath $path -Algorithm SHA256).Hash.ToLowerInvariant() }
function Get-Tree { (git -C $root rev-parse "HEAD^{tree}").Trim() }
function Assert-NonReparse($path) {
  $current = [IO.Path]::GetFullPath($path)
  while ($true) {
    $item = Get-Item -LiteralPath $current -Force -ErrorAction Stop
    if (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) { throw "reparse point: $current" }
    $parent = Split-Path -LiteralPath $current -Parent
    if ($parent -eq $current -or !$parent) { return }; $current = $parent
  }
}
function Assert-Leaf($path) { Assert-NonReparse $path; if (!(Test-Path -LiteralPath $path -PathType Leaf)) { throw "missing leaf: $path" } }
function Get-TaskDigest {
  $bytes = [Text.UTF8Encoding]::new($false).GetBytes((Export-ScheduledTask -TaskName $taskName))
  $sha = [Security.Cryptography.SHA256]::Create()
  try { ([BitConverter]::ToString($sha.ComputeHash($bytes))).Replace("-","").ToLowerInvariant() } finally { $sha.Dispose() }
}
```

At wrapper start, require a non-reparse wrapper leaf and
`Get-Hash $PSCommandPath == ExpectedWrapperSha256`; require the current
Windows identity equals `ExpectedDeploymentIdentity`. Reassert non-reparse
root/env/data/db paths, old HEAD/tree, old task XML digest/principal/logon/F28
actions, unchanged `.env` digest, clean tracked state, exact old
listener PID **and creation date**, process owner, health/schema/workers, and
release-path collision gate. Capture the preexisting backup-directory set.

After `git -C $root merge --ff-only origin/main`, set
`$mutationBegan=$true`, then require exact merged HEAD/tree. Run
`& $installer -WhatIf -ProjectRoot $root -DataRoot $ExpectedDataRoot` and,
once only, `& $installer -ProjectRoot $root -DataRoot $ExpectedDataRoot`.
Never start a second release installer invocation.

### Backup verification before rollback eligibility

Treat a new backup as rollback-capable only when exactly one new backup
directory exists and all conditions below pass. This is the complete
`Assert-VerifiedRollbackBackup` pattern from
`scripts/accept-f28-restart.ps1`, extended with installer bindings:

```powershell
function Assert-VerifiedRollbackBackup($path) {
  Assert-NonReparse $path
  $manifestPath = Join-Path $path "backup-manifest.json"
  $sidecarPath = "$manifestPath.sha256"
  $completePath = Join-Path $path "backup-complete.json"
  $configPath = Join-Path $path "effective-config.json"
  foreach ($p in @($manifestPath,$sidecarPath,$completePath,$configPath)) { Assert-Leaf $p }
  $manifestHash = Get-Hash $manifestPath
  if ((Get-Content -LiteralPath $sidecarPath -Raw).Trim() -cne "$manifestHash  backup-manifest.json") { throw "manifest sidecar mismatch" }
  $complete = Get-Content -LiteralPath $completePath -Raw | ConvertFrom-Json
  $manifest = Get-Content -LiteralPath $manifestPath -Raw | ConvertFrom-Json
  $config = Get-Content -LiteralPath $configPath -Raw | ConvertFrom-Json
  if ($complete.status -cne "complete" -or $complete.manifest_sha256 -cne $manifestHash -or
      !$complete.database_backed_up -or $complete.database_path -cne $ExpectedDatabasePath -or
      $config.project_root -cne $root -or $config.data_root -cne $ExpectedDataRoot -or
      $config.database_path -cne $ExpectedDatabasePath -or
      $config.build_revision -cne $ExpectedMergedCommit -or $config.build_tree -cne $ExpectedMergedTree -or
      !$manifest.database.backed_up -or $manifest.database.source_path -cne $ExpectedDatabasePath) { throw "backup binding mismatch" }
  $prefix = $path.TrimEnd("\") + "\"
  foreach ($member in @($manifest.files)) {
    $relative = ([string]$member.path).Replace("/","\")
    if ([IO.Path]::IsPathRooted($relative) -or $relative -match '(^|\\)\.\.?($|\\)') { throw "unsafe member: $relative" }
    $resolved = [IO.Path]::GetFullPath((Join-Path $path $relative))
    if (!$resolved.StartsWith($prefix,[StringComparison]::OrdinalIgnoreCase)) { throw "member escapes backup" }
    Assert-Leaf $resolved
    if ((Get-Hash $resolved) -cne $member.sha256 -or (Get-Item -LiteralPath $resolved -Force).Length -ne [long]$member.size) { throw "member mismatch: $relative" }
  }
  $db = [IO.Path]::GetFullPath((Join-Path $path ([string]$manifest.database.backup_path).Replace("/","\")))
  $task = [IO.Path]::GetFullPath((Join-Path $path ([string]$config.scheduled_task.xml).Replace("/","\")))
  if (!$db.StartsWith($prefix,[StringComparison]::OrdinalIgnoreCase) -or !$task.StartsWith($prefix,[StringComparison]::OrdinalIgnoreCase)) { throw "backup binding escapes root" }
  Assert-Leaf $db; Assert-Leaf $task
  if (!$config.scheduled_task.existed -or (Get-Hash $task) -cne $config.scheduled_task.sha256 -or (Get-Hash $task) -cne $ExpectedOldTaskXmlSha256) { throw "task backup mismatch" }
  [pscustomobject]@{Path=$path; Database=$db; Task=$task}
}
```

On successful release, require changed listener identity, one listener,
unchanged schema, original process owner, exact F28 task
actions/principal/logon, all workers healthy once, health revision/tree/root,
unchanged `.env` hash, no release-path collision, and the public player
asset containing `selectedChoiceIds`.

### Rollback

Any failure after `$mutationBegan` runs this sequence once:

1. Verify the complete backup above. If that fails, report
   `rollback incomplete`; do not certify data/task recovery.
2. Stop only the exact named task/same-root Hub runtime.
3. Switch detached to `ExpectedOldCommit`, and require old HEAD/tree.
4. Quarantine existing `hub.db`, `hub.db-wal`, `hub.db-shm`, and
   `artifacts` under a unique non-reparse `failed-release-quarantine`
   leaf. Restore backup database/artifacts, then run
   `PRAGMA integrity_check` through the old root's
   `.venv\Scripts\python.exe`.
5. Run the **old** `install-windows.ps1` first. Then stop its runtime,
   register the original exported task XML from the verified backup, re-export
   it and require its digest equals `ExpectedOldTaskXmlSha256`, start the
   restored task, and verify old health/schema/listener/workers/process owner.

Only if every step passes may the wrapper report rollback complete; it still
exits nonzero because the release failed. Any exception says
`rollback incomplete` and includes the release failure and failed recovery
assertion. It never retries the release.

## 4. Transfer, parser gate, execute, independent postflight, cleanup

```bash
wrapper_sha256=$(shasum -a 256 "$deployment_wrapper" | awk '{print $1}')
scp -q "$deployment_wrapper" "nuc:$remote_wrapper"
```

Before execution, use a read-only remote PowerShell command to require the
exact remote leaf and parent are non-reparse, the remote SHA-256 equals
`wrapper_sha256`, and Windows PowerShell 5.1
`[Management.Automation.Language.Parser]::ParseFile` returns zero errors.
Then invoke the wrapper **once**, passing `ExpectedWrapperSha256` and every
Task 2 binding. Do not execute it again after any outcome.

On success, a separate read-only verifier repeats all final root/data/db/env
non-reparse, tracked-clean, `.env` digest, task action/principal/logon,
listener/process identity, schema, workers, health revision/tree/root/database,
and player-marker checks. It also requires the exact remote wrapper leaf to be
absent after cleanup.

```bash
test -n "$deploy_tmp_dir" && test -n "$deployment_wrapper"
test "$deployment_wrapper" = "$deploy_tmp_dir/oms-grouped-matching-deploy.ps1"
rm -- "$deployment_wrapper"
rmdir -- "$deploy_tmp_dir"
```

Remote cleanup is limited to the exact unique `remote_wrapper` after
re-checking it is a non-reparse leaf. Final reporting distinguishes local
tests, exact CI run, merge identity, and live NUC health; it must not claim a
live Gemini extraction or a real matching-content import.
