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
locate_hook check-public-reader-grant.sh
install_jq_stub

MIGRATIONS_DIR="${TEST_TMPDIR}/repo/projects/monolith/chart/migrations"
mkdir -p "$MIGRATIONS_DIR"

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

finish_tests
