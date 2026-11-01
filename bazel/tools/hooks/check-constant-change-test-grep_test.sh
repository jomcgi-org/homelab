#!/usr/bin/env bash
# Unit tests for check-constant-change-test-grep.sh PreToolUse hook.
#
# The hook:
#   - Reads JSON from stdin with .tool_name, .tool_input.file_path, and either
#     .tool_input.old_string + .tool_input.new_string (Edit) or
#     .tool_input.content (Write, compared against the file on disk)
#   - Exits 0 always (warning-only, never blocks)
#   - Emits a WARNING on stderr when an UPPER_SNAKE_CASE constant's numeric
#     value changes between old and new content
#   - Skips non-Python files, unchanged values, and new files (no old content)

set -euo pipefail

# ---------------------------------------------------------------------------
# Locate the shared test helpers and the hook from Bazel runfiles
# ---------------------------------------------------------------------------
TESTLIB=""
for candidate in \
	"${RUNFILES_DIR:-}/_main/bazel/tools/hooks/testlib.sh" \
	"${TEST_SRCDIR:-}/_main/bazel/tools/hooks/testlib.sh" \
	"${BASH_SOURCE[0]%/*}/testlib.sh"; do
	if [[ -f "$candidate" ]]; then
		TESTLIB="$candidate"
		break
	fi
done
# shellcheck source=bazel/tools/hooks/testlib.sh
source "${TESTLIB:?cannot locate testlib.sh in runfiles}"
locate_hook check-constant-change-test-grep.sh
install_jq_stub

# ---------------------------------------------------------------------------
# Test cases
# ---------------------------------------------------------------------------

PY_FILE="${TEST_TMPDIR}/agent.py"

# 1. Edit: constant value changes → warns with old and new values
run_test "edit_constant_changed_warns" \
	"$(make_edit_json "$PY_FILE" \
		'_RESEARCH_INTERVAL_SECS = 300' \
		'_RESEARCH_INTERVAL_SECS = 600')" \
	0 "WARNING.*_RESEARCH_INTERVAL_SECS.*300.*600"

# 2. Edit: constant value unchanged → no warning
run_test "edit_constant_unchanged_no_warning" \
	"$(make_edit_json "$PY_FILE" \
		'_RESEARCH_INTERVAL_SECS = 300  # seconds' \
		'_RESEARCH_INTERVAL_SECS = 300  # polling interval')" \
	0 ""

# 3. Edit: non-Python file → skip (no warning)
run_test "edit_non_python_skipped" \
	"$(make_edit_json "${TEST_TMPDIR}/config.yaml" \
		'MAX_RETRIES = 3' \
		'MAX_RETRIES = 5')" \
	0 ""

# 4. Edit: constant with leading underscore changes → warns
run_test "edit_leading_underscore_warns" \
	"$(make_edit_json "$PY_FILE" \
		'_TIMEOUT_SECS = 30' \
		'_TIMEOUT_SECS = 120')" \
	0 "WARNING.*_TIMEOUT_SECS.*30.*120"

# 5. Edit: multiple constants, only one changes → warns only for the changed one
OLD_MULTI='MAX_RETRIES = 3
_INTERVAL = 60
BATCH_SIZE = 100'
NEW_MULTI='MAX_RETRIES = 3
_INTERVAL = 120
BATCH_SIZE = 100'
WARN_OUT=$(make_edit_json "$PY_FILE" "$OLD_MULTI" "$NEW_MULTI" |
	bash "$HOOK" 2>&1 >/dev/null || true)
if echo "$WARN_OUT" | grep -qE "WARNING.*_INTERVAL.*60.*120" &&
	! echo "$WARN_OUT" | grep -qE "MAX_RETRIES|BATCH_SIZE"; then
	echo "PASS [edit_multi_only_changed_warns]"
	PASS=$((PASS + 1))
else
	echo "FAIL [edit_multi_only_changed_warns]: stderr=$(printf '%q' "$WARN_OUT")"
	FAIL=$((FAIL + 1))
fi

# 6. Edit: no constants in change → no warning
run_test "edit_no_constants_no_warning" \
	"$(make_edit_json "$PY_FILE" \
		'def fetch_data():' \
		'async def fetch_data():')" \
	0 ""

# 7. Write: existing file has constant, new content changes it → warns
EXISTING_PY="${TEST_TMPDIR}/existing.py"
printf '_POLL_INTERVAL = 30\n' >"$EXISTING_PY"
run_test "write_existing_file_constant_changed_warns" \
	"$(make_write_json "$EXISTING_PY" '_POLL_INTERVAL = 90')" \
	0 "WARNING.*_POLL_INTERVAL.*30.*90"

# 8. Write: new file (does not exist on disk) → no warning
NEW_PY="${TEST_TMPDIR}/brand_new.py"
run_test "write_new_file_no_warning" \
	"$(make_write_json "$NEW_PY" 'MAX_SIZE = 1024')" \
	0 ""

# 9. Write: existing file, value unchanged → no warning
SAME_PY="${TEST_TMPDIR}/same.py"
printf 'TIMEOUT = 60\n' >"$SAME_PY"
run_test "write_unchanged_value_no_warning" \
	"$(make_write_json "$SAME_PY" 'TIMEOUT = 60')" \
	0 ""

# 10. Empty JSON → skip
run_test "empty_json_allowed" \
	'{}' \
	0 ""

# 11. Warning message includes grep command with old value
GREP_OUT=$(make_edit_json "$PY_FILE" \
	'_MAX_TOKENS = 4096' \
	'_MAX_TOKENS = 8192' |
	bash "$HOOK" 2>&1 >/dev/null || true)
if echo "$GREP_OUT" | grep -qE 'grep.*4096.*\*_test\.py'; then
	echo "PASS [warning_includes_grep_command]"
	PASS=$((PASS + 1))
else
	echo "FAIL [warning_includes_grep_command]: stderr=$(printf '%q' "$GREP_OUT")"
	FAIL=$((FAIL + 1))
fi

finish_tests
