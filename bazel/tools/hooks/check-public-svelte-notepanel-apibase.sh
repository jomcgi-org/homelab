#!/bin/bash
# PreToolUse hook: warn when writing/editing a .svelte file under routes/public/
# that uses NotePanel without explicitly overriding apiBase to a /public/ path.
#
# Background: NotePanel defaults apiBase to /api/knowledge/notes, which is the
# private (authenticated) endpoint. Public pages that render NotePanel must
# explicitly set apiBase="/api/knowledge/public/notes" (or another /public/
# variant) so that only visibility-filtered notes are returned to unauthenticated
# visitors.
#
# This hook fires on Write|Edit when:
#   1. The file path matches **/routes/public/**/*.svelte
#   2. The new content contains 'NotePanel' (import or component tag usage)
#   3. The content does NOT also contain apiBase= with a value containing '/public/'
#
# Input: JSON on stdin from Claude Code hook system
# Exit 0: allow (warnings emitted on stderr — advisory only)
# Exit 2: block (not used)

set -euo pipefail

INPUT=$(cat)

# Only check .svelte files under routes/public/
FILE_PATH=$(echo "$INPUT" | jq -r '.tool_input.file_path // empty')
if [[ -z "$FILE_PATH" ]]; then
	exit 0
fi

# Path must match **/routes/public/**/*.svelte
if [[ "$FILE_PATH" != */routes/public/*.svelte && "$FILE_PATH" != */routes/public/*/*.svelte && "$FILE_PATH" != */routes/public/*/*/*.svelte ]]; then
	# More robust glob via a pattern check
	if ! echo "$FILE_PATH" | grep -qE '.*/routes/public/.+\.svelte$'; then
		exit 0
	fi
fi

# Get the content being written (Write tool) or the replacement string (Edit tool)
NEW_CONTENT=$(echo "$INPUT" | jq -r '.tool_input.new_string // .tool_input.content // empty')
if [[ -z "$NEW_CONTENT" ]]; then
	exit 0
fi

# Only relevant if NotePanel is present (import or tag usage)
if ! echo "$NEW_CONTENT" | grep -q 'NotePanel'; then
	exit 0
fi

# Check that apiBase= with a /public/ value is also present
if echo "$NEW_CONTENT" | grep -qE 'apiBase=["\x27][^"'\'']*\/public\/'; then
	exit 0
fi

cat >&2 <<-EOF
	WARNING: NotePanel used in a public route without a /public/ apiBase override.

	File: $FILE_PATH

	NotePanel defaults apiBase to /api/knowledge/notes, which is the private
	(authenticated) knowledge endpoint. Public pages must override this to a
	visibility-filtered endpoint such as /api/knowledge/public/notes to prevent
	leaking private notes to unauthenticated visitors.

	Fix: pass the apiBase prop explicitly, e.g.:
	  <NotePanel apiBase="/api/knowledge/public/notes" ... />

	Without this override, NotePanel will silently return private notes to anyone
	who can reach the public page.
EOF

exit 0
