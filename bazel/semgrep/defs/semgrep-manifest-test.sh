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
# Exit code 0 = no findings, 1 = findings, 2 = scan infrastructure failure.
#
# Env: SEMGREP_EXCLUDE_RULES, comma-separated items to skip. Each item is used in two ways:
#      1. Matched against YAML filename (basename without .yaml) to exclude entire config files
#      2. Matched as a suffix against semgrep check_ids to exclude individual rule findings
#      UPLOAD_SCRIPT, Semgrep App lifecycle helper

set -euo pipefail

if [[ $# -lt 5 ]]; then
	echo "Usage: $0 <helm> <release> <chart> <namespace> <rules...> -- <values...>"
	exit 2
fi

if [[ -z "${SEMGREP_APP_TOKEN:-}" || -z "${SEMGREP_APP_TOKEN//[[:space:]]/}" ]]; then
	echo "ERROR: SEMGREP_APP_TOKEN is required for configured Semgrep CI scans" >&2
	exit 2
fi
export SEMGREP_URL="${SEMGREP_URL:-https://semgrep.dev}"

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
	exit 2
fi

# OSS engine is a runtime dependency — must be co-located for Pro to work
SEMGREP_CORE=$(find "$SEARCH_ROOT" -name "semgrep-core" -not -name "*proprietary*" \( -type f -o -type l \) 2>/dev/null | head -1)
if [[ -z "$SEMGREP_CORE" ]]; then
	echo "ERROR: semgrep-core (OSS) not found — required as Pro runtime dependency"
	exit 2
fi

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

# Probe both staged engines so loader and dependency failures fail closed.
for probe in semgrep-core semgrep-core-proprietary; do
	PROBE_EXIT=0
	PROBE_STDERR="${TEST_TMPDIR}/${probe}.probe.stderr"
	PROBE_VERSION=$("$PRO_DIR/$probe" -version 2>"$PROBE_STDERR") || PROBE_EXIT=$?
	if [[ "$PROBE_EXIT" -ne 0 || -z "${PROBE_VERSION//[[:space:]]/}" ]]; then
		head -c 4096 "$PROBE_STDERR" >&2
		echo "ERROR: $probe version probe failed (exit=$PROBE_EXIT)" >&2
		exit 2
	fi
	if [[ "$probe" == "semgrep-core" ]]; then
		SEMGREP_ENGINE_VERSION="${PROBE_VERSION%%$'\n'*}"
		export SEMGREP_ENGINE_VERSION
	fi
done

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
	"${VALUES_ARGS[@]}" >"$MANIFESTS"; then
	echo "ERROR: Helm template rendering failed" >&2
	exit 2
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
		echo "ERROR: semgrep-core exited $SCAN_EXIT on $(basename "$rule_file")" >&2
		cat "$STDERR_FILE" >&2
		exit 2
	fi

	RESULT_INDEX=$((RESULT_INDEX + 1))
done

# Merge results into a single JSON and determine findings
MERGED_FILE="${TEST_TMPDIR}/results.json"
SCAN_EXIT=0
python3 - "$RESULTS_DIR" "$MERGED_FILE" "$RESULT_INDEX" <<'PYEOF' || SCAN_EXIT=$?
import glob
import json
import os
import sys

results_dir = sys.argv[1]
output_file = sys.argv[2]
expected_count = int(sys.argv[3])
merged = {"results": [], "errors": [], "paths": {"scanned": []}}
scanned = set()

files = sorted(glob.glob(os.path.join(results_dir, "result_*.json")))
try:
    if expected_count == 0:
        raise ValueError("no engine scans were executed")
    if len(files) != expected_count:
        raise ValueError(f"expected {expected_count} engine outputs, found {len(files)}")
    for f in files:
        with open(f) as stream:
            data = json.load(stream)
        if not isinstance(data, dict):
            raise ValueError(f"{f} is not a JSON object")
        if not isinstance(data.get("results"), list):
            raise ValueError(f"{f} has no results list")
        if not isinstance(data.get("errors"), list):
            raise ValueError(f"{f} has no errors list")
        if data["errors"]:
            raise ValueError(f"{f} reports {len(data['errors'])} engine errors")
        paths = data.get("paths")
        if not isinstance(paths, dict) or not isinstance(paths.get("scanned"), list):
            raise ValueError(f"{f} has no paths.scanned list")
        if not all(isinstance(path, str) and path.strip() for path in paths["scanned"]):
            raise ValueError(f"{f} has an invalid scanned path")
        scanned.update(paths["scanned"])
        merged["results"].extend(data["results"])
    if not scanned:
        raise ValueError("engine reported zero scanned paths")
except (OSError, ValueError, TypeError, json.JSONDecodeError) as error:
    print(f"ERROR: invalid Semgrep engine output: {error}", file=sys.stderr)
    sys.exit(2)

with open(output_file, "w") as stream:
    merged["paths"]["scanned"] = sorted(scanned)
    json.dump(merged, stream)

print(f"SCANNED: {len(scanned)} engine-confirmed path(s), {len(files)} pass(es)")

if merged["results"]:
    sys.exit(1)
PYEOF

if [[ "$SCAN_EXIT" -gt 1 ]]; then
	exit "$SCAN_EXIT"
fi

# When rule-ID exclusions are set, post-filter the JSON results.
if [[ "$SCAN_EXIT" -eq 1 && ${#EXCLUDE_IDS[@]} -gt 0 ]]; then
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

# Best-effort App lifecycle. The helper uses only declared inputs and
# environment metadata, and its bounded remote failures cannot alter SCAN_EXIT.
if [[ -n "${UPLOAD_SCRIPT:-}" ]]; then
	"$UPLOAD_SCRIPT" "$MERGED_FILE" "$SCAN_EXIT" 2>&1 ||
		echo "WARNING: Semgrep App helper failed (non-fatal)" >&2
else
	echo "WARNING: Semgrep App helper is not configured (non-fatal)" >&2
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
