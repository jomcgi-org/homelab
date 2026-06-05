#!/bin/bash
# PreToolUse hook: warn when a bare ``# nosemgrep`` suppression is written
# without a specific rule ID.
#
# Bare ``# nosemgrep`` (with no colon + rule-id after it) suppresses ALL
# semgrep rules on that line — a broad suppression that hides future rule
# violations.  The preferred form is ``# nosemgrep: specific-rule-id``.
#
# Input: JSON on stdin from Claude Code hook system
# Exit 0: allow (warnings emitted on stderr — advisory only)
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

# Detect bare "# nosemgrep" — lines where nosemgrep is NOT followed by a
# colon and a rule ID.  A valid suppression looks like:
#   # nosemgrep: rule-id-here
# Bare suppressions match:
#   # nosemgrep
#   # nosemgrep  (trailing whitespace)
BARE_LINES=$(echo "$NEW_CONTENT" | grep -nE '#\s*nosemgrep\s*$' || true)

if [[ -n "$BARE_LINES" ]]; then
	FILE_PATH=$(echo "$INPUT" | jq -r '.tool_input.file_path // empty')
	cat >&2 <<-EOF
		WARNING: Bare # nosemgrep suppression detected — suppresses ALL rules.

		File: ${FILE_PATH:-<unknown>}
		Line(s):
		$BARE_LINES

		A bare # nosemgrep silently suppresses every semgrep rule on that line,
		including rules added in the future.  Use a specific rule ID instead:

		  # nosemgrep: rule-category.rule-name

		This makes the suppression self-documenting and scoped to a known
		violation, so new rules still fire on that line.

		If you intentionally want to suppress all rules on this line, proceed —
		but consider whether a targeted suppression is more appropriate.
	EOF
fi

exit 0
