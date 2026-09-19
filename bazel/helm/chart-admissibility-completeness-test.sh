#!/usr/bin/env bash
set -euo pipefail

script_source="$(realpath "$1")"
repo_root="$(cd "$(dirname "$script_source")/../.." && pwd)"
registry="$repo_root/bazel/helm/BUILD"

missing=0

is_skipped() {
	local path="$1"
	grep -Fq "\"${path}\":" "$registry"
}

check_values_file() {
	local path="$1"
	local rel="${path#"$repo_root"/}"
	local deploy_dir="${rel%/*}"
	local filename="${rel##*/}"
	local suffix="${filename#values}"
	suffix="${suffix%.yaml}"
	suffix="${suffix//-/_}"
	local render="//${deploy_dir}:manifests${suffix}/all.yaml"

	if grep -Fq "\"${render}\"" "$registry" || is_skipped "$rel"; then
		return
	fi
	echo "missing chart admissibility coverage: $rel" >&2
	missing=1
}

check_chart_dir() {
	local path="$1"
	local rel="${path#"$repo_root"/}"
	local parent="${rel%/chart}"
	local render="//${parent}/deploy:manifests/all.yaml"

	if grep -Fq "\"${render}\"" "$registry" || is_skipped "$rel" || is_skipped "$parent"; then
		return
	fi
	echo "missing chart admissibility coverage: $rel" >&2
	missing=1
}

while IFS= read -r path; do
	check_values_file "$path"
done < <(find "$repo_root/projects" -path '*/deploy/values*.yaml' -type f | sort)

while IFS= read -r path; do
	check_chart_dir "$path"
done < <(find "$repo_root/projects" -type d -name chart | sort)

if [[ $missing -ne 0 ]]; then
	exit 1
fi

echo "Every deploy values file and chart directory is rendered or documents its skip."
