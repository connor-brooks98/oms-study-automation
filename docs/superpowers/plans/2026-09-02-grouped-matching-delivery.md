# Grouped Matching Quiz Delivery Plan

**Goal:** merge the reviewed feature and install that exact `main` commit on
the NUC as one fail-closed release. A release failure after the checkout starts
one rollback only and still returns failure.

## Fixed limits

- Branch `codex/grouped-matching-quiz`; no force-push, rebase, or history rewrite.
- NUC root/task/port: `C:\Services\oms-study-automation-v2`, `OMS Study Hub V2`,
  and `127.0.0.1:8765`.
- No provider call, content import/publication, provider-setting change, or Anki write.
- `database_reachable` means the ready endpoint can reach its configured database;
  it does not disclose or prove a database path. This procedure separately parses
  the effective SQLite URL, checks its resolved leaf, and checks the backup
  manifest source path.
- `.venv` and data-root backup/quarantine leaves may change only outside changed
  release paths. Any tracked dirty state, release-path untracked/ignored collision,
  reparse point, or identity mismatch stops the release.

## 1. Push, bind exactly one CI run, and merge

```bash
set -euo pipefail

test "$(git branch --show-current)" = codex/grouped-matching-quiz
git diff --check
test -z "$(git status --porcelain --untracked-files=no)"
release_commit=$(git rev-parse HEAD)
release_tree=$(git rev-parse 'HEAD^{tree}')
git fetch origin main
git merge-base --is-ancestor origin/main "$release_commit"
git push -u origin codex/grouped-matching-quiz
pr_url=$(gh pr create --base main --head codex/grouped-matching-quiz --title "feat(quiz): support grouped matching questions" --body $'Grouped matching interaction and provider-compatible Gemini MIME fix.\n\nLocal Python, JavaScript, Ruff, and mypy checks passed. No live provider or content publication.')
pr_number=$(gh pr view "$pr_url" --json number --jq .number)
test "$(gh pr view "$pr_number" --json headRefOid --jq .headRefOid)" = "$release_commit"

ci_run_id=
for attempt in $(seq 1 30); do
  ci_run_id=$(gh run list --workflow CI --branch codex/grouped-matching-quiz --event pull_request --limit 100 --json databaseId,workflowName,headBranch,headSha,event | .venv/bin/python -c '
import json,sys
rows=[x for x in json.load(sys.stdin) if (x["workflowName"],x["event"],x["headBranch"],x["headSha"])==("CI","pull_request","codex/grouped-matching-quiz",sys.argv[1])]
assert len(rows)<=1,rows
print(rows[0]["databaseId"] if rows else "")
' "$release_commit")
  test -z "$ci_run_id" || break
  sleep 10
done
test -n "$ci_run_id"
for attempt in $(seq 1 120); do
  ci_run=$(gh run view "$ci_run_id" --json workflowName,event,headBranch,headSha,status,conclusion,jobs)
  ci_state=$(printf '%s' "$ci_run" | .venv/bin/python -c 'import json,sys;print(json.load(sys.stdin)["status"])')
  test "$ci_state" = completed && break
  sleep 10
done
test "$ci_state" = completed
printf '%s' "$ci_run" | .venv/bin/python -c '
import json,sys
x=json.load(sys.stdin); want={"Python (lint, types, tests)","JavaScript tests","Windows Python 3.12 document processors","Windows rehearsal source/preflight"}
assert (x["workflowName"],x["event"],x["headBranch"],x["headSha"]) == ("CI","pull_request","codex/grouped-matching-quiz",sys.argv[1]),x
assert x["status"] == "completed" and x["conclusion"] == "success",x
assert {j["name"] for j in x["jobs"]} == want,x["jobs"]
assert all(j["status"] == "completed" and j["conclusion"] == "success" for j in x["jobs"]),x["jobs"]
' "$release_commit"
gh pr merge "$pr_number" --merge --match-head-commit "$release_commit"
git fetch origin main
merge_commit=$(gh pr view "$pr_number" --json state,mergedAt,mergeCommit --jq 'if .state=="MERGED" and .mergedAt != null and .mergeCommit.oid != null then .mergeCommit.oid else error("missing merge OID") end')
test "$(git rev-parse origin/main)" = "$merge_commit"
merged_tree=$(git rev-parse "$merge_commit^{tree}")
git merge-base --is-ancestor "$release_commit" "$merge_commit"
printf '%s %s %s %s\n' "$release_commit" "$release_tree" "$merge_commit" "$merged_tree" > /tmp/oms-grouped-matching-release-binding.txt
```

If `merged_tree` differs from `release_tree`, stop and run Python, JavaScript,
Ruff, mypy, `git diff --check`, and fresh review in a temporary worktree at
`merge_commit`. Otherwise this CI run and tree equality bind review to the merge.

## 2. Use the tracked release artifacts

The release transaction is implemented in two tracked files:

- `scripts/deploy-grouped-matching-release.ps1` is a PowerShell 5.1 script with
  explicit `Preflight`, `Deploy`, and `Postflight` modes. It emits exactly
  one JSON object on successful stdout per mode; diagnostics stay on stderr or
  in its NUC-side release-log leaf.
- `scripts/deploy-grouped-matching-nuc.sh` is the one-use Bash driver. It
  hashes, transfers, parses, invokes, postflights, and removes the exact
  release script with an EXIT trap.

The driver consumes the exact four-field binding written in section 1. It
refuses a different branch identity, a changed merge tree, a reused remote
leaf, a hash mismatch, a reparse point, PowerShell 5.1 parse errors, more than
one JSON response, or a missing expected marker.

## 3. Run the tracked driver exactly once

```bash
set -euo pipefail

test "$(git rev-parse origin/main)" = "$merge_commit"
test "$(git rev-parse "$merge_commit^{tree}")" = "$merged_tree"
test -f /tmp/oms-grouped-matching-release-binding.txt
bash -n scripts/deploy-grouped-matching-nuc.sh
scripts/deploy-grouped-matching-nuc.sh
```

`Deploy` appears exactly once in the driver. The PowerShell script does a
read-only preflight, runs the installer preview, fast-forwards only to the
bound `origin/main` commit, runs the installer once, and then requires a
complete verified rollback backup and replacement runtime before it returns
success. The driver’s EXIT trap removes only its nonce-bound remote leaf and
proves its absence on both success and failure.

If the checkout changes before the installer begins, the PowerShell script
restores the old checkout and certifies that the runtime was untouched. If an
installer has begun without a complete verified backup, it attempts old-runtime
recovery but reports rollback incomplete; it never certifies task, data, or
health state. It never retries the release.

## 4. Capture delivery evidence

Record the bound PR number/CI run, release and merge SHA/tree, the three JSON
markers, the backup path, postflight identity/health evidence, and the driver’s
remote-leaf absence proof. These artifacts prove a deployment transaction only;
they do not authorize provider calls, content publication, or Anki writes.

The final report distinguishes local verification, the bound CI run, merge
identity, and live NUC health. It never claims a live Gemini extraction or a
real matching-content import.
