#!/usr/bin/env bash
# Keep a Renovate dependency bump publishable by advancing its wrapper chart.
set -euo pipefail

chart_dir="${1:?usage: bump-renovated-chart.sh <chart-dir>}"
chart_file="${chart_dir%/}/Chart.yaml"

[[ -f "$chart_file" ]] || {
	echo "ERROR: missing ${chart_file}" >&2
	exit 1
}

read_version() {
	grep '^version:' "$1" | head -1 | awk '{print $2}' | tr -d '"'
}

local_version="$(read_version "$chart_file")"
main_version="$(git show "origin/main:${chart_file}" | grep '^version:' | head -1 | awk '{print $2}' | tr -d '"')"

if [[ ! "$local_version" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] ||
	[[ ! "$main_version" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
	echo "ERROR: invalid chart versions (local ${local_version}, main ${main_version})" >&2
	exit 1
fi

# A branch update can run the task again after it already bumped the wrapper.
if [[ "$local_version" != "$main_version" ]]; then
	echo "Wrapper already bumped: ${local_version} (main ${main_version})"
	exit 0
fi

IFS=. read -r major minor patch <<<"$main_version"
new_version="${major}.${minor}.$((patch + 1))"
sed "s/^version:.*/version: ${new_version}/" "$chart_file" >"${chart_file}.tmp"
mv "${chart_file}.tmp" "$chart_file"
echo "Bumped ${chart_file}: ${main_version} -> ${new_version}"
