#!/bin/bash
# PreToolUse hook: warn when writing a migration SQL file whose timestamp
# prefix is <= the latest existing migration in the same directory.
#
# SQL migrations must be applied in ascending timestamp order. Writing a
# migration with a timestamp <= the latest existing one risks applying
# migrations out of order, which can silently corrupt schema state or cause
# database errors on deployment.
#
# Fires on Write|Edit when the target path matches **/chart/migrations/*.sql.
#
# Input: JSON on stdin from Claude Code hook system
# Exit 0: always (warning only — never blocks)
# Exit 2: block (not used — advisory only)

set -euo pipefail

INPUT=$(cat)

FILE_PATH=$(echo "$INPUT" | jq -r '.tool_input.file_path // empty')
if [[ -z "$FILE_PATH" ]]; then
	exit 0
fi

# Only trigger on SQL files inside a chart/migrations/ directory
if [[ "$FILE_PATH" != */chart/migrations/*.sql ]]; then
	exit 0
fi

BASENAME=$(basename "$FILE_PATH")
MIGRATIONS_DIR=$(dirname "$FILE_PATH")

# Extract leading numeric timestamp prefix (e.g. "20240101120000" from
# "20240101120000_create_users.sql"). No prefix → nothing to check.
NEW_TS=$(echo "$BASENAME" | grep -oE '^[0-9]+' || true)
if [[ -z "$NEW_TS" ]]; then
	exit 0
fi

# If the migrations directory doesn't exist yet there's nothing to compare.
if [[ ! -d "$MIGRATIONS_DIR" ]]; then
	exit 0
fi

# Walk existing SQL files and find the highest timestamp prefix.
LATEST_TS=""
LATEST_FILE=""
for f in "$MIGRATIONS_DIR"/*.sql; do
	[[ -f "$f" ]] || continue
	# Skip the file being written so an idempotent re-write doesn't self-warn.
	[[ "$(basename "$f")" == "$BASENAME" ]] && continue
	ts=$(basename "$f" | grep -oE '^[0-9]+' || true)
	[[ -z "$ts" ]] && continue
	# Lexicographic comparison is correct here: all timestamps should share
	# the same digit count, and leading-zero padding keeps them sortable.
	if [[ "$ts" > "$LATEST_TS" ]]; then
		LATEST_TS="$ts"
		LATEST_FILE=$(basename "$f")
	fi
done

if [[ -z "$LATEST_TS" ]]; then
	exit 0 # No existing timestamped migrations to compare against.
fi

# Warn if the new timestamp does not strictly follow the existing latest.
if [[ "$NEW_TS" -le "$LATEST_TS" ]]; then
	cat >&2 <<-EOF
		WARNING: Migration timestamp out of order.

		New migration:    $BASENAME  (timestamp: $NEW_TS)
		Latest existing:  $LATEST_FILE  (timestamp: $LATEST_TS)

		Migrations in $(basename "$MIGRATIONS_DIR")/ are applied in ascending
		timestamp order. A timestamp <= the latest existing migration will
		be applied before or at the same position as $LATEST_FILE, which
		can corrupt schema state or cause errors on deployment.

		Use a timestamp strictly greater than $LATEST_TS (e.g. the current
		UTC datetime: $(date -u +%Y%m%d%H%M%S 2>/dev/null || echo "run: date -u +%Y%m%d%H%M%S")).
	EOF
fi

exit 0
