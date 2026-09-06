#!/usr/bin/env bash
# semgrep-manifest-test.sh - Renders Helm manifests and scans with semgrep-core Pro
#
# Usage: semgrep-manifest-test.sh <helm> <release> <chart> <namespace> <rules...> -- <values-files...>
#
# The semgrep-core binary is discovered via find(1) in runfiles rather than
# passed as an argument, because Bazel's $(rootpath) can't resolve platform-
# specific select() targets in sh_test args.
#
# Combines helm template rendering with semgrep-core scanning in a single test.
# Exit code 0 = clean, 1 = findings, 2 = infrastructure failure.
#
# Env: SEMGREP_EXCLUDE_RULES — comma-separated items to skip. Each item is used in two ways:
#      1. Matched against YAML filename (basename without .yaml) to exclude entire config files
#      2. Matched as a suffix against semgrep check_ids to exclude individual rule findings

set -euo pipefail

if [[ $# -lt 5 ]]; then
	echo "Usage: $0 <helm> <release> <chart> <namespace> <rules...> -- <values...>"
	exit 2
fi

HELM="$1"
RELEASE="$2"
CHART="$3"
NAMESPACE="$4"
shift 4

source "$(dirname "${BASH_SOURCE[0]}")/semgrep-common.sh"
semgrep_setup

# Parse exclude items: filename-based exclusion (EXCLUDE_LIST) and
# rule-ID-based exclusion (EXCLUDE_IDS).
EXCLUDE_LIST=",${SEMGREP_EXCLUDE_RULES:-},"
EXCLUDE_IDS=()
if [[ -n "${SEMGREP_EXCLUDE_RULES:-}" ]]; then
	IFS=',' read -ra _EXCLUDE_ITEMS <<<"$SEMGREP_EXCLUDE_RULES"
	for _item in "${_EXCLUDE_ITEMS[@]}"; do
		_item="${_item## }"
		_item="${_item%% }"
		if [[ -n "$_item" ]]; then
			EXCLUDE_IDS+=("$_item")
		fi
	done
fi

# Collect rule files until -- separator, skipping excluded rules
RULE_FILES=()
RULE_COUNT=0
while [[ $# -gt 0 && "$1" != "--" ]]; do
	RULE_COUNT=$((RULE_COUNT + 1))
	rule_name="$(basename "$1" .yaml)"
	if [[ "$EXCLUDE_LIST" != *",$rule_name,"* ]]; then
		RULE_FILES+=("$(pwd)/$1")
	fi
	shift
done

if [[ $# -eq 0 ]]; then
	echo "ERROR: missing -- separator between rules and values files"
	exit 2
fi
shift # skip --

# Build values arguments
VALUES_ARGS=()
for vf in "$@"; do
	VALUES_ARGS+=("--values" "$vf")
done

# Render manifests to a temp file with .yaml extension (semgrep needs it)
MANIFESTS="${TEST_TMPDIR}/rendered-manifests.yaml"

echo "Rendering manifests:"
echo "  Release:   $RELEASE"
echo "  Chart:     $CHART"
echo "  Namespace: $NAMESPACE"
echo "  Values:    $*"

if ! "$HELM" template "$RELEASE" "$CHART" \
	--namespace "$NAMESPACE" \
	${VALUES_ARGS[@]+"${VALUES_ARGS[@]}"} >"$MANIFESTS"; then
	echo "INFRASTRUCTURE: Helm template rendering failed"
	exit 2
fi

[[ -s "$MANIFESTS" ]] && LC_ALL=C grep -q '[^[:space:]]' "$MANIFESTS" || semgrep_error "Helm rendered an empty manifest"

echo ""
echo "Scanning rendered manifests with semgrep-core:"
echo "  Rules: ${RULE_FILES[*]:-none}"
echo ""

if [[ ${#RULE_FILES[@]} -eq 0 ]]; then
	[[ "$RULE_COUNT" -gt 0 ]] || semgrep_error "no rules supplied"
	echo "POLICY: All $RULE_COUNT rule files explicitly excluded; no scan required"
	exit 0
fi

# Copy rendered manifest into a scan directory for -lang yaml <dir> invocation
SCAN_DIR="${TEST_TMPDIR}/manifest_scan"
mkdir -p "$SCAN_DIR"
cp "$MANIFESTS" "$SCAN_DIR/rendered-manifests.yaml"

# Run semgrep-core once per rule file with interfile analysis, merge JSON results
RESULTS_DIR="${TEST_TMPDIR}/results"
mkdir -p "$RESULTS_DIR"
RESULT_INDEX=0

for rule_file in "${RULE_FILES[@]}"; do
	RESULT_FILE="$RESULTS_DIR/result_${RESULT_INDEX}.json"
	STDERR_FILE="${TEST_TMPDIR}/stderr_${RESULT_INDEX}.txt"
	semgrep_run_pass "MANIFEST rule=$rule_file lang=yaml" \
		-rules "$rule_file" -pro_inter_file -lang yaml "$SCAN_DIR" -json -json_nodots

	RESULT_INDEX=$((RESULT_INDEX + 1))
done

# Validate every pass before finding-only filters can change the result.
MERGED_FILE="${TEST_TMPDIR}/results.json"
semgrep_merge

# Upload disabled — see semgrep-test.sh comment for rationale.

semgrep_finish
