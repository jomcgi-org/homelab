#!/usr/bin/env bash
# Unit tests for check-migration-timestamp-order.sh PreToolUse hook.
#
# The hook:
#   - Reads JSON from stdin with .tool_input.file_path (and optionally
#     .tool_input.content or .tool_input.new_string — not used by this hook)
#   - Exits 0 always (warning-only, never blocks)
#   - Emits a WARNING on stderr when the new file's timestamp prefix is <=
#     the latest existing migration timestamp in the same directory
#   - Skips non-SQL files, files outside chart/migrations/, files with no
#     timestamp prefix, and directories that don't exist yet

set -euo pipefail

# ---------------------------------------------------------------------------
# Locate hook from Bazel runfiles
# ---------------------------------------------------------------------------
HOOK_REL="bazel/tools/hooks/check-migration-timestamp-order.sh"
HOOK=""
for candidate in \
	"${RUNFILES_DIR:-}/_main/${HOOK_REL}" \
	"${TEST_SRCDIR:-}/_main/${HOOK_REL}" \
	"${BASH_SOURCE[0]%/*}/check-migration-timestamp-order.sh"; do
	if [[ -f "$candidate" ]]; then
		HOOK="$candidate"
		break
	fi
done
if [[ -z "$HOOK" ]]; then
	echo "ERROR: cannot locate check-migration-timestamp-order.sh in runfiles" >&2
	exit 1
fi

# ---------------------------------------------------------------------------
# Install a minimal jq stub so the hook runs in the hermetic sandbox.
# The hook uses:
#   jq -r '.tool_input.file_path // empty'
# ---------------------------------------------------------------------------
mkdir -p "${TEST_TMPDIR}/bin"
cat >"${TEST_TMPDIR}/bin/jq" <<'JQ_STUB'
#!/usr/bin/env python3
"""Minimal jq stub covering expressions used by check-migration-timestamp-order.sh."""
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
# Set up a fake migrations directory with existing files
# ---------------------------------------------------------------------------
MIGRATIONS_DIR="${TEST_TMPDIR}/chart/migrations"
mkdir -p "$MIGRATIONS_DIR"
touch "${MIGRATIONS_DIR}/20240101000000_initial_schema.sql"
touch "${MIGRATIONS_DIR}/20240601120000_add_users_table.sql"
touch "${MIGRATIONS_DIR}/20241231235959_latest_migration.sql"

# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------
PASS=0
FAIL=0

make_json() {
	local fp="$1"
	python3 - "$fp" <<'PY'
import json, sys
fp = sys.argv[1]
print(json.dumps({"tool_input": {"file_path": fp}}))
PY
}

run_test() {
	local name="$1"
	local input_json="$2"
	local want_exit="$3"
	local want_stderr_re="$4"  # regex that must match stderr (empty = no output expected)

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

# 1. New migration with timestamp strictly greater than latest → no warning
run_test "newer_timestamp_no_warning" \
	"$(make_json "${MIGRATIONS_DIR}/20250101000000_add_new_table.sql")" \
	0 ""

# 2. New migration with timestamp equal to latest → warns
run_test "equal_timestamp_warns" \
	"$(make_json "${MIGRATIONS_DIR}/20241231235959_duplicate.sql")" \
	0 "WARNING.*out of order"

# 3. New migration with timestamp older than latest → warns
run_test "older_timestamp_warns" \
	"$(make_json "${MIGRATIONS_DIR}/20240101000001_backdated.sql")" \
	0 "WARNING.*out of order"

# 4. File outside chart/migrations/ → skip
run_test "non_migrations_dir_skipped" \
	"$(make_json "${TEST_TMPDIR}/other/dir/20250101000000_foo.sql")" \
	0 ""

# 5. Non-SQL file inside migrations dir → skip (path does not match *.sql inside chart/migrations)
run_test "non_sql_file_skipped" \
	"$(make_json "${MIGRATIONS_DIR}/README.md")" \
	0 ""

# 6. Migration filename without timestamp prefix → skip
run_test "no_timestamp_prefix_skipped" \
	"$(make_json "${MIGRATIONS_DIR}/create_index.sql")" \
	0 ""

# 7. Empty JSON → skip
run_test "empty_json_allowed" \
	'{}' \
	0 ""

# 8. Migration directory doesn't exist yet → skip (first migration, no comparisons)
NEW_MIGRATIONS_DIR="${TEST_TMPDIR}/new_chart/migrations"
run_test "nonexistent_dir_skipped" \
	"$(make_json "${NEW_MIGRATIONS_DIR}/20250101000000_first.sql")" \
	0 ""

# 9. Idempotent re-write of the latest existing file → no warning (self is excluded)
run_test "rewrite_latest_no_warning" \
	"$(make_json "${MIGRATIONS_DIR}/20241231235959_latest_migration.sql")" \
	0 ""

# 10. Empty migrations directory → no warning (nothing to compare against)
EMPTY_MIGRATIONS_DIR="${TEST_TMPDIR}/empty_chart/migrations"
mkdir -p "$EMPTY_MIGRATIONS_DIR"
run_test "empty_dir_no_warning" \
	"$(make_json "${EMPTY_MIGRATIONS_DIR}/20250101000000_first.sql")" \
	0 ""

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
echo ""
echo "Results: ${PASS} passed, ${FAIL} failed"
if [[ "$FAIL" -gt 0 ]]; then
	exit 1
fi
exit 0
