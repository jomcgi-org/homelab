#!/usr/bin/env bash
set -euo pipefail

script_source="$(realpath "$1")"
if [[ $# -gt 1 ]]; then
	repo_root="$(realpath "$2")"
else
	repo_root="$(cd "$(dirname "$script_source")/../.." && pwd)"
fi
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

check_chart_file() {
	local path="$1"
	local rel="${path#"$repo_root"/}"
	local chart_dir="${rel%/Chart.yaml}"
	local render="//${chart_dir}:manifests/all.yaml"
	local application_dir=""

	# The existing named-chart loop owns this convention and maps it to the
	# sibling deploy package. Avoid reporting the same missing chart twice.
	if [[ "$chart_dir" == */chart ]]; then
		return
	fi

	if grep -Fq "\"${render}\"" "$registry" || is_skipped "$chart_dir"; then
		return
	fi

	# Published application charts may live below an application's helm/
	# directory while their admissibility render lives in its deploy package.
	if [[ "$chart_dir" == */helm/* ]]; then
		application_dir="${chart_dir%%/helm/*}"
		render="//${application_dir}/deploy:manifests/all.yaml"
		if grep -Fq "\"${render}\"" "$registry"; then
			return
		fi
	fi

	echo "missing chart admissibility coverage: $chart_dir" >&2
	missing=1
}

paths="$(find "$repo_root/projects" -path '*/deploy/values*.yaml' -type f | sort)"
while IFS= read -r path; do
	[[ -n "$path" ]] && check_values_file "$path"
done <<<"$paths"

paths="$(find "$repo_root/projects" -type d -name chart -not -path '*/charts/*' | sort)"
while IFS= read -r path; do
	[[ -n "$path" ]] && check_chart_dir "$path"
done <<<"$paths"

paths="$(find "$repo_root/projects" -name Chart.yaml -not -path '*/charts/*' -type f | sort)"
while IFS= read -r path; do
	[[ -n "$path" ]] && check_chart_file "$path"
done <<<"$paths"

if [[ $missing -ne 0 ]]; then
	exit 1
fi

echo "Every deploy values file and first-party chart source is rendered or documents its skip."
