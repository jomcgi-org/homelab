#!/bin/bash
# PreToolUse hook: warn when an UPPER_SNAKE_CASE numeric constant assignment
# is being changed to a different numeric value in a Python file.
#
# When a constant like `_RESEARCH_INTERVAL_SECS = 300` changes to a new value,
# test files may be asserting on the old value. This hook emits an advisory
# prompt to grep test files before finalising the change.
#
# Input: JSON on stdin from Claude Code hook system
# Exit 0: allow (warnings emitted on stderr — advisory only)
# Exit 2: block (not used)

set -euo pipefail

INPUT=$(cat)

# Only check Python files
FILE_PATH=$(echo "$INPUT" | jq -r '.tool_input.file_path // empty')
if [[ -z "$FILE_PATH" ]]; then
	exit 0
fi
if [[ "$FILE_PATH" != *.py ]]; then
	exit 0
fi

TOOL_NAME=$(echo "$INPUT" | jq -r '.tool_name // empty')

# Get old and new content depending on the tool in use.
# Edit: old_string is the text being replaced, new_string is the replacement.
# Write: content is the full new file; compare against what's currently on disk.
if [[ "$TOOL_NAME" == "Edit" ]]; then
	OLD_CONTENT=$(echo "$INPUT" | jq -r '.tool_input.old_string // empty')
	NEW_CONTENT=$(echo "$INPUT" | jq -r '.tool_input.new_string // empty')
else
	# Write tool
	NEW_CONTENT=$(echo "$INPUT" | jq -r '.tool_input.content // empty')
	if [[ -f "$FILE_PATH" ]]; then
		OLD_CONTENT=$(cat "$FILE_PATH")
	else
		OLD_CONTENT=""
	fi
fi

if [[ -z "$OLD_CONTENT" ]] || [[ -z "$NEW_CONTENT" ]]; then
	exit 0
fi

# Extract UPPER_SNAKE_CASE = <integer> assignments.
# Matches names like TIMEOUT, _INTERVAL_SECS, MAX_RETRIES, etc.
# Output format: one "NAME VALUE" pair per line.
extract_constants() {
	printf '%s\n' "$1" |
		grep -oE '[_A-Z][A-Z0-9_]* = [0-9]+' |
		sed 's/ = / /' ||
		true
}

OLD_PAIRS=$(extract_constants "$OLD_CONTENT")
NEW_PAIRS=$(extract_constants "$NEW_CONTENT")

if [[ -z "$OLD_PAIRS" ]] || [[ -z "$NEW_PAIRS" ]]; then
	exit 0
fi

# For each constant found in the old content, check whether the new content
# contains the same name with a different numeric value.
while IFS=' ' read -r name old_val; do
	[[ -z "$name" ]] && continue
	new_val=$(printf '%s\n' "$NEW_PAIRS" | awk -v n="$name" '$1==n{print $2; exit}')
	if [[ -n "$new_val" ]] && [[ "$new_val" != "$old_val" ]]; then
		cat >&2 <<-EOF
			WARNING: You changed constant ${name} from ${old_val} to ${new_val}.
			Run grep -r ${old_val} --include="*_test.py" to check if any tests assert on the old value.

			File: $FILE_PATH
		EOF
	fi
done <<<"$OLD_PAIRS"

exit 0
