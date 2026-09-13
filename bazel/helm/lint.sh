#!/usr/bin/env bash
# Lint repository Helm charts changed from a base ref with Helm from PATH.

set -euo pipefail

usage() {
	cat <<'EOF'
Usage: lint.sh [--base REF]

Run helm lint --strict once for each Helm chart with changed files. Changes
include committed and working-tree changes from the merge base, plus untracked
files. REF defaults to origin/main or the CI_BASE_REF environment variable.

Options:
  --base REF   Git base ref (default: CI_BASE_REF or origin/main)
  -h, --help   Show this help
EOF
}

die() {
	printf 'lint: %s\n' "$*" >&2
	exit 1
}

BASE_REF="${CI_BASE_REF:-origin/main}"
while [[ $# -gt 0 ]]; do
	case "$1" in
	--base)
		[[ $# -ge 2 ]] || {
			printf 'lint: --base requires a ref\n' >&2
			exit 2
		}
		BASE_REF="$2"
		shift 2
		;;
	-h | --help)
		usage
		exit 0
		;;
	*)
		printf 'lint: unexpected argument: %s\n' "$1" >&2
		usage >&2
		exit 2
		;;
	esac
done

REPO_ROOT="$(git rev-parse --show-toplevel 2>/dev/null)" ||
	die "not inside a git work tree"
REPO_ROOT="$(cd "$REPO_ROOT" && pwd -P)"
cd "$REPO_ROOT"

git rev-parse --verify "$BASE_REF^{commit}" >/dev/null 2>&1 ||
	die "base ref does not resolve to a commit: $BASE_REF"
MERGE_BASE="$(git merge-base HEAD "$BASE_REF")" ||
	die "cannot find a merge base with $BASE_REF"

CHARTS=()
add_chart() {
	local chart="$1"
	local existing
	if [[ ${#CHARTS[@]} -gt 0 ]]; then
		for existing in "${CHARTS[@]}"; do
			[[ "$existing" == "$chart" ]] && return 0
		done
	fi
	CHARTS+=("$chart")
}

find_changed_chart() {
	local changed_file="$1"
	local directory
	if [[ "$changed_file" == */* ]]; then
		directory="${changed_file%/*}"
	else
		directory="."
	fi

	while [[ "$directory" != "." && "$directory" != "/" ]]; do
		if [[ -f "$directory/Chart.yaml" ]]; then
			add_chart "$directory"
			return 0
		fi
		if [[ "$directory" == */* ]]; then
			directory="${directory%/*}"
		else
			directory="."
		fi
	done
	if [[ -f Chart.yaml ]]; then
		add_chart "."
	fi
	return 0
}

CHANGED_FILES="$(mktemp "${TMPDIR:-/tmp}/helm-lint-changed.XXXXXX")"
SORTED_FILES="$(mktemp "${TMPDIR:-/tmp}/helm-lint-sorted.XXXXXX")"
cleanup() {
	rm -f "$CHANGED_FILES" "$SORTED_FILES"
}
trap cleanup EXIT
{
	git diff --name-only --diff-filter=ACMRD "$MERGE_BASE" --
	git ls-files --others --exclude-standard
} >"$CHANGED_FILES"
sed '/^$/d' "$CHANGED_FILES" | sort -u >"$SORTED_FILES"

while IFS= read -r changed_file; do
	[[ -n "$changed_file" ]] && find_changed_chart "$changed_file"
done <"$SORTED_FILES"

if [[ ${#CHARTS[@]} -eq 0 ]]; then
	printf 'No changed Helm charts relative to %s\n' "$BASE_REF"
	exit 0
fi

HELM_BIN="${HELM:-helm}"
command -v "$HELM_BIN" >/dev/null 2>&1 ||
	die "helm not found; run ./bootstrap.sh and ensure the extracted tools are on PATH"

failure_status=0
for chart in "${CHARTS[@]}"; do
	printf 'Linting %s\n' "$chart"
	set +e
	"$HELM_BIN" lint --strict "$chart"
	rc=$?
	set -e
	if [[ $rc -ne 0 ]]; then
		printf 'lint: helm lint failed for %s with exit status %s\n' "$chart" "$rc" >&2
		[[ $failure_status -ne 0 ]] || failure_status="$rc"
	fi
done

exit "$failure_status"
