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
# Locate hook from Bazel runfiles
# ---------------------------------------------------------------------------
HOOK_REL="bazel/tools/hooks/check-constant-change-test-grep.sh"
HOOK=""
for candidate in \
	"${RUNFILES_DIR:-}/_main/${HOOK_REL}" \
	"${TEST_SRCDIR:-}/_main/${HOOK_REL}" \
	"${BASH_SOURCE[0]%/*}/check-constant-change-test-grep.sh"; do
	if [[ -f "$candidate" ]]; then
		HOOK="$candidate"
		break
	fi
done
if [[ -z "$HOOK" ]]; then
	echo "ERROR: cannot locate check-constant-change-test-grep.sh in runfiles" >&2
	exit 1
fi

# ---------------------------------------------------------------------------
# Install a minimal jq stub so the hook runs in the hermetic Bazel sandbox.
# The hook uses:
#   jq -r '.tool_input.file_path // empty'
#   jq -r '.tool_name // empty'
#   jq -r '.tool_input.old_string // empty'
#   jq -r '.tool_input.new_string // empty'
#   jq -r '.tool_input.content // empty'
# ---------------------------------------------------------------------------
mkdir -p "${TEST_TMPDIR}/bin"
cat >"${TEST_TMPDIR}/bin/jq" <<'JQ_STUB'
#!/usr/bin/env python3
"""Minimal jq stub covering expressions used by check-constant-change-test-grep.sh."""
import json, sys

args = sys.argv[1:]
raw = False
if args and args[0] == "-r":
    raw = True
    args = args[1:]

expr = args[0] if args else "."
data = json.load(sys.stdin)

def jq_eval(obj, expr):
    """Evaluate '.a.b // .c.d // empty' style expressions."""
    for alt in expr.split("//"):
        alt = alt.strip()
        if alt == "empty":
            return None
        keys = [k for k in alt.lstrip(".").split(".") if k]
        val = obj
        try:
            for k in keys:
                val = val[k] if isinstance(val, dict) else None
                if val is None:
                    break
        except (KeyError, TypeError):
            val = None
        if val is not None:
            return val
    return None

result = jq_eval(data, expr)
if result is None:
    pass  # empty — print nothing
elif raw:
    print(result)
else:
    print(json.dumps(result))
JQ_STUB
chmod +x "${TEST_TMPDIR}/bin/jq"
export PATH="${TEST_TMPDIR}/bin:${PATH}"

# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------
PASS=0
FAIL=0

# Build JSON for an Edit tool call
make_edit_json() {
	local fp="$1" old_str="$2" new_str="$3"
	python3 - "$fp" "$old_str" "$new_str" <<'PY'
import json, sys
fp, old_str, new_str = sys.argv[1], sys.argv[2], sys.argv[3]
print(json.dumps({
    "tool_name": "Edit",
    "tool_input": {"file_path": fp, "old_string": old_str, "new_string": new_str}
}))
PY
}

# Build JSON for a Write tool call
make_write_json() {
	local fp="$1" content="$2"
	python3 - "$fp" "$content" <<'PY'
import json, sys
fp, content = sys.argv[1], sys.argv[2]
print(json.dumps({
    "tool_name": "Write",
    "tool_input": {"file_path": fp, "content": content}
}))
PY
}

run_test() {
	local name="$1"
	local input_json="$2"
	local want_exit="$3"
	local want_stderr_re="$4" # regex that must match stderr (empty = no output expected)

	local stderr_out
	local got_exit=0
	stderr_out=$(printf '%s' "$input_json" | bash "$HOOK" 2>&1 >/dev/null) || got_exit=$?

	local ok=true

	if [[ "$got_exit" -ne "$want_exit" ]]; then
		echo "FAIL [$name]: exit $got_exit, want $want_exit"
		ok=false
	fi

	if [[ -n "$want_stderr_re" ]]; then
		if ! echo "$stderr_out" | grep -qE "$want_stderr_re"; then
			echo "FAIL [$name]: stderr $(printf '%q' "$stderr_out") did not match /$want_stderr_re/"
			ok=false
		fi
	else
		if [[ -n "$stderr_out" ]]; then
			echo "FAIL [$name]: unexpected stderr: $(printf '%q' "$stderr_out")"
			ok=false
		fi
	fi

	if $ok; then
		echo "PASS [$name]"
		PASS=$((PASS + 1))
	else
		FAIL=$((FAIL + 1))
	fi
}

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

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
echo ""
echo "Results: ${PASS} passed, ${FAIL} failed"
if [[ "$FAIL" -gt 0 ]]; then
	exit 1
fi
exit 0
