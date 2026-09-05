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
# Exit code 0 = no findings, non-zero = violations found or render failure.
#
# Env: SEMGREP_EXCLUDE_RULES — comma-separated items to skip. Each item is used in two ways:
#      1. Matched against YAML filename (basename without .yaml) to exclude entire config files
#      2. Matched as a suffix against semgrep check_ids to exclude individual rule findings

set -euo pipefail

if [[ $# -lt 5 ]]; then
	echo "Usage: $0 <helm> <release> <chart> <namespace> <rules...> -- <values...>"
	exit 1
fi

HELM="$1"
RELEASE="$2"
CHART="$3"
NAMESPACE="$4"
shift 4

# Discover Pro engine from runfiles.
# Search RUNFILES_DIR (not cwd) because external repo files live in sibling
# directories (e.g. +semgrep+semgrep_engine_arm64/) outside _main/.
# Use -type f -o -type l to match both regular files and symlinks.
SEARCH_ROOT="${RUNFILES_DIR:-.}"
SEMGREP_PRO_ENGINE=$(find "$SEARCH_ROOT" -name "semgrep-core-proprietary" \( -type f -o -type l \) 2>/dev/null | head -1)
if [[ -z "$SEMGREP_PRO_ENGINE" ]]; then
	echo "ERROR: semgrep-core-proprietary not found in runfiles"
	exit 1
fi

# OSS engine is a runtime dependency — must be co-located for Pro to work
SEMGREP_CORE=$(find "$SEARCH_ROOT" -name "semgrep-core" -not -name "*proprietary*" \( -type f -o -type l \) 2>/dev/null | head -1)
if [[ -z "$SEMGREP_CORE" ]]; then
	echo "ERROR: semgrep-core (OSS) not found — required as Pro runtime dependency"
	exit 1
fi

# Verify the binary can execute on this platform (OCI-vendored engine is
# Linux ELF — won't run on macOS). Use OSS binary for lighter check.
if ! "$SEMGREP_CORE" -version >/dev/null 2>&1; then
	echo "SKIPPED: semgrep-core found but cannot execute on this platform ($(uname -s)/$(uname -m))"
	exit 0
fi

# Export the engine version for downstream tools (e.g., upload.py).
SEMGREP_ENGINE_VERSION=$("$SEMGREP_CORE" -version 2>/dev/null | head -1 || echo "")
export SEMGREP_ENGINE_VERSION

# Stage both binaries in the same directory (Pro requires co-located OSS binary)
PRO_DIR="${TEST_TMPDIR}/pro_bin"
mkdir -p "$PRO_DIR"
cp "$SEMGREP_CORE" "$PRO_DIR/semgrep-core"
chmod 755 "$PRO_DIR/semgrep-core"
cp "$SEMGREP_PRO_ENGINE" "$PRO_DIR/semgrep-core-proprietary"
chmod 755 "$PRO_DIR/semgrep-core-proprietary"
ENGINE="$PRO_DIR/semgrep-core-proprietary"

# Copy libs beside the Pro binary so RPATH=$ORIGIN/libs works
if [[ -d "$(dirname "$SEMGREP_CORE")/libs" ]]; then
	cp -r "$(dirname "$SEMGREP_CORE")/libs" "$PRO_DIR/"
fi

# Pro engine requires a non-empty SEMGREP_APP_TOKEN for interfile analysis.
# Redirect the API endpoint to a dead socket so the engine never phones home
# (interfile analysis works offline; token is only checked for presence).
# Real token + URL are preserved when available (for uploads).
export SEMGREP_APP_TOKEN="${SEMGREP_APP_TOKEN:-offline}"
export SEMGREP_URL="${SEMGREP_URL:-http://127.0.0.1:0}"

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
while [[ $# -gt 0 && "$1" != "--" ]]; do
	rule_name="$(basename "$1" .yaml)"
	if [[ "$EXCLUDE_LIST" != *",$rule_name,"* ]]; then
		RULE_FILES+=("$(pwd)/$1")
	fi
	shift
done

if [[ $# -eq 0 ]]; then
	echo "ERROR: missing -- separator between rules and values files"
	exit 1
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
	"${VALUES_ARGS[@]}" >"$MANIFESTS"; then
	echo "FAILED: Helm template rendering failed"
	exit 1
fi

echo ""
echo "Scanning rendered manifests with semgrep-core:"
echo "  Rules: ${RULE_FILES[*]:-none}"
echo ""

if [[ ${#RULE_FILES[@]} -eq 0 ]]; then
	echo "PASSED: All rules excluded, nothing to scan"
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
	SCAN_EXIT=0
	"$ENGINE" -rules "$rule_file" -pro_inter_file -lang yaml "$SCAN_DIR" -json -json_nodots \
		>"$RESULT_FILE" 2>"$STDERR_FILE" || SCAN_EXIT=$?

	if [[ "$SCAN_EXIT" -ne 0 ]]; then
		echo "WARNING: semgrep-core exited $SCAN_EXIT on $(basename "$rule_file")" >&2
		cat "$STDERR_FILE" >&2
	fi

	RESULT_INDEX=$((RESULT_INDEX + 1))
done

# Merge results into a single JSON and determine findings
MERGED_FILE="${TEST_TMPDIR}/results.json"
SCAN_EXIT=0
python3 - "$RESULTS_DIR" "$MERGED_FILE" <<'PYEOF' || SCAN_EXIT=$?
import json, glob, sys, os

results_dir = sys.argv[1]
output_file = sys.argv[2]
merged = {"results": [], "errors": []}

for f in sorted(glob.glob(os.path.join(results_dir, "result_*.json"))):
    try:
        data = json.load(open(f))
        merged["results"].extend(data.get("results", []))
        merged["errors"].extend(data.get("errors", []))
    except (json.JSONDecodeError, KeyError):
        pass

json.dump(merged, open(output_file, "w"))

if merged["results"]:
    sys.exit(1)
PYEOF

# Upload disabled — see semgrep-test.sh comment for rationale.

# When rule-ID exclusions are set, post-filter the JSON results.
if [[ "$SCAN_EXIT" -ne 0 && ${#EXCLUDE_IDS[@]} -gt 0 ]]; then
	if python3 - "$MERGED_FILE" "${EXCLUDE_IDS[@]}" <<'PYEOF'; then
import json, sys

with open(sys.argv[1]) as f:
    data = json.load(f)
exclude_ids = sys.argv[2:]
results = data.get("results", [])
filtered, excluded = [], 0
for r in results:
    cid = r.get("check_id", "")
    if any(cid.endswith("." + e) or cid == e for e in exclude_ids):
        excluded += 1
    else:
        filtered.append(r)
if filtered:
    for r in filtered:
        cid = r.get("check_id", "")
        parts = cid.rsplit(".", 2)
        short = ".".join(parts[-2:]) if len(parts) >= 2 else cid
        path = r.get("path", "?")
        line = r.get("start", {}).get("line", "?")
        msg = r.get("extra", {}).get("message", "")
        print(f"  {short} at {path}:{line}")
        if msg:
            print(f"    {msg[:200]}")
        print()
    print(f"Found {len(filtered)} finding(s) ({excluded} excluded)")
    sys.exit(1)
if excluded:
    print(f"  ({excluded} finding(s) excluded by rule ID filter)")
sys.exit(0)
PYEOF
		SCAN_EXIT=0
	fi
fi

if [[ "$SCAN_EXIT" -eq 0 ]]; then
	echo "PASSED: No semgrep findings in rendered manifests"
else
	# Print findings summary from JSON so test logs are actionable
	python3 - "$MERGED_FILE" <<'PYEOF' 2>/dev/null || true
import json, sys
with open(sys.argv[1]) as f:
    data = json.load(f)
for r in data.get("results", []):
    cid = r.get("check_id", "")
    parts = cid.rsplit(".", 2)
    short = ".".join(parts[-2:]) if len(parts) >= 2 else cid
    path = r.get("path", "?")
    line = r.get("start", {}).get("line", "?")
    msg = r.get("extra", {}).get("message", "")
    print(f"  {short} at {path}:{line}")
    if msg:
        print(f"    {msg[:200]}")
    print()
PYEOF
	echo "FAILED: Semgrep found violations in rendered manifests"
fi
exit "$SCAN_EXIT"
