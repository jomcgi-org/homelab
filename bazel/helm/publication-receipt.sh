#!/usr/bin/env bash
# Render a completed chart publication from the verified per-chart records.
# Run from the source checkout. The caller must verify publication first.
set -euo pipefail

record_dir="${1:?Usage: publication-receipt.sh <record_dir> <source_sha>}"
source_sha="${2:?Usage: publication-receipt.sh <record_dir> <source_sha>}"
if [[ ! "$source_sha" =~ ^[0-9a-f]{40}$ ]]; then
	echo "Invalid publication source SHA" >&2
	exit 1
fi

shopt -s nullglob
declare -A seen=()
lines=("Chart-Source-Commit: $source_sha" "Chart-Publication-Complete: true")
for record in "$record_dir"/*; do
	if [[ -f "$record" ]]; then
		mapfile -t record_lines <"$record"
	else
		record_lines=()
	fi
	if [[ "${#record_lines[@]}" -ne 1 ]] ||
		! read -r chart version extra <<<"${record_lines[0]}" ||
		[[ ! "$chart" =~ ^projects/[A-Za-z0-9_./-]+$ || -n "$extra" ||
			"/$chart/" == *"/../"* || "/$chart/" == *"/./"* || "/$chart/" == *"//"* ||
			! -f "$chart/Chart.yaml" || ! "$version" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ||
			-n "${seen[$chart]:-}" ]]; then
		echo "Invalid publication record: $record" >&2
		exit 1
	fi
	seen[$chart]=1
	lines+=("Chart-Published: $chart $version")
done

printf '%s\n' "${lines[@]}"
