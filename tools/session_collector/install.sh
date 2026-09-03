#!/bin/sh
set -eu

repo=$(CDPATH= cd -- "$(dirname -- "$0")/../.." && pwd)
source_plist="$repo/tools/session_collector/launchd/dev.jomcgi.session-collector.plist"
target_dir="$HOME/Library/LaunchAgents"
target_plist="$target_dir/dev.jomcgi.session-collector.plist"

mkdir -p "$target_dir" "$HOME/Library/Logs"
sed -e "s|__REPO__|$repo|g" -e "s|__HOME__|$HOME|g" "$source_plist" >"$target_plist"
launchctl bootstrap "gui/$(id -u)" "$target_plist"
