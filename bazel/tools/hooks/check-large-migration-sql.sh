#!/bin/bash
# PreToolUse hook: warn when writing a large SQL file into chart/migrations/.
# Advisory only -- exit 0 always.
#
# SQL files in chart/migrations/ are Helm-globbed into the monolith-migrations
# ConfigMap, which ArgoCD applies client-side. The entire ConfigMap (including
# the last-applied-configuration annotation) is hard-capped at 256 KiB. A
# large migration silently breaks ArgoCD sync with:
#   metadata.annotations: Too long
#
# Keep seeds and bulk data out of migrations -- load them out-of-band instead.
# See projects/monolith/hikes/seed/ for the established pattern.
#
# Fires on Write when the target path matches */chart/migrations/*.sql.
# Edit events are skipped because new_string is a partial diff and its byte
# count does not represent the final file size.
#
# Input: JSON on stdin from Claude Code hook system
# Exit 0: allow (warnings emitted on stderr -- advisory only)
# Exit 2: block (not used)

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

# Only check Write operations -- full file content is available
TOOL_NAME=$(echo "$INPUT" | jq -r '.tool_name // empty')
if [[ "$TOOL_NAME" != "Write" ]]; then
	exit 0
fi

CONTENT=$(echo "$INPUT" | jq -r '.tool_input.content // empty')
if [[ -z "$CONTENT" ]]; then
	exit 0
fi

# Measure byte length of the content being written
BYTE_LEN=$(printf '%s' "$CONTENT" | wc -c)

LIMIT=50000

if [[ "$BYTE_LEN" -gt "$LIMIT" ]]; then
	FILENAME=$(basename "$FILE_PATH")
	cat >&2 <<-EOF
		WARNING: Large SQL file in chart/migrations/ may break ArgoCD sync.

		File:  $FILENAME
		Size:  ${BYTE_LEN} bytes  (advisory limit: ${LIMIT} bytes)

		SQL files in chart/migrations/ are Helm-globbed into the monolith-migrations
		ConfigMap. ArgoCD applies it client-side and stores the whole object in the
		last-applied-configuration annotation, which is hard-capped at 256 KiB.
		A large migration silently breaks sync with:
		  metadata.annotations: Too long

		For bulk data (seeds, large reference tables), load out-of-band instead.
		See projects/monolith/hikes/seed/ for the established pattern.
	EOF
fi

exit 0
