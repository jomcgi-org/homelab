#!/usr/bin/env bash
# Unit tests for check-large-migration-sql.sh PreToolUse hook.
#
# The hook:
#   - Only fires on Write operations (not Edit)
#   - Only fires on files matching */chart/migrations/*.sql
#   - Warns when the byte length of content exceeds 50000 bytes
#   - Exits 0 always (advisory only)

set -euo pipefail

# ---------------------------------------------------------------------------
# Locate hook from Bazel runfiles
# ---------------------------------------------------------------------------
HOOK_REL="bazel/tools/hooks/check-large-migration-sql.sh"
HOOK=""
for candidate in \
	"${RUNFILES_DIR:-}/_main/${HOOK_REL}" \
	"${TEST_SRCDIR:-}/_main/${HOOK_REL}" \
	"${BASH_SOURCE[0]%/*}/check-large-migration-sql.sh"; do
	if [[ -f "$candidate" ]]; then
		HOOK="$candidate"
		break
	fi
done
if [[ -z "$HOOK" ]]; then
	echo "ERROR: cannot locate check-large-migration-sql.sh in runfiles" >&2
	exit 1
fi

# ---------------------------------------------------------------------------
# jq stub (hermetic Bazel sandbox has no jq)
# ---------------------------------------------------------------------------
mkdir -p "${TEST_TMPDIR}/bin"
cat >"${TEST_TMPDIR}/bin/jq" <<'JQ_STUB'
#!/usr/bin/env python3
"""Minimal jq stub for check-large-migration-sql.sh."""
import json, sys

args = sys.argv[1:]
raw = False
if args and args[0] == "-r":
    raw = True
    args = args[1:]

expr = args[0] if args else "."
data = json.load(sys.stdin)

def jq_eval(obj, expr):
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
    pass
elif raw:
    print(result)
else:
    print(json.dumps(result))
JQ_STUB
chmod +x "${TEST_TMPDIR}/bin/jq"
export PATH="${TEST_TMPDIR}/bin:${PATH}"

SQL_PATH="/workspace/homelab/projects/monolith/chart/migrations/20240101000000_seed.sql"

# ---------------------------------------------------------------------------
# JSON helpers
# ---------------------------------------------------------------------------
make_write_json() {
	local fp="$1" content="$2"
	python3 - "$fp" "$content" <<'PY'
import json, sys
fp, content = sys.argv[1], sys.argv[2]
print(json.dumps({"tool_name": "Write", "tool_input": {"file_path": fp, "content": content}}))
PY
}

make_edit_json() {
	local fp="$1" new_str="$2"
	python3 - "$fp" "$new_str" <<'PY'
import json, sys
fp, new_str = sys.argv[1], sys.argv[2]
print(json.dumps({"tool_name": "Edit", "tool_input": {"file_path": fp, "new_string": new_str}}))
PY
}

# Generate a string of exactly N bytes
make_content() {
	local n="$1"
	python3 -c "print('x' * $n, end='')"
}

# ---------------------------------------------------------------------------
# Test runner
# ---------------------------------------------------------------------------
PASS=0
FAIL=0

run_test() {
	local name="$1" input_json="$2" want_exit="$3" want_stderr_re="$4"

	local stderr_out got_exit=0
	stderr_out=$(printf '%s' "$input_json" | bash "$HOOK" 2>&1 >/dev/null) || got_exit=$?

	local ok=true

	if [[ "$got_exit" -ne "$want_exit" ]]; then
		echo "FAIL [$name]: exit $got_exit, want $want_exit"
		ok=false
	fi

	if [[ -n "$want_stderr_re" ]]; then
		if ! printf '%s\n' "$stderr_out" | grep -qE "$want_stderr_re"; then
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

# 1. Small file (under limit) -- no warning
run_test "small_file_silent" \
	"$(make_write_json "$SQL_PATH" "$(make_content 1000)")" \
	0 ""

# 2. File exactly at limit (50000 bytes) -- no warning
run_test "file_at_limit_silent" \
	"$(make_write_json "$SQL_PATH" "$(make_content 50000)")" \
	0 ""

# 3. File over limit (50001 bytes) -- warns
run_test "file_over_limit_warns" \
	"$(make_write_json "$SQL_PATH" "$(make_content 50001)")" \
	0 "WARNING"

# 4. Large file warning mentions filename
run_test "warning_mentions_filename" \
	"$(make_write_json "$SQL_PATH" "$(make_content 60000)")" \
	0 "20240101000000_seed\.sql"

# 5. Large file warning mentions byte count
run_test "warning_mentions_byte_count" \
	"$(make_write_json "$SQL_PATH" "$(make_content 60000)")" \
	0 "60000"

# 6. Large file warning mentions out-of-band pattern
run_test "warning_mentions_seed_pattern" \
	"$(make_write_json "$SQL_PATH" "$(make_content 60000)")" \
	0 "hikes/seed"

# 7. Edit tool (not Write) -- skip even if content is large
run_test "edit_tool_skipped" \
	"$(make_edit_json "$SQL_PATH" "$(make_content 60000)")" \
	0 ""

# 8. Non-SQL file path -- skip
run_test "non_sql_path_skipped" \
	"$(make_write_json "/workspace/homelab/projects/monolith/chart/migrations/README.md" "$(make_content 60000)")" \
	0 ""

# 9. SQL file outside chart/migrations/ -- skip
run_test "sql_outside_migrations_skipped" \
	"$(make_write_json "/workspace/homelab/projects/monolith/app/query.sql" "$(make_content 60000)")" \
	0 ""

# 10. Missing file_path -- skip
run_test "missing_file_path_skipped" \
	'{"tool_name":"Write","tool_input":{"content":"SELECT 1"}}' \
	0 ""

# 11. Empty JSON -- skip
run_test "empty_json_skipped" \
	'{}' \
	0 ""

# 12. Large file warning mentions ConfigMap annotation limit
run_test "warning_mentions_configmap_limit" \
	"$(make_write_json "$SQL_PATH" "$(make_content 60000)")" \
	0 "256 KiB"

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
echo ""
echo "Results: ${PASS} passed, ${FAIL} failed"
if [[ "$FAIL" -gt 0 ]]; then
	exit 1
fi
exit 0
