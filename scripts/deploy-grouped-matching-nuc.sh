#!/usr/bin/env bash
# One-use driver for the reviewed grouped-matching release. Do not generalize.
set -euo pipefail

script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
release_script="$script_dir/deploy-grouped-matching-release.ps1"
binding_file="/tmp/oms-grouped-matching-release-binding.txt"
test -f "$release_script"
test -f "$binding_file"
read -r release_commit release_tree merge_commit merged_tree < "$binding_file"
[[ "$release_commit" =~ ^[0-9a-f]{40}$ && "$release_tree" =~ ^[0-9a-f]{40}$ && "$merge_commit" =~ ^[0-9a-f]{40}$ && "$merged_tree" =~ ^[0-9a-f]{40}$ ]]

nonce=$(uuidgen | tr '[:upper:]' '[:lower:]')
remote_transport_path="C:/Users/conbr/AppData/Local/Temp/oms-grouped-matching-${nonce}.ps1"
remote_native_path="C:\\Users\\conbr\\AppData\\Local\\Temp\\oms-grouped-matching-${nonce}.ps1"
script_sha256=$(shasum -a 256 "$release_script" | awk '{print $1}')
[[ "$script_sha256" =~ ^[0-9a-f]{64}$ ]]

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
cleanup_remote() {
  local command
  command="\$p=[Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('$remote_path_b64'));if(Test-Path -LiteralPath \$p){\$i=Get-Item -LiteralPath \$p -Force -ErrorAction Stop;if(\$i.PSIsContainer -or ((\$i.Attributes -band [IO.FileAttributes]::ReparsePoint)-ne 0)){throw 'remote release leaf invalid'};\$parent=Split-Path -LiteralPath \$p -Parent;while(\$parent){\$parentItem=Get-Item -LiteralPath \$parent -Force -ErrorAction Stop;if((\$parentItem.Attributes -band [IO.FileAttributes]::ReparsePoint)-ne 0){throw 'remote release parent invalid'};\$next=Split-Path -LiteralPath \$parent -Parent;if(-not \$next -or \$next -eq \$parent){break};\$parent=\$next};Remove-Item -LiteralPath \$p -Force -ErrorAction Stop};if(Test-Path -LiteralPath \$p){throw 'remote release leaf remains'}"
  printf '%s' "$command" | remote_ps
}
trap cleanup_remote EXIT

# Refuse to overwrite anything, then send the tracked artifact once.
printf '%s' "\$p=[Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('$remote_path_b64'));if(Test-Path -LiteralPath \$p){throw 'unique remote release path already exists'}" | remote_ps
scp -q "$release_script" "nuc:$remote_transport_path"

verify_command="\$p=[Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('$remote_path_b64'));\$i=Get-Item -LiteralPath \$p -Force -ErrorAction Stop;if(\$i.PSIsContainer -or ((\$i.Attributes -band [IO.FileAttributes]::ReparsePoint)-ne 0)){throw 'remote release leaf invalid'};\$parent=Split-Path -LiteralPath \$p -Parent;while(\$parent){\$parentItem=Get-Item -LiteralPath \$parent -Force -ErrorAction Stop;if((\$parentItem.Attributes -band [IO.FileAttributes]::ReparsePoint)-ne 0){throw 'remote release parent invalid'};\$next=Split-Path -LiteralPath \$parent -Parent;if(-not \$next -or \$next -eq \$parent){break};\$parent=\$next};if((Get-FileHash -LiteralPath \$p -Algorithm SHA256).Hash.ToLowerInvariant() -cne '$script_sha256'){throw 'remote release hash differs'};\$tokens=\$null;\$errors=\$null;[Management.Automation.Language.Parser]::ParseFile(\$p,[ref]\$tokens,[ref]\$errors)|Out-Null;if(\$errors.Count -ne 0){throw ((\$errors|ForEach-Object { \$_.Message }) -join ';')}"
printf '%s' "$verify_command" | remote_ps

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
[[ -n "$backup_path" ]]
backup_b64=$(printf '%s' "$backup_path" | to_b64)
postflight_json=$(invoke_mode Postflight "$binding_b64" "$backup_b64")
printf '%s' "$postflight_json" | python3 -c 'import json,sys;x=json.load(sys.stdin);assert x["marker"]=="OMS_GROUPED_MATCHING_POSTFLIGHT_COMPLETE";assert (x["commit"],x["tree"])==(sys.argv[1],sys.argv[2])' "$merge_commit" "$merged_tree"
