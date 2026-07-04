#!/usr/bin/env bash
# Unit tests for check-public-reader-grant.sh PreToolUse hook.
#
# The hook:
#   - Reads JSON from stdin with .tool_name, .tool_input.file_path, and
#     either .tool_input.content (Write) or .tool_input.new_string (Edit)
#   - Only triggers on */projects/monolith/chart/migrations/*.sql paths
#   - Finds "CREATE TABLE [IF NOT EXISTS] <schema>.<table>" targeting a
#     public-served schema
#   - Exits 2 unless the content (or, for Edit, the on-disk file) mentions
#     public_reader, or carries a "-- no-public-reader: <reason>" override
#   - Exits 0 otherwise

set -euo pipefail

# ---------------------------------------------------------------------------
# Locate hook from Bazel runfiles
# ---------------------------------------------------------------------------
HOOK_REL="bazel/tools/hooks/check-public-reader-grant.sh"
HOOK=""
for candidate in \
	"${RUNFILES_DIR:-}/_main/${HOOK_REL}" \
	"${TEST_SRCDIR:-}/_main/${HOOK_REL}" \
	"${BASH_SOURCE[0]%/*}/check-public-reader-grant.sh"; do
	if [[ -f "$candidate" ]]; then
		HOOK="$candidate"
		break
	fi
done
if [[ -z "$HOOK" ]]; then
	echo "ERROR: cannot locate check-public-reader-grant.sh in runfiles" >&2
	exit 1
fi

# ---------------------------------------------------------------------------
# Install a minimal jq stub so the hook runs in the hermetic Bazel sandbox.
# The hook uses simple '.a.b // .c.d // empty' style expressions.
# ---------------------------------------------------------------------------
mkdir -p "${TEST_TMPDIR}/bin"
cat >"${TEST_TMPDIR}/bin/jq" <<'JQ_STUB'
#!/usr/bin/env python3
"""Minimal jq stub covering expressions used by check-public-reader-grant.sh."""
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
    pass  # empty: print nothing
elif raw:
    print(result)
else:
    print(json.dumps(result))
JQ_STUB
chmod +x "${TEST_TMPDIR}/bin/jq"
export PATH="${TEST_TMPDIR}/bin:${PATH}"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
MIGRATIONS_DIR="${TEST_TMPDIR}/repo/projects/monolith/chart/migrations"
mkdir -p "$MIGRATIONS_DIR"

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

PASS=0
FAIL=0

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

# 1. Public-schema CREATE TABLE, no grant anywhere → blocked
run_test "public_schema_create_table_no_grant_blocks" \
	"$(make_write_json "${MIGRATIONS_DIR}/20260704120000_grimoire_foo.sql" \
		'CREATE TABLE grimoire.foo (id int);')" \
	2 "BLOCKED.*public_reader grant"

# 2. Public-schema CREATE TABLE with a GRANT in the same content → passes
run_test "public_schema_create_table_with_grant_passes" \
	"$(make_write_json "${MIGRATIONS_DIR}/20260704120000_grimoire_foo.sql" \
		'CREATE TABLE grimoire.foo (id int);
GRANT SELECT ON grimoire.foo TO public_reader;')" \
	0 ""

# 3. Edit whose new_string lacks the grant, but the on-disk file already has
#    it elsewhere → passes
EDIT_FILE="${MIGRATIONS_DIR}/20260704130000_grimoire_bar.sql"
cat >"$EDIT_FILE" <<'SQL'
CREATE TABLE grimoire.bar (id int);
GRANT SELECT ON grimoire.bar TO public_reader;
SQL
run_test "edit_grant_already_on_disk_passes" \
	"$(make_edit_json "$EDIT_FILE" 'CREATE TABLE grimoire.bar (id int);')" \
	0 ""

# 4. Override comment in the same content → passes
run_test "override_comment_passes" \
	"$(make_write_json "${MIGRATIONS_DIR}/20260704140000_grimoire_baz.sql" \
		'-- no-public-reader: internal audit log, not served publicly
CREATE TABLE grimoire.baz (id int);')" \
	0 ""

# 5. Non-public schema → passes
run_test "non_public_schema_passes" \
	"$(make_write_json "${MIGRATIONS_DIR}/20260704150000_agent_platform_foo.sql" \
		'CREATE TABLE agent_platform.foo (id int);')" \
	0 ""

# 6. Non-migration file → passes
run_test "non_migration_file_passes" \
	"$(make_write_json "${TEST_TMPDIR}/repo/projects/monolith/app/main.py" \
		'CREATE TABLE grimoire.foo (id int);')" \
	0 ""

# 7. Edit with no grant in new_string and no on-disk file (new migration via
#    Edit against a nonexistent path) → blocked
run_test "edit_no_grant_no_disk_file_blocks" \
	"$(make_edit_json "${MIGRATIONS_DIR}/20260704160000_grimoire_qux.sql" \
		'CREATE TABLE grimoire.qux (id int);')" \
	2 "BLOCKED.*public_reader grant"

# 8. CREATE TABLE IF NOT EXISTS variant on a public schema, no grant → blocked
run_test "create_table_if_not_exists_blocks" \
	"$(make_write_json "${MIGRATIONS_DIR}/20260704170000_worldcup_foo.sql" \
		'CREATE TABLE IF NOT EXISTS worldcup.foo (id int);')" \
	2 "BLOCKED.*public_reader grant"

# 9. Empty content → passes (nothing to check)
run_test "empty_content_passes" \
	'{"tool_name":"Write","tool_input":{"file_path":"'"${MIGRATIONS_DIR}"'/20260704180000_empty.sql"}}' \
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
