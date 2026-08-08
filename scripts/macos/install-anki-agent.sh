#!/bin/sh
set -eu

agent_home=${HOME}
agent_executable=
hub_url=${OMS_ANKI_AGENT_HUB_URL:-}
agent_id=${OMS_ANKI_AGENT_AGENT_ID:-connor-mac}
load_agent=1

while [ "$#" -gt 0 ]; do
  case "$1" in
    --home)
      agent_home=$2
      shift 2
      ;;
    --executable)
      agent_executable=$2
      shift 2
      ;;
    --hub-url)
      hub_url=$2
      shift 2
      ;;
    --agent-id)
      agent_id=$2
      shift 2
      ;;
    --no-load)
      load_agent=0
      shift
      ;;
    *)
      echo "Unknown option: $1" >&2
      exit 2
      ;;
  esac
done

if [ -z "$agent_executable" ]; then
  agent_executable=$(command -v oms-anki-agent || true)
fi
if [ -z "$agent_executable" ] || [ ! -x "$agent_executable" ]; then
  echo "oms-anki-agent executable was not found" >&2
  exit 1
fi
case "$agent_executable" in
  /*) ;;
  *)
    echo "oms-anki-agent executable must be an absolute path" >&2
    exit 1
    ;;
esac
case "$hub_url" in
  https://*) ;;
  *)
    echo "--hub-url must be an HTTPS origin" >&2
    exit 1
    ;;
esac

script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
template="$script_dir/com.omsstudy.anki-agent.plist"
launch_dir="$agent_home/Library/LaunchAgents"
log_dir="$agent_home/Library/Logs/OMSStudyHub"
target="$launch_dir/com.omsstudy.anki-agent.plist"
mkdir -p "$launch_dir" "$log_dir"

escape_replacement() {
  printf '%s' "$1" | sed 's/[&|\\]/\\&/g'
}

escaped_executable=$(escape_replacement "$agent_executable")
escaped_hub_url=$(escape_replacement "$hub_url")
escaped_agent_id=$(escape_replacement "$agent_id")
escaped_log_dir=$(escape_replacement "$log_dir")
temporary=$(mktemp "$launch_dir/.anki-agent.XXXXXX")
trap 'rm -f "$temporary"' EXIT HUP INT TERM
sed \
  -e "s|__EXECUTABLE__|$escaped_executable|g" \
  -e "s|__HUB_URL__|$escaped_hub_url|g" \
  -e "s|__AGENT_ID__|$escaped_agent_id|g" \
  -e "s|__LOG_DIR__|$escaped_log_dir|g" \
  "$template" > "$temporary"
chmod 600 "$temporary"
mv "$temporary" "$target"
trap - EXIT HUP INT TERM

user_id=$(id -u)
if [ "$load_agent" -eq 1 ]; then
  launchctl bootout "gui/$user_id/com.omsstudy.anki-agent" 2>/dev/null || true
  launchctl bootstrap "gui/$user_id" "$target"
fi

echo "Installed $target"
echo "Status: launchctl print gui/$user_id/com.omsstudy.anki-agent"
