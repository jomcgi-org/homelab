#!/bin/bash
# PreToolUse hook: warn when content being written or edited contains an em-dash
# (U+2014).
#
# The CLAUDE.md Writing Style section forbids em-dashes in new content. Use a
# comma, colon, parentheses, or split the sentence instead.
#
# This hook is advisory only. It emits a WARNING on stderr and exits 0 so the
# write/edit is not blocked.
#
# Input: JSON on stdin from Claude Code hook system
# Exit 0: allow (warnings emitted on stderr, advisory only)
# Exit 2: block (not used)

set -euo pipefail

INPUT=$(cat)

# Get the content being written/edited
TOOL_NAME=$(echo "$INPUT" | jq -r '.tool_name // empty')
if [[ "$TOOL_NAME" == "Edit" ]]; then
	NEW_CONTENT=$(echo "$INPUT" | jq -r '.tool_input.new_string // empty')
else
	NEW_CONTENT=$(echo "$INPUT" | jq -r '.tool_input.content // empty')
fi

if [[ -z "$NEW_CONTENT" ]]; then
	exit 0
fi

# Check for the em-dash character (U+2014, UTF-8: 0xE2 0x80 0x94)
if echo "$NEW_CONTENT" | grep -qP '\x{2014}' 2>/dev/null || \
   echo "$NEW_CONTENT" | grep -qF $'\xe2\x80\x94'; then
	FILE_PATH=$(echo "$INPUT" | jq -r '.tool_input.file_path // empty')
	cat >&2 <<-'EOF'
		WARNING: Em-dash (U+2014) found in content being written.
	EOF
	cat >&2 <<-EOF
		File: ${FILE_PATH:-<unknown>}
	EOF
	cat >&2 <<-'EOF'

		The CLAUDE.md Writing Style guide forbids em-dashes in new content.
		Replace each em-dash with a comma, colon, parentheses, or split the
		sentence instead. For example:

		  Before: "The service (complex) handles auth."
		  Use:    comma, colon, or parentheses (see CLAUDE.md Writing Style).

		This is advisory only. The write will proceed, but please revise the
		content before committing.
	EOF
fi

exit 0
