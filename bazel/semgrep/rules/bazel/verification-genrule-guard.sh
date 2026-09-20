#!/usr/bin/env bash
# Scan suite-shaped genrules in exact BUILD and BUILD.bazel files.

set -euo pipefail

if [[ $# -ne 1 || ! -d "$1" ]]; then
	echo "Usage: $0 <source-root>" >&2
	exit 2
fi

SOURCE_ROOT=$(cd "$1" && pwd)
RUNFILES_ROOT="${RUNFILES_DIR:-.}"

resolve_main_file() {
	local relative="$1"
	if [[ -f "$RUNFILES_ROOT/_main/$relative" ]]; then
		printf '%s\n' "$RUNFILES_ROOT/_main/$relative"
	elif [[ -f "$RUNFILES_ROOT/$relative" ]]; then
		printf '%s\n' "$RUNFILES_ROOT/$relative"
	elif [[ -f "$relative" ]]; then
		printf '%s\n' "$relative"
	else
		return 1
	fi
}

RULE_FILE=$(resolve_main_file "bazel/semgrep/rules/bazel/verification-genrule-missing-tag.yaml") || {
	echo "ERROR: verification-genrule-missing-tag.yaml not found in runfiles" >&2
	exit 2
}

BUILDIFIER=""
if [[ -n "${VERIFICATION_GUARD_BUILDIFIER:-}" ]]; then
	BUILDIFIER=$(resolve_main_file "$VERIFICATION_GUARD_BUILDIFIER" || true)
fi
if [[ -z "$BUILDIFIER" ]]; then
	BUILDIFIER=$(find "$RUNFILES_ROOT" -name buildifier \( -type f -o -type l \) 2>/dev/null | head -1)
fi
if [[ -z "$BUILDIFIER" ]]; then
	echo "ERROR: buildifier not found in runfiles" >&2
	exit 2
fi

SEMGREP_CORE=""
ENGINE_RUNFILES=()
read -r -a ENGINE_RUNFILES <<<"${VERIFICATION_GUARD_ENGINE_FILES:-}"
for engine_runfile in "${ENGINE_RUNFILES[@]}"; do
	if [[ "${engine_runfile##*/}" == "semgrep-core" ]]; then
		SEMGREP_CORE=$(resolve_main_file "$engine_runfile" || true)
		[[ -n "$SEMGREP_CORE" ]] && break
	fi
done
if [[ -z "$SEMGREP_CORE" ]]; then
	SEMGREP_CORE=$(find "$RUNFILES_ROOT" -name semgrep-core -not -name '*proprietary*' \( -type f -o -type l \) 2>/dev/null | head -1)
fi
if [[ -z "$SEMGREP_CORE" ]]; then
	echo "ERROR: semgrep-core not found in runfiles" >&2
	exit 2
fi
if ! "$SEMGREP_CORE" -version >/dev/null 2>&1; then
	echo "ERROR: semgrep-core cannot execute on $(uname -s)/$(uname -m)" >&2
	exit 2
fi

TMP_ROOT=$(mktemp -d "${TEST_TMPDIR:-/tmp}/verification-genrule-guard.XXXXXX")
trap 'rm -rf "$TMP_ROOT"' EXIT
SCAN_ROOT="$TMP_ROOT/scan"
PARSE_ROOT="$TMP_ROOT/parse"
mkdir -p "$SCAN_ROOT" "$PARSE_ROOT"

BUILD_FILES=()
BUILD_LIST="$TMP_ROOT/build-files"
find "$SOURCE_ROOT" -type f \( -name BUILD -o -name BUILD.bazel \) -print0 | sort -z >"$BUILD_LIST"
while IFS= read -r -d '' file; do
	BUILD_FILES+=("$file")
done <"$BUILD_LIST"

if [[ ${#BUILD_FILES[@]} -eq 0 ]]; then
	echo "ERROR: no BUILD or BUILD.bazel files found under $SOURCE_ROOT" >&2
	exit 2
fi

STAGED_FILES=()
PARSE_FILES=()
for file in "${BUILD_FILES[@]}"; do
	relative="${file#"$SOURCE_ROOT"/}"
	if [[ "$file" == "$SOURCE_ROOT" ]]; then
		relative="${file##*/}"
	fi
	destination="$SCAN_ROOT/$relative"
	parse_destination="$PARSE_ROOT/$relative"
	mkdir -p "${destination%/*}" "${parse_destination%/*}"
	cp "$file" "$destination"
	cp "$file" "$parse_destination"
	STAGED_FILES+=("$destination")
	PARSE_FILES+=("$parse_destination")
done

# Buildifier parses Starlark before Semgrep runs. This makes malformed BUILD
# syntax a hard failure instead of allowing the name heuristic to scan text.
# It formats disposable copies so source attribute order remains testable.
if ! "$BUILDIFIER" -mode=fix "${PARSE_FILES[@]}" >/dev/null; then
	echo "ERROR: buildifier rejected a scanned BUILD file" >&2
	exit 2
fi

RESULTS="$TMP_ROOT/results.json"
STDERR_FILE="$TMP_ROOT/semgrep.stderr"
TARGETS_FILE="$TMP_ROOT/targets.json"

# Directory scans discard extensionless BUILD files even with an explicit
# language. Use core's typed target list so each Starlark file is parsed as
# Python, the grammar Semgrep uses for this Starlark subset.
python3 - "$TARGETS_FILE" "${STAGED_FILES[@]}" <<'PY'
import json
import sys

output = sys.argv[1]
targets = []
for path in sys.argv[2:]:
    targets.append(
        [
            "CodeTarget",
            {
                "path": {"fpath": path, "ppath": path},
                "analyzer": "python",
                "products": ["sast"],
            },
        ]
    )

with open(output, "w", encoding="utf-8") as handle:
    json.dump(["Targets", targets], handle)
PY

scan_exit=0
"$SEMGREP_CORE" \
	-rules "$RULE_FILE" \
	-targets "$TARGETS_FILE" \
	-json \
	-json_nodots \
	>"$RESULTS" 2>"$STDERR_FILE" || scan_exit=$?
if [[ $scan_exit -ne 0 ]]; then
	echo "ERROR: semgrep-core exited $scan_exit" >&2
	cat "$STDERR_FILE" >&2
	exit 2
fi

python3 - "$RESULTS" "${#BUILD_FILES[@]}" <<'PY'
import json
import sys

results_path = sys.argv[1]
file_count = int(sys.argv[2])

try:
    with open(results_path, encoding="utf-8") as handle:
        payload = json.load(handle)
except (OSError, json.JSONDecodeError) as exc:
    print(f"ERROR: invalid semgrep-core JSON: {exc}", file=sys.stderr)
    raise SystemExit(2)

if not isinstance(payload, dict):
    print("ERROR: semgrep-core JSON root is not an object", file=sys.stderr)
    raise SystemExit(2)

required = {"version", "results", "errors", "paths"}
missing = sorted(required - payload.keys())
if missing:
    print(f"ERROR: semgrep-core JSON missing fields: {', '.join(missing)}", file=sys.stderr)
    raise SystemExit(2)

errors = payload["errors"]
if not isinstance(errors, list):
    print("ERROR: semgrep-core errors field is not a list", file=sys.stderr)
    raise SystemExit(2)
if errors:
    for error in errors:
        print(f"ERROR: semgrep-core parse error: {error}", file=sys.stderr)
    raise SystemExit(2)

findings = payload["results"]
if not isinstance(findings, list):
    print("ERROR: semgrep-core results field is not a list", file=sys.stderr)
    raise SystemExit(2)

paths = payload["paths"]
scanned = paths.get("scanned") if isinstance(paths, dict) else None
if not isinstance(scanned, list) or len(set(scanned)) != file_count:
    scanned_count = len(set(scanned)) if isinstance(scanned, list) else 0
    print(
        f"ERROR: semgrep-core scanned {scanned_count} of {file_count} BUILD files",
        file=sys.stderr,
    )
    raise SystemExit(2)

for finding in findings:
    path = finding.get("path", "<unknown>")
    line = finding.get("start", {}).get("line", "?")
    check_id = finding.get("check_id", "verification-genrule-missing-tag")
    message = finding.get("extra", {}).get("message", "missing verification tag")
    print(f"ERROR: {check_id}: {path}:{line}: {message}", file=sys.stderr)

if findings:
    print(
        f"FAILED: {len(findings)} suite-shaped genrule(s) lack verification",
        file=sys.stderr,
    )
    raise SystemExit(1)

print(f"PASSED: scanned {file_count} BUILD file(s), no missing verification tags")
PY
