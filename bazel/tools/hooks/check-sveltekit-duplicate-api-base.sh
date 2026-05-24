#!/bin/bash
# PreToolUse hook: warn when writing/editing a +page.server.js or +server.js file
# under projects/monolith/frontend/ that declares a local API_BASE constant.
#
# Background: Every +page.server.js file in the SvelteKit frontend has historically
# redeclared `const API_BASE = process.env.API_BASE` inline. PR analysis of #2339–
# #2353 found seven files with this copy-paste and one inconsistent variant
# (MONOLITH_API vs API_BASE). Centralising to a shared $lib/server/api.js module
# makes future base-URL changes a one-line edit and prevents the inconsistency.
#
# This hook fires on Write|Edit when:
#   1. The file path matches **/projects/monolith/frontend/**/(+page.server.js|+server.js)
#   2. The new content contains `const API_BASE = process.env.API_BASE`
#
# Input: JSON on stdin from Claude Code hook system
# Exit 0: allow (warnings emitted on stderr — advisory only)
# Exit 2: block (not used)

set -euo pipefail

INPUT=$(cat)

# Only check +page.server.js and +server.js files under projects/monolith/frontend/
FILE_PATH=$(echo "$INPUT" | jq -r '.tool_input.file_path // empty')
if [[ -z "$FILE_PATH" ]]; then
	exit 0
fi

# Must be under projects/monolith/frontend/
if ! echo "$FILE_PATH" | grep -qE '.*/projects/monolith/frontend/'; then
	exit 0
fi

# Must be a +page.server.js or +server.js file
BASENAME=$(basename "$FILE_PATH")
if [[ "$BASENAME" != "+page.server.js" && "$BASENAME" != "+server.js" ]]; then
	exit 0
fi

# Get the content being written (Write tool) or the replacement string (Edit tool)
NEW_CONTENT=$(echo "$INPUT" | jq -r '.tool_input.new_string // .tool_input.content // empty')
if [[ -z "$NEW_CONTENT" ]]; then
	exit 0
fi

# Check for the duplicate API_BASE constant declaration
if ! echo "$NEW_CONTENT" | grep -qF 'const API_BASE = process.env.API_BASE'; then
	exit 0
fi

cat >&2 <<-EOF
	WARNING: Duplicate API_BASE constant detected in a SvelteKit server file.

	File: $FILE_PATH

	Every +page.server.js and +server.js file should import API_BASE from the
	shared \$lib/server/api.js module instead of redeclaring it inline:

	  // Instead of:
	  const API_BASE = process.env.API_BASE;

	  // Use:
	  import { API_BASE } from '\$lib/server/api.js';

	Inline redeclarations lead to copy-paste drift (e.g. MONOLITH_API vs API_BASE)
	and make future base-URL changes require edits in seven+ files. Centralise to
	the shared module so changes propagate automatically.

	If \$lib/server/api.js does not yet exist, create it with:
	  export const API_BASE = process.env.API_BASE;
EOF

exit 0
