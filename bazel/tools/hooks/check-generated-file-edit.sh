#!/bin/bash
# PreToolUse hook for Write|Edit operations.
# Warns when editing a file that contains a "DO NOT EDIT" header in its first 5 lines.
#
# Input: JSON on stdin from Claude Code hook system
# Exit 0: always (advisory only, never blocks)

set -euo pipefail

INPUT=$(cat)
FILE_PATH=$(echo "$INPUT" | jq -r '.tool_input.file_path // empty')

# No file path — nothing to check
if [[ -z "$FILE_PATH" ]]; then
	exit 0
fi

# File does not exist yet (new file) — nothing to check
if [[ ! -f "$FILE_PATH" ]]; then
	exit 0
fi

# Read the first 5 lines and check for "DO NOT EDIT" (case-insensitive)
if head -n 5 "$FILE_PATH" | grep -qi "do not edit"; then
	cat >&2 <<-EOF
		WARNING: This file appears to be generated (contains DO NOT EDIT header).
		Edit the source spec or template instead, then regenerate.
		If you must edit this file directly, proceed with caution.
		File: $FILE_PATH
	EOF
fi

exit 0
