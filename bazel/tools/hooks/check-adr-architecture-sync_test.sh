#!/usr/bin/env bash
# Unit tests for check-adr-architecture-sync.sh PreToolUse hook.
#
# The hook:
#   - Reads JSON from stdin with .tool_input.file_path
#   - Always exits 0 (advisory only)
#   - Emits a REMINDER on stderr when the path is under a covered ADR
#     category (docs/decisions/embervm/), naming the architecture doc
#   - Stays silent for every other path
#
# jq is mocked via a minimal Python3 stub placed earlier on PATH so the
# hook runs in the hermetic Bazel sandbox.

set -euo pipefail

HOOK_REL="bazel/tools/hooks/check-adr-architecture-sync.sh"
HOOK=""
for candidate in \
	"${RUNFILES_DIR:-}/_main/${HOOK_REL}" \
	"${TEST_SRCDIR:-}/_main/${HOOK_REL}" \
	"${BASH_SOURCE[0]%/*}/check-adr-architecture-sync.sh"; do
	if [[ -f "$candidate" ]]; then
		HOOK="$candidate"
		break
	fi
done
if [[ -z "$HOOK" ]]; then
	echo "ERROR: cannot locate check-adr-architecture-sync.sh in runfiles" >&2
	exit 1
fi

mkdir -p "${TEST_TMPDIR}/bin"
cat >"${TEST_TMPDIR}/bin/jq" <<'JQ_STUB'
#!/usr/bin/env python3
"""Minimal jq stub covering: jq -r '.tool_input.file_path // empty'"""
import json, sys

args = sys.argv[1:]
if args and args[0] == "-r":
    args = args[1:]

data = json.load(sys.stdin)
val = data.get("tool_input", {}).get("file_path") or ""
print(val)
JQ_STUB
chmod +x "${TEST_TMPDIR}/bin/jq"
export PATH="${TEST_TMPDIR}/bin:${PATH}"

FAILURES=0

# run_hook <file_path> -> sets STDERR_OUT and EXIT_CODE
run_hook() {
	local file_path="$1"
	local input
	input=$(printf '{"tool_name": "Edit", "tool_input": {"file_path": "%s"}}' "$file_path")
	set +e
	STDERR_OUT=$(printf '%s' "$input" | "$HOOK" 2>&1 >/dev/null)
	EXIT_CODE=$?
	set -e
}

expect_reminder() {
	local file_path="$1"
	run_hook "$file_path"
	if [[ "$EXIT_CODE" -ne 0 ]]; then
		echo "FAIL: expected exit 0 for $file_path, got $EXIT_CODE" >&2
		FAILURES=$((FAILURES + 1))
	elif [[ "$STDERR_OUT" != *"REMINDER"* ]] || [[ "$STDERR_OUT" != *"projects/embervm/ARCHITECTURE.md"* ]]; then
		echo "FAIL: expected REMINDER naming the architecture doc for $file_path, got: $STDERR_OUT" >&2
		FAILURES=$((FAILURES + 1))
	else
		echo "PASS: reminder for $file_path"
	fi
}

expect_silent() {
	local file_path="$1"
	run_hook "$file_path"
	if [[ "$EXIT_CODE" -ne 0 ]]; then
		echo "FAIL: expected exit 0 for $file_path, got $EXIT_CODE" >&2
		FAILURES=$((FAILURES + 1))
	elif [[ -n "$STDERR_OUT" ]]; then
		echo "FAIL: expected no output for $file_path, got: $STDERR_OUT" >&2
		FAILURES=$((FAILURES + 1))
	else
		echo "PASS: silent for $file_path"
	fi
}

# Covered category: any write under docs/decisions/embervm/ reminds.
expect_reminder "/repo/docs/decisions/embervm/027-snapshot-modes-workload-property.md"
expect_reminder "/repo/docs/decisions/embervm/README.md"

# Uncovered categories and unrelated paths stay silent.
expect_silent "/repo/docs/decisions/agents/046-mmds-dynamic-workload-env.md"
expect_silent "/repo/projects/embervm/ARCHITECTURE.md"
expect_silent "/repo/projects/embervm/README.md"
expect_silent "/repo/main.go"

# Missing file_path (e.g. a differently-shaped tool input) stays silent.
set +e
STDERR_OUT=$(printf '{"tool_name": "Edit", "tool_input": {}}' | "$HOOK" 2>&1 >/dev/null)
EXIT_CODE=$?
set -e
if [[ "$EXIT_CODE" -ne 0 ]] || [[ -n "$STDERR_OUT" ]]; then
	echo "FAIL: expected silent exit 0 for missing file_path" >&2
	FAILURES=$((FAILURES + 1))
else
	echo "PASS: silent for missing file_path"
fi

if [[ "$FAILURES" -gt 0 ]]; then
	echo "${FAILURES} test(s) failed" >&2
	exit 1
fi
echo "All tests passed"
