#!/bin/sh
set -eu

repo=$(CDPATH= cd -- "$(dirname -- "$0")/../.." && pwd)
source_plist="$repo/tools/session_collector/launchd/dev.jomcgi.session-collector.plist"
target_dir="$HOME/Library/LaunchAgents"
target_plist="$target_dir/dev.jomcgi.session-collector.plist"

if [ ! -x "$repo/.venv/bin/python3" ] ||
	! "$repo/.venv/bin/python3" -c 'import httpx' >/dev/null 2>&1; then
	printf '%s\n' "python3 -m venv $repo/.venv" >&2
	printf '%s\n' "$repo/.venv/bin/pip install httpx" >&2
	exit 1
fi

base_url=$(cd "$repo" && "$repo/.venv/bin/python3" -m tools.session_collector.base_url)
mkdir -p "$target_dir" "$HOME/Library/Logs" \
	"$HOME/Library/Application Support/homelab/session-collector"
sed -e "s|__REPO__|$repo|g" -e "s|__HOME__|$HOME|g" \
	-e "s|__BASE_URL__|$base_url|g" "$source_plist" >"$target_plist"
launchctl bootout "gui/$(id -u)/dev.jomcgi.session-collector" 2>/dev/null || true
sleep 1
launchctl bootstrap "gui/$(id -u)" "$target_plist"
