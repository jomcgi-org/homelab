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
locate_hook check-large-migration-sql.sh
install_jq_stub

SQL_PATH="/workspace/homelab/projects/monolith/chart/migrations/20240101000000_seed.sql"

# Generate a string of exactly N bytes
make_content() {
	local n="$1"
	python3 -c "print('x' * $n, end='')"
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

finish_tests
