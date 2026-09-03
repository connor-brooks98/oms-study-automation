#!/usr/bin/env bash
# One-use driver for the reviewed grouped-matching release. Do not generalize.
set -euo pipefail

script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
repo_root=$(cd -- "$script_dir/.." && pwd)
cd "$repo_root"
release_script="scripts/deploy-grouped-matching-release.ps1"
test "$(git branch --show-current)" = "codex/grouped-matching-quiz"
test -z "$(git status --porcelain)"
release_commit=$(git rev-parse HEAD)
release_tree=$(git rev-parse 'HEAD^{tree}')
git fetch origin main
merge_commit=$(git rev-parse origin/main)
merged_tree=$(git rev-parse "$merge_commit^{tree}")
git merge-base --is-ancestor "$release_commit" "$merge_commit"
test "$release_tree" = "$merged_tree"
expected_blob_sha256=$(git show "$merge_commit:$release_script" | shasum -a 256 | awk '{print $1}')
script_sha256=$(shasum -a 256 "$release_script" | awk '{print $1}')
test "$script_sha256" = "$expected_blob_sha256"

nonce=$(uuidgen | tr '[:upper:]' '[:lower:]')
remote_transport_path="C:/Users/conbr/AppData/Local/Temp/oms-grouped-matching-${nonce}.ps1"
remote_native_path="C:\\Users\\conbr\\AppData\\Local\\Temp\\oms-grouped-matching-${nonce}.ps1"
remote_created=false

to_b64() { LC_ALL=C python3 -c 'import base64,sys; print(base64.b64encode(sys.stdin.buffer.read()).decode())'; }
remote_ps() {
  local encoded
  encoded=$(LC_ALL=C python3 -c 'import base64,sys; print(base64.b64encode(sys.stdin.read().encode("utf-16le")).decode())')
  ssh nuc "powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -EncodedCommand $encoded"
}
json_one() {
  python3 -c 'import json,sys; rows=[line for line in sys.stdin.read().splitlines() if line.strip()]; assert len(rows)==1, rows; print(json.dumps(json.loads(rows[0]),separators=(",",":")))'
}
remote_path_b64=$(printf '%s' "$remote_native_path" | to_b64)
assert_remote_parent_and_absent() {
  local command
  command="\$p=[Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('$remote_path_b64'));\$parent=Split-Path -LiteralPath \$p -Parent;while(\$parent){\$item=Get-Item -LiteralPath \$parent -Force -ErrorAction Stop;if((\$item.Attributes -band [IO.FileAttributes]::ReparsePoint)-ne 0){throw 'remote release parent invalid'};\$next=Split-Path -LiteralPath \$parent -Parent;if(-not \$next -or \$next -eq \$parent){break};\$parent=\$next};if(Test-Path -LiteralPath \$p){throw 'unique remote release path already exists'}"
  printf '%s' "$command" | remote_ps >/dev/null
}
cleanup_remote() {
  if [[ "$remote_created" != true ]]; then return; fi
  local command
  command="\$p=[Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('$remote_path_b64'));\$i=Get-Item -LiteralPath \$p -Force -ErrorAction Stop;if(\$i.PSIsContainer -or ((\$i.Attributes -band [IO.FileAttributes]::ReparsePoint)-ne 0)){throw 'remote release leaf invalid'};if((Get-FileHash -LiteralPath \$p -Algorithm SHA256).Hash.ToLowerInvariant() -cne '$script_sha256'){throw 'remote release ownership hash differs'};Remove-Item -LiteralPath \$p -Force -ErrorAction Stop;if(Test-Path -LiteralPath \$p){throw 'remote release leaf remains'}"
  printf '%s' "$command" | remote_ps >/dev/null
  remote_created=false
}
trap cleanup_remote EXIT

assert_remote_parent_and_absent
scp -q "$release_script" "nuc:$remote_transport_path"
remote_created=true

