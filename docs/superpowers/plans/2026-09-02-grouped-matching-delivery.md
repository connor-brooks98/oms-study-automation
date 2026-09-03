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

## 2. Create the complete Windows PowerShell 5.1 wrapper

The wrapper has three modes. `Preflight` is read-only and emits one compact JSON
binding. `Deploy` accepts that exact base64 binding, executes once, and rolls
back on any post-mutation failure. `Postflight` is read-only and accepts that
same binding plus the backup emitted by `Deploy`.

```bash
set -euo pipefail

deploy_tmp_dir=$(mktemp -d)
deployment_wrapper="$deploy_tmp_dir/oms-grouped-matching-deploy.ps1"
source_plan=docs/superpowers/plans/2026-09-02-grouped-matching-delivery.md
test -f "$source_plan"
apply_patch <<PATCH
*** Begin Patch
*** Add File: $deployment_wrapper
$(sed -n '/^\[CmdletBinding()\]/,/^```$/p' "$source_plan" | sed '$d;s/^/+/')
*** End Patch
PATCH
```

```powershell
[CmdletBinding()]
param(
  [Parameter(Mandatory=$true)][ValidateSet("Preflight","Deploy","Postflight")][string]$Mode,
  [Parameter(Mandatory=$true)][string]$ExpectedWrapperSha256,
  [Parameter(Mandatory=$true)][string]$ExpectedMergedCommit,
  [Parameter(Mandatory=$true)][string]$ExpectedMergedTree,
  [string]$BindingJsonBase64 = "",
  [string]$ExpectedBackupPath = ""
)
$ErrorActionPreference="Stop"; $ProgressPreference="SilentlyContinue"
$root="C:\Services\oms-study-automation-v2"; $taskName="OMS Study Hub V2"
$envPath=Join-Path $root ".env"; $installer=Join-Path $root "scripts\install-windows.ps1"
$player=Join-Path $root "src\oms_hub\web\static\public_quiz.js"; $mutationBegan=$false; $rollbackAttempted=$false
function N([string]$s){if($LASTEXITCODE -ne  0){throw "$s failed: $LASTEXITCODE"}}
function H([string]$p){(Get-FileHash -LiteralPath $p -Algorithm SHA256 -ErrorAction Stop).Hash.ToLowerInvariant()}
function SH([string]$s){$h=[Security.Cryptography.SHA256]::Create();try{([BitConverter]::ToString($h.ComputeHash((New-Object Text.UTF8Encoding($false)).GetBytes($s)))).Replace("-","").ToLowerInvariant()}finally{$h.Dispose()}}
function NR([string]$p){$p=[IO.Path]::GetFullPath($p);while($true){$i=Get-Item -LiteralPath $p -Force -ErrorAction Stop;if(($i.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne  0){throw "reparse point: $p"};$q=Split-Path -LiteralPath $p -Parent;if(!$q -or $q -eq $p){return};$p=$q}}
function LF([string]$p){NR $p;if(!(Test-Path -LiteralPath $p -PathType Leaf)){throw "missing leaf: $p"}}
function DR([string]$p){NR $p;if(!(Test-Path -LiteralPath $p -PathType Container)){throw "missing directory: $p"}}
function GV([string]$n){$e=[regex]::Escape($n);$l=Get-Content -LiteralPath $envPath|Where-Object{$_ -match "^\s*(?:export\s+)?${e}\s*="}|Select-Object -Last 1;if($null -eq $l){return $null};$v=([string]$l -replace "^\s*(?:export\s+)?${e}\s*=\s*","").Trim();if($v.StartsWith('"') -or $v.StartsWith("'")){$q=[string]$v[0];$x=$v.IndexOf($q,1);if($x -lt 1){throw "unterminated $n"};$t=$v.Substring($x+1).Trim();if($t -and -not $t.StartsWith("#")){throw "trailing $n"};return $v.Substring(1,$x-1)};return ([regex]::Replace($v,"\s+#.*$","")).Trim()}
function EV([string]$n,[string]$d){$v=[Environment]::GetEnvironmentVariable($n,"Process");if($null -ne $v){return $v};$v=GV $n;if($null -ne $v){return $v};return $d}
function CFG {NR $root;LF $envPath;$data=[IO.Path]::GetFullPath((EV "OMS_HUB_DATA_DIR" "C:\ProgramData\OMSStudyHub"));$url=EV "OMS_HUB_DATABASE_URL" "sqlite:///C:/ProgramData/OMSStudyHub/hub.db";if(!$url.StartsWith("sqlite:///",[StringComparison]::OrdinalIgnoreCase)){throw "non-sqlite URL"};$raw=$url.Substring(10).Replace("/","\\");if(!$raw -or $raw -eq ":memory:"){throw "invalid SQLite path"};if(![IO.Path]::IsPathRooted($raw)){$raw=Join-Path $root $raw};$db=[IO.Path]::GetFullPath($raw);$pt=EV "OMS_HUB_DASHBOARD_PORT" "8787";$port=0;if(![int]::TryParse($pt,[ref]$port) -or $port -lt  1024 -or $port -gt  65535){throw "invalid port"};DR $data;LF $db;return [ordered]@{data_root=$data;database_url=$url;database_path=$db;port=$port;env_sha256=(H $envPath)}}
function G([string[]]$a){$x=@(& git.exe -C $root @a 2>$null);N ("git "+($a -join " "));return @($x)}
function SRC {$h=([string](G @("rev-parse","HEAD"))[0]).Trim().ToLowerInvariant();$t=([string](G @("rev-parse","HEAD^{tree}"))[0]).Trim().ToLowerInvariant();if(@(G @("status","--porcelain=v1","--untracked-files=no")).Count -ne 0){throw "tracked checkout dirty"};return [ordered]@{commit=$h;tree=$t}}
function PS { $p=[IO.Path]::GetFullPath((Join-Path [Environment]::SystemDirectory "WindowsPowerShell\v1.0\powershell.exe"));LF $p;return $p }
function TD {SH (Export-ScheduledTask -TaskName $taskName -ErrorAction Stop)}
function F28([object]$c,[object]$b){$t=Get-ScheduledTask -TaskName $taskName;if([string]$t.State -cne "Running" -or [string]$t.Settings.ExecutionTimeLimit -cne "PT0S" -or [string]$t.Principal.UserId -cne [string]$b.task_principal -or [string]$t.Principal.LogonType -cne [string]$b.task_logon_type -or (TD) -cne [string]$b.task_xml_sha256){throw "task binding differs"};$a=@($t.Actions);$ids=@("f28-primary-0","f28-recovery-1","f28-recovery-2","f28-recovery-3");if($a.Count -ne 4){throw "task action count"};$start=Join-Path $root "scripts\start-hub.ps1";$recovery=Join-Path $root "scripts\restart-hub-after-failure.ps1";$p=PS;foreach($i in 0..3){$arg=if($i -eq 0){"-NoProfile -ExecutionPolicy Bypass -File `"$start`" -DataRoot `"$($c.data_root)`" -ActionIndex 0"}else{"-NoProfile -ExecutionPolicy Bypass -File `"$recovery`" -DataRoot `"$($c.data_root)`" -ActionIndex $i -DelaySeconds 60"};if([string]$a[$i].Id -cne $ids[$i] -or -not [string]::Equals(([string]$a[$i].Execute).Trim(),$p,[StringComparison]::OrdinalIgnoreCase) -or -not [string]::Equals(([string]$a[$i].Arguments).Trim(),$arg,[StringComparison]::Ordinal) -or -not [string]::Equals(([string]$a[$i].WorkingDirectory).TrimEnd("\\"),$root.TrimEnd("\\"),[StringComparison]::OrdinalIgnoreCase)){throw "F28 action $i differs"}}}
function OWN([int]$pid){$p=Get-CimInstance Win32_Process -Filter "ProcessId=$pid";$o=Invoke-CimMethod -InputObject $p -MethodName GetOwner;if($o.ReturnValue -ne  0){throw "owner unavailable"};return ([string]$o.Domain+"\\"+[string]$o.User)}
function SNAP {return @(Get-CimInstance Win32_Process|ForEach-Object{[ordered]@{pid=[int]$_.ProcessId;parent_pid=[int]$_.ParentProcessId;name=[string]$_.Name;executable_path=[string]$_.ExecutablePath;creation_date=([Management.ManagementDateTimeConverter]::ToDateTime($_.CreationDate).ToUniversalTime().ToString("o"))}})}
function LIS([int]$port){$x=@(Get-NetTCPConnection -State Listen -LocalAddress 127.0.0.1 -LocalPort $port -ErrorAction Stop);if($x.Count -ne  1){throw "listener count $($x.Count)"};$p=Get-CimInstance Win32_Process -Filter ("ProcessId="+[int]$x[0].OwningProcess);return [ordered]@{pid=[int]$p.ProcessId;creation_date=([Management.ManagementDateTimeConverter]::ToDateTime($p.CreationDate).ToUniversalTime().ToString("o"));executable_path=[string]$p.ExecutablePath;owner=(OWN ([int]$p.ProcessId))}}
function ANC([int]$pid){$all=SNAP;$m=@{};foreach($p in $all){$m[[string]$p.pid]=$p};$seen=@{};while($m.ContainsKey([string]$pid)){if($seen.ContainsKey([string]$pid)){throw "process cycle"};$seen[[string]$pid]=$true;$p=$m[[string]$pid];if([string]$p.name -ieq "oms-hub.exe" -and [string]$p.executable_path -like ($root.TrimEnd("\\")+"\\*")){return};$pid=[int]$p.parent_pid};throw "listener is not same-root task ancestry"}
function HH([object]$c,[object]$b,[string]$commit,[string]$tree){$h=Invoke-RestMethod -Uri ("http://127.0.0.1:"+$c.port+"/health/ready") -TimeoutSec 3;if([string]$h.status -cne "ok" -or $h.database_reachable -ne $true -or [string]$h.deployment_root -cne $root -or [string]$h.build_revision -cne $commit -or [string]$h.build_tree -cne $tree -or [int]$h.schema_version -ne [int]$b.schema_version){throw "health identity/schema/database differs"};$want=@("generation_worker","ingestion_worker","studio_worker");if((@($h.workers.PSObject.Properties.Name|Sort-Object) -join "|") -cne ($want -join "|")){throw "workers differ"};foreach($n in $want){$w=$h.workers.$n;if($w.alive -ne $true -or [int]$w.start_count -ne  1 -or $null -ne $w.active_work_age_seconds){throw "worker $n differs"}}}
function COLL([object]$b){$paths=@(G @("diff","--name-only",$b.old_commit,$b.merged_commit));foreach($p in $paths){if(@(G @("ls-files","--others","--exclude-standard","--",$p)).Count -ne 0 -or @(G @("ls-files","--others","--ignored","--exclude-standard","--",$p)).Count -ne 0){throw "release-path collision: $p"}};return $paths}
function BN([string]$r){DR $r;return @((Get-ChildItem -LiteralPath $r -Directory -Force|ForEach-Object{$_.Name})|Sort-Object)}
function Assert-VerifiedRollbackBackup([string]$path,[object]$c,[object]$b){DR $path;$mp=Join-Path $path "backup-manifest.json";$sp="$mp.sha256";$cp=Join-Path $path "backup-complete.json";$ep=Join-Path $path "effective-config.json";foreach($x in @($mp,$sp,$cp,$ep)){LF $x};$mh=H $mp;if((Get-Content -LiteralPath $sp -Raw).Trim() -cne "$mh  backup-manifest.json"){throw "manifest sidecar"};$co=Get-Content -LiteralPath $cp -Raw|ConvertFrom-Json;$m=Get-Content -LiteralPath $mp -Raw|ConvertFrom-Json;$e=Get-Content -LiteralPath $ep -Raw|ConvertFrom-Json;if([string]$co.status -cne "complete" -or [string]$co.manifest -cne "backup-manifest.json" -or [string]$co.manifest_sha256 -cne $mh -or $co.database_backed_up -ne $true -or [string]$co.database_path -cne $c.database_path){throw "completion record"};if([int]$m.schema_version -ne  1 -or [string]$m.project_root -cne $root -or [string]$m.database_path -cne $c.database_path -or $m.database.backed_up -ne $true -or [string]$m.database.source_path -cne $c.database_path -or [string]$m.database.source_url -cne $c.database_url){throw "manifest binding"};if([string]$e.project_root -cne $root -or [string]$e.data_root -cne $c.data_root -or [string]$e.database_path -cne $c.database_path -or [string]$e.database_url -cne $c.database_url -or [string]$e.build_revision -cne $b.merged_commit -or [string]$e.build_tree -cne $b.merged_tree){throw "effective config"};$prefix=$path.TrimEnd("\\")+"\\";$seen=@{};foreach($f in @($m.files)){$r=([string]$f.path).Replace("/","\\");if(!$r -or [IO.Path]::IsPathRooted($r) -or $r -match "(^|\\\\)\.\.?(\\\\|$)" -or $seen.ContainsKey($r.ToLowerInvariant())){throw "unsafe/nonunique member"};$seen[$r.ToLowerInvariant()]=$true;$z=[IO.Path]::GetFullPath((Join-Path $path $r));if(!$z.StartsWith($prefix,[StringComparison]::OrdinalIgnoreCase)){throw "member escape"};LF $z;if((H $z) -cne [string]$f.sha256 -or (Get-Item -LiteralPath $z).Length -ne [long]$f.size){throw "member hash/size"}};$db=[IO.Path]::GetFullPath((Join-Path $path ([string]$m.database.backup_path).Replace("/","\\")));$tx=[IO.Path]::GetFullPath((Join-Path $path ([string]$e.scheduled_task.xml).Replace("/","\\")));foreach($z in @($db,$tx)){if(!$z.StartsWith($prefix,[StringComparison]::OrdinalIgnoreCase)){throw "named member escape"};LF $z};if($e.scheduled_task.existed -ne $true -or (H $tx) -cne [string]$e.scheduled_task.sha256 -or (H $tx) -cne [string]$b.task_xml_sha256){throw "task backup"};return [ordered]@{path=$path;database=$db;task_xml=$tx;artifacts=(Join-Path $path "artifacts")}}
function HUB {$all=@(Get-CimInstance Win32_Process);$kids=@{};foreach($p in $all){$k=[string]$p.ParentProcessId;if(!$kids.ContainsKey($k)){$kids[$k]=@()};$kids[$k]+=$p};$q=New-Object Collections.Queue;$r=@{};foreach($p in $all){if([string]$p.Name -ieq "oms-hub.exe" -and [string]$p.ExecutablePath -like ($root.TrimEnd("\\")+"\\*")){$q.Enqueue($p)}};while($q.Count){$p=$q.Dequeue();if($r.ContainsKey([string]$p.ProcessId)){continue};$r[[string]$p.ProcessId]=$p;foreach($x in @($kids[[string]$p.ProcessId])){$q.Enqueue($x)}};return @($r.Values)}
function STOP {Stop-ScheduledTask -TaskName $taskName -ErrorAction Stop;$end=(Get-Date).AddSeconds(30);while((Get-Date) -lt $end){$p=@(HUB);if($p.Count -eq  0){return};foreach($x in $p){Stop-Process -Id ([int]$x.ProcessId) -Force -ErrorAction SilentlyContinue};Start-Sleep -Milliseconds 500};throw "same-root runtime remains"}
function INST([object]$c){$p=Start-Process -FilePath (PS) -ArgumentList @("-NoProfile","-NonInteractive","-ExecutionPolicy","Bypass","-File",$installer,"-ProjectRoot",$root,"-DataRoot",$c.data_root) -PassThru -WindowStyle Hidden;$zero=$false;while(!$p.HasExited){$n=@(Get-NetTCPConnection -State Listen -LocalAddress 127.0.0.1 -LocalPort $c.port -ErrorAction SilentlyContinue).Count;if($n -eq  0){$zero=$true};if($n -gt  1){Stop-Process -Id $p.Id -Force -ErrorAction SilentlyContinue;throw "more than one listener during installer"};Start-Sleep -Milliseconds 250;$p.Refresh()};if($p.ExitCode -ne  0){throw "installer exit $($p.ExitCode)"};if(!$zero){throw "listener never left during installer"}}
function FINAL([object]$c,[object]$b,[string]$backup){$s=SRC;if($s.commit -cne $b.merged_commit -or $s.tree -cne $b.merged_tree -or (H $envPath) -cne $b.env_sha256){throw "final source/env"};F28 $c $b;$l=LIS $c.port;if($l.pid -eq [int]$b.old_listener_pid -or $l.creation_date -cne [string]$b.old_listener_creation_date -or $l.owner -cne [string]$b.process_identity){throw "listener replacement"};ANC $l.pid;HH $c $b $b.merged_commit $b.merged_tree;COLL $b|Out-Null;Assert-VerifiedRollbackBackup $backup $c $b|Out-Null;LF $player;if(!(Get-Content -LiteralPath $player -Raw).Contains("selectedChoiceIds")){throw "player marker"}}
function RB([object]$c,[object]$b,[string]$bp,[string]$why){$script:rollbackAttempted=$true;try{$bk=Assert-VerifiedRollbackBackup $bp $c $b;STOP;G @("checkout","--detach",$b.old_commit)|Out-Null;$s=SRC;if($s.commit -cne $b.old_commit -or $s.tree -cne $b.old_tree){throw "old checkout"};$qr=Join-Path $c.data_root "failed-release-quarantine";if(!(Test-Path -LiteralPath $qr)){New-Item -ItemType Directory -Path $qr -Force|Out-Null};DR $qr;$q=Join-Path $qr ((Get-Date).ToUniversalTime().ToString("yyyyMMdd-HHmmss")+"-"+[guid]::NewGuid().ToString("N"));New-Item -ItemType Directory -Path $q|Out-Null;DR $q;foreach($x in @($c.database_path,($c.database_path+"-wal"),($c.database_path+"-shm"),(Join-Path $c.data_root "artifacts"))){if(Test-Path -LiteralPath $x){NR $x;Move-Item -LiteralPath $x -Destination $q -ErrorAction Stop}};Copy-Item -LiteralPath $bk.database -Destination $c.database_path -ErrorAction Stop;if(Test-Path -LiteralPath $bk.artifacts -PathType Container){Copy-Item -LiteralPath $bk.artifacts -Destination (Join-Path $c.data_root "artifacts") -Recurse -ErrorAction Stop};$py=Join-Path $root ".venv\Scripts\python.exe";LF $py;& $py -c "import sqlite3,sys; x=sqlite3.connect(sys.argv[1]).execute('PRAGMA integrity_check').fetchone(); assert x == ('ok',), x" $c.database_path;N "old SQLite integrity";& $installer -ProjectRoot $root -DataRoot $c.data_root;N "old installer";STOP;Register-ScheduledTask -TaskName $taskName -Xml (Get-Content -LiteralPath $bk.task_xml -Raw) -Force|Out-Null;if((TD) -cne [string]$b.task_xml_sha256){throw "restored task XML"};Start-ScheduledTask -TaskName $taskName;$end=(Get-Date).AddSeconds(45);$last="";while((Get-Date) -lt $end){try{F28 $c $b;$l=LIS $c.port;if($l.owner -cne [string]$b.process_identity){throw "old owner"};ANC $l.pid;HH $c $b $b.old_commit $b.old_tree;$last="";break}catch{$last=$_.Exception.Message;Start-Sleep -Seconds 1}};if($last){throw $last};$s=SRC;if($s.commit -cne $b.old_commit -or $s.tree -cne $b.old_tree -or (H $envPath) -cne $b.env_sha256){throw "restored source/env"};throw "release failed and rollback completed: $why"}catch{throw "rollback incomplete: release failure: $why; recovery failure: $($_.Exception.Message)"}}
function BIND {if([string]::IsNullOrWhiteSpace($BindingJsonBase64)){throw "binding required"};try{$b=[Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($BindingJsonBase64))|ConvertFrom-Json}catch{throw "invalid binding JSON"};foreach($k in @("old_commit","old_tree","schema_version","old_listener_pid","old_listener_creation_date","process_identity","task_xml_sha256","task_principal","task_logon_type","deployment_identity","data_root","database_path","port","env_sha256","merged_commit","merged_tree")){if($null -eq $b.$k -or [string]::IsNullOrWhiteSpace([string]$b.$k)){throw "binding missing $k"}};if([string]$b.merged_commit -cne $ExpectedMergedCommit -or [string]$b.merged_tree -cne $ExpectedMergedTree -or [int]$b.port -ne  8765){throw "binding release/port"};return $b}
foreach($x in @($ExpectedWrapperSha256,$ExpectedMergedCommit,$ExpectedMergedTree)){if($x -notmatch "^[0-9a-f]{40,64}$"){throw "invalid expected hash"}};LF $PSCommandPath;if((H $PSCommandPath) -cne $ExpectedWrapperSha256){throw "wrapper self-hash"};$c=CFG
if($Mode -eq "Preflight"){$s=SRC;G @("fetch","origin","main")|Out-Null;$om=([string](G @("rev-parse","origin/main"))[0]).Trim().ToLowerInvariant();if($om -cne $ExpectedMergedCommit){throw "origin/main differs"};G @("merge-base","--is-ancestor","HEAD","origin/main")|Out-Null;$t=Get-ScheduledTask -TaskName $taskName;if([System.Security.Principal.WindowsIdentity]::GetCurrent().Name -cne [string]$t.Principal.UserId){throw "deployment identity differs from task principal"};$l=LIS $c.port;ANC $l.pid;$b=[ordered]@{marker="OMS_GROUPED_MATCHING_PREFLIGHT_COMPLETE";old_commit=$s.commit;old_tree=$s.tree;schema_version=[int](Invoke-RestMethod -Uri ("http://127.0.0.1:"+$c.port+"/health/ready") -TimeoutSec 3).schema_version;old_listener_pid=$l.pid;old_listener_creation_date=$l.creation_date;process_identity=$l.owner;task_xml_sha256=(TD);task_principal=[string]$t.Principal.UserId;task_logon_type=[string]$t.Principal.LogonType;deployment_identity=[System.Security.Principal.WindowsIdentity]::GetCurrent().Name;data_root=$c.data_root;database_url=$c.database_url;database_path=$c.database_path;port=$c.port;env_sha256=$c.env_sha256;merged_commit=$ExpectedMergedCommit;merged_tree=$ExpectedMergedTree};if($b.port -ne 8765){throw "configured port"};F28 $c $b;HH $c $b $b.old_commit $b.old_tree;$b.release_paths=COLL $b;$b|ConvertTo-Json -Compress -Depth 8;exit 0}
$b=BIND;if([string]$b.deployment_identity -cne [System.Security.Principal.WindowsIdentity]::GetCurrent().Name-or $c.data_root -cne [string]$b.data_root -or $c.database_path -cne [string]$b.database_path -or $c.port -ne [int]$b.port -or $c.env_sha256 -cne [string]$b.env_sha256){throw "binding changed"}
if($Mode -eq "Deploy"){$br=Join-Path $c.data_root "backups";$before=BN $br;$bp="";try{$s=SRC;if($s.commit -cne $b.old_commit -or $s.tree -cne $b.old_tree){throw "old source"};F28 $c $b;$l=LIS $c.port;if($l.pid -ne [int]$b.old_listener_pid -or $l.creation_date -cne [string]$b.old_listener_creation_date -or $l.owner -cne [string]$b.process_identity){throw "old listener"};ANC $l.pid;HH $c $b $b.old_commit $b.old_tree;COLL $b|Out-Null;& $installer -WhatIf -ProjectRoot $root -DataRoot $c.data_root;N "installer WhatIf";$script:mutationBegan=$true;G @("merge","--ff-only","origin/main")|Out-Null;$s=SRC;if($s.commit -cne $b.merged_commit -or $s.tree -cne $b.merged_tree){throw "merged source"};INST $c;$new=@((BN $br)|Where-Object{$before -notcontains $_});if($new.Count -ne  1){throw "new backup count $($new.Count)"};$bp=Join-Path $br $new[0];Assert-VerifiedRollbackBackup $bp $c $b|Out-Null;$end=(Get-Date).AddSeconds(45);$last="";while((Get-Date) -lt $end){try{FINAL $c $b $bp;$last="";break}catch{$last=$_.Exception.Message;Start-Sleep -Seconds 1}};if($last){throw $last};[ordered]@{marker="OMS_GROUPED_MATCHING_DEPLOY_COMPLETE";commit=$b.merged_commit;tree=$b.merged_tree;new_backup=$bp;env_sha256=$b.env_sha256}|ConvertTo-Json -Compress;exit 0}catch{$why=$_.Exception.Message;if($script:mutationBegan -and -not $script:rollbackAttempted){if(!$bp){$new=@((BN $br)|Where-Object{$before -notcontains $_});if($new.Count -eq  1){$bp=Join-Path $br $new[0]}};RB $c $b $bp $why};throw $why}}
if(!$ExpectedBackupPath){throw "Postflight backup required"};FINAL $c $b $ExpectedBackupPath;[ordered]@{marker="OMS_GROUPED_MATCHING_POSTFLIGHT_COMPLETE";commit=$b.merged_commit;tree=$b.merged_tree;backup=$ExpectedBackupPath;env_sha256=$b.env_sha256}|ConvertTo-Json -Compress
```

## 3. Transfer, PowerShell 5.1 parse gate, one deploy, postflight

The commands below use `-EncodedCommand`, so values do not pass through the NUC
shell as executable syntax. `remote_ps` is only an SSH transport; every command
failure terminates its calling block.

```bash
set -euo pipefail

test "$deployment_wrapper" = "$deploy_tmp_dir/oms-grouped-matching-deploy.ps1"
test -f "$deployment_wrapper"
release_nonce=$(uuidgen | tr '[:upper:]' '[:lower:]')
remote_wrapper="C:\\Users\\conbr\\AppData\\Local\\Temp\\oms-grouped-matching-deploy-$release_nonce.ps1"
wrapper_sha256=$(shasum -a 256 "$deployment_wrapper" | awk '{print $1}')
scp -q "$deployment_wrapper" "nuc:$remote_wrapper"
remote_ps(){ local e; e=$(LC_ALL=C .venv/bin/python -c 'import base64,sys;print(base64.b64encode(sys.stdin.read().encode("utf-16le")).decode())'); ssh nuc "powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -EncodedCommand $e"; }
b64(){ LC_ALL=C .venv/bin/python -c 'import base64,sys;print(base64.b64encode(sys.stdin.buffer.read()).decode())'; }
wrapper_b64=$(printf '%s' "$remote_wrapper" | b64)
printf '%s' "\$p=[Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('$wrapper_b64'));\$i=Get-Item -LiteralPath \$p -Force -ErrorAction Stop;if(\$i.PSIsContainer -or ((\$i.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne  0)){throw 'wrapper leaf'};\$q=Split-Path -LiteralPath \$p -Parent;while(\$true){\$n=Get-Item -LiteralPath \$q -Force -ErrorAction Stop;if((\$n.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne  0){throw 'wrapper parent'};\$z=Split-Path -LiteralPath \$q -Parent;if(!\$z-or\$z-eq\$q){break};\$q=\$z};if((Get-FileHash -LiteralPath \$p -Algorithm SHA256).Hash.ToLowerInvariant() -cne '$wrapper_sha256'){throw 'wrapper hash'};\$t=\$null;\$e=\$null;[Management.Automation.Language.Parser]::ParseFile(\$p,[ref]\$t,[ref]\$e)|Out-Null;if(\$e.Count -ne  0){throw ((\$e|ForEach-Object{\$_.Message}) -join  ';')}" | remote_ps
read -r release_commit release_tree merge_commit merged_tree < /tmp/oms-grouped-matching-release-binding.txt
preflight_ps="\$p=[Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('$wrapper_b64')); & \$p -Mode Preflight -ExpectedWrapperSha256 '$wrapper_sha256' -ExpectedMergedCommit '$merge_commit' -ExpectedMergedTree '$merged_tree'"
preflight_json=$(printf '%s' "$preflight_ps" | remote_ps)
printf '%s' "$preflight_json" | .venv/bin/python -c 'import json,sys; x=json.load(sys.stdin); assert x["marker"]=="OMS_GROUPED_MATCHING_PREFLIGHT_COMPLETE"; assert x["port"]==8765; assert x["merged_commit"]==sys.argv[1] and x["merged_tree"]==sys.argv[2]; print(json.dumps(x,separators=(",",":")))' "$merge_commit" "$merged_tree" > "$deploy_tmp_dir/preflight.json"
binding_b64=$(b64 < "$deploy_tmp_dir/preflight.json")
```

`Deploy` appears exactly once below. It sets `mutationBegan` before `git merge
--ff-only`; any checkout-changing failure therefore enters the verified-backup
rollback path.

```bash
set -euo pipefail

deploy_ps="\$p=[Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('$wrapper_b64')); & \$p -Mode Deploy -ExpectedWrapperSha256 '$wrapper_sha256' -ExpectedMergedCommit '$merge_commit' -ExpectedMergedTree '$merged_tree' -BindingJsonBase64 '$binding_b64'"
deploy_json=$(printf '%s' "$deploy_ps" | remote_ps)
backup_path=$(printf '%s' "$deploy_json" | .venv/bin/python -c 'import json,sys;x=json.load(sys.stdin);assert x["marker"]=="OMS_GROUPED_MATCHING_DEPLOY_COMPLETE";assert x["commit"]==sys.argv[1] and x["tree"]==sys.argv[2];print(x["new_backup"])' "$merge_commit" "$merged_tree")
test -n "$backup_path"
backup_b64=$(printf '%s' "$backup_path" | b64)
postflight_ps="\$p=[Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('$wrapper_b64')); \$bk=[Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('$backup_b64')); & \$p -Mode Postflight -ExpectedWrapperSha256 '$wrapper_sha256' -ExpectedMergedCommit '$merge_commit' -ExpectedMergedTree '$merged_tree' -BindingJsonBase64 '$binding_b64' -ExpectedBackupPath \$bk"
postflight_json=$(printf '%s' "$postflight_ps" | remote_ps)
printf '%s' "$postflight_json" | .venv/bin/python -c 'import json,sys;x=json.load(sys.stdin);assert x["marker"]=="OMS_GROUPED_MATCHING_POSTFLIGHT_COMPLETE";assert x["commit"]==sys.argv[1] and x["tree"]==sys.argv[2]' "$merge_commit" "$merged_tree"
```

## 4. Exact cleanup and absence proof

```bash
set -euo pipefail

cleanup_ps="\$p=[Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('$wrapper_b64')); \$i=Get-Item -LiteralPath \$p -Force -ErrorAction Stop;if(\$i.PSIsContainer -or ((\$i.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne  0)){throw 'cleanup leaf'};Remove-Item -LiteralPath \$p -Force -ErrorAction Stop"
printf '%s' "$cleanup_ps" | remote_ps
absence_ps="\$p=[Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('$wrapper_b64'));if(Test-Path -LiteralPath \$p){throw 'remote wrapper remains'}"
printf '%s' "$absence_ps" | remote_ps
test "$deployment_wrapper" = "$deploy_tmp_dir/oms-grouped-matching-deploy.ps1"
test -f "$deployment_wrapper"
rm -- "$deployment_wrapper"
rmdir -- "$deploy_tmp_dir"
```

The final report distinguishes local verification, the bound CI run, merge
identity, and live NUC health. It never claims a live Gemini extraction or a
real matching-content import.
