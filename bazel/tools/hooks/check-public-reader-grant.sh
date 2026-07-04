#!/bin/bash
# PreToolUse hook: block a CREATE TABLE in a public-served schema that ships
# without a public_reader grant in the same migration.
#
# The jomcgi.dev public tier reads through the public_reader Postgres role.
# A table created in a public-served schema without a
#   GRANT SELECT ON <schema>.<table> TO public_reader;
# (or an existing schema-wide default-privilege grant) is unreachable from
# the public tier: the route 503s with a generic "permission denied" that
# looks like any other 5xx in dashboards and is normally only caught by a
# prod curl after deploy. This hook forces the grant (or an explicit
# intentional-private note) to be visible in the same diff.
#
# Only fires on projects/monolith/chart/migrations/*.sql.
#
# Input: JSON on stdin from Claude Code hook system
# Exit 0: allow
# Exit 2: block (public-schema table with no public_reader mention and no
#         override comment)

set -euo pipefail

# Public-served schemas, derived by grepping existing migrations for
# "TO public_reader" grants:
#   grep -rhoE 'SCHEMA [a-z_]+|ON [a-z_]+\.[a-z_]+ TO public_reader' \
#     projects/monolith/chart/migrations/*.sql
#
# To extend: once a new schema gets its own "GRANT ... TO public_reader"
# migration (schema-wide or per-table), add the schema name here.
PUBLIC_SCHEMAS=(
	campsites
	chat_public
	dr_jobs
	grimoire
	hikes
	observability
	public_api
	ships
	stars
	trips
	worldcup
)

INPUT=$(cat)

FILE_PATH=$(echo "$INPUT" | jq -r '.tool_input.file_path // empty')
if [[ -z "$FILE_PATH" ]]; then
	exit 0
fi

if [[ "$FILE_PATH" != */projects/monolith/chart/migrations/*.sql ]]; then
	exit 0
fi

TOOL_NAME=$(echo "$INPUT" | jq -r '.tool_name // empty')
if [[ "$TOOL_NAME" == "Edit" ]]; then
	CONTENT=$(echo "$INPUT" | jq -r '.tool_input.new_string // empty')
else
	CONTENT=$(echo "$INPUT" | jq -r '.tool_input.content // empty')
fi

if [[ -z "$CONTENT" ]]; then
	exit 0
fi

# Find every "CREATE TABLE [IF NOT EXISTS] <schema>.<table>" in the content
# and keep only the ones targeting a public-served schema.
OFFENDING_TABLES=""
while IFS= read -r match; do
	[[ -z "$match" ]] && continue
	qualified=$(echo "$match" | grep -oiE '[a-z_]+\.[a-z_]+$')
	schema=$(echo "${qualified%%.*}" | tr '[:upper:]' '[:lower:]')
	for public_schema in "${PUBLIC_SCHEMAS[@]}"; do
		if [[ "$schema" == "$public_schema" ]]; then
			OFFENDING_TABLES="${OFFENDING_TABLES}${qualified}"$'\n'
			break
		fi
	done
done < <(printf '%s\n' "$CONTENT" | grep -oiE 'CREATE[[:space:]]+TABLE[[:space:]]+(IF[[:space:]]+NOT[[:space:]]+EXISTS[[:space:]]+)?[a-z_]+\.[a-z_]+' || true)

if [[ -z "$OFFENDING_TABLES" ]]; then
	exit 0
fi

# The grant can be satisfied by this diff, by an override comment in this
# diff, or (Edit only) by either already being present on disk.
HAS_SIGNAL=false
if printf '%s' "$CONTENT" | grep -qi 'public_reader'; then
	HAS_SIGNAL=true
fi
if printf '%s' "$CONTENT" | grep -qiE -- '--[[:space:]]*no-public-reader:'; then
	HAS_SIGNAL=true
fi
if [[ "$HAS_SIGNAL" == false ]] && [[ "$TOOL_NAME" == "Edit" ]] && [[ -f "$FILE_PATH" ]]; then
	if grep -qi 'public_reader' "$FILE_PATH"; then
		HAS_SIGNAL=true
	fi
	if grep -qiE -- '--[[:space:]]*no-public-reader:' "$FILE_PATH"; then
		HAS_SIGNAL=true
	fi
fi

if [[ "$HAS_SIGNAL" == true ]]; then
	exit 0
fi

cat >&2 <<-EOF
	BLOCKED: New table in a public-served schema has no public_reader grant.

	File: $FILE_PATH
	Table(s):
	$(printf '%s' "$OFFENDING_TABLES" | sed 's/^/  /')

	Tables created in a public-served schema need a
	  GRANT SELECT ON <schema>.<table> TO public_reader;
	in the same migration. Without it, the jomcgi.dev public tier gets
	"permission denied" reading the table, which surfaces as a generic 503
	(normally only caught by curling prod after deploy, not by CI).

	See docs/runbooks/public-tier-checklist.md for the full checklist.

	Fix by either:
	  1. Adding a GRANT SELECT ... TO public_reader; for this table in this
	     migration (or confirming an existing schema-wide default-privilege
	     grant already covers it, and saying so in a comment).
	  2. If the table is intentionally private, add an override comment:
	       -- no-public-reader: <reason>
EOF

exit 2