verify_command="\$p=[Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('$remote_path_b64'));\$i=Get-Item -LiteralPath \$p -Force -ErrorAction Stop;if(\$i.PSIsContainer -or ((\$i.Attributes -band [IO.FileAttributes]::ReparsePoint)-ne 0)){throw 'remote release leaf invalid'};\$parent=Split-Path -LiteralPath \$p -Parent;while(\$parent){\$parentItem=Get-Item -LiteralPath \$parent -Force -ErrorAction Stop;if((\$parentItem.Attributes -band [IO.FileAttributes]::ReparsePoint)-ne 0){throw 'remote release parent invalid'};\$next=Split-Path -LiteralPath \$parent -Parent;if(-not \$next -or \$next -eq \$parent){break};\$parent=\$next};if((Get-FileHash -LiteralPath \$p -Algorithm SHA256).Hash.ToLowerInvariant() -cne '$script_sha256'){throw 'remote release hash differs'};\$tokens=\$null;\$errors=\$null;[Management.Automation.Language.Parser]::ParseFile(\$p,[ref]\$tokens,[ref]\$errors)|Out-Null;if(\$errors.Count -ne 0){throw ((\$errors|ForEach-Object { \$_.Message }) -join ';')}"
printf '%s' "$verify_command" | remote_ps >/dev/null

invoke_mode() {
  local mode=$1 binding_b64=${2:-} backup_b64=${3:-} command
  command="\$p=[Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('$remote_path_b64')); & \$p -Mode $mode -ExpectedScriptSha256 '$script_sha256' -ExpectedMergedCommit '$merge_commit' -ExpectedMergedTree '$merged_tree'"
  if [[ -n "$binding_b64" ]]; then command+=" -BindingJsonBase64 '$binding_b64'"; fi
  if [[ -n "$backup_b64" ]]; then command+=" -ExpectedBackupPath ([Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('$backup_b64')))"; fi
  printf '%s' "$command" | remote_ps | json_one
}

preflight_json=$(invoke_mode Preflight)
preflight_json=$(printf '%s' "$preflight_json" | python3 -c 'import json,sys;x=json.load(sys.stdin);assert x["marker"]=="OMS_GROUPED_MATCHING_PREFLIGHT_COMPLETE";assert x["port"]==8765;assert (x["merged_commit"],x["merged_tree"])==(sys.argv[1],sys.argv[2]);print(json.dumps(x,separators=(",",":")))' "$merge_commit" "$merged_tree")
binding_b64=$(printf '%s' "$preflight_json" | to_b64)

# Exactly one Deploy call is permitted in this driver.
deploy_json=$(invoke_mode Deploy "$binding_b64")
backup_path=$(printf '%s' "$deploy_json" | python3 -c 'import json,sys;x=json.load(sys.stdin);assert x["marker"]=="OMS_GROUPED_MATCHING_DEPLOY_COMPLETE";assert (x["commit"],x["tree"])==(sys.argv[1],sys.argv[2]);print(x["new_backup"])' "$merge_commit" "$merged_tree")
backup_b64=$(printf '%s' "$backup_path" | to_b64)
postflight_json=$(invoke_mode Postflight "$binding_b64" "$backup_b64")
printf '%s' "$postflight_json" | python3 -c 'import json,sys;x=json.load(sys.stdin);assert x["marker"]=="OMS_GROUPED_MATCHING_POSTFLIGHT_COMPLETE";assert (x["commit"],x["tree"])==(sys.argv[1],sys.argv[2])' "$merge_commit" "$merged_tree"

cleanup_remote
python3 -c 'import json,sys;pre,dep,post,script_hash=json.load(sys.stdin);print(json.dumps({"marker":"OMS_GROUPED_MATCHING_DELIVERY_COMPLETE","old":{"commit":pre["old_commit"],"tree":pre["old_tree"],"listener_pid":pre["old_listener_pid"],"listener_creation_date":pre["old_listener_creation_date"]},"new":{"commit":post["commit"],"tree":post["tree"],"listener_pid":post["listener_pid"],"listener_creation_date":post["listener_creation_date"]},"markers":[pre["marker"],dep["marker"],post["marker"]],"backup_path":dep["new_backup"],"env_sha256":dep["env_sha256"],"script_sha256":script_hash},separators=(",",":")))' "$script_sha256" <<EOF
[
$preflight_json,
$deploy_json,
$postflight_json,
"$script_sha256"
]
EOF
