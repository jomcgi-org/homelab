#!/bin/bash
# PreToolUse hook: warn when writing a /public/ route to knowledge/router.py
# without referencing public_notes_filter() or effective_visibility().
#
# Background: PR #2298 established that every /public/ route querying Note
# must call public_notes_filter() or effective_visibility() to avoid
# leaking non-public notes. A bare .where() without these helpers silently
# returns private notes to unauthenticated callers.
#
# This hook fires on Write|Edit when:
#   1. The file path matches **/knowledge/router.py
#   2. The new content introduces a route decorator containing "/public/"
#   3. The function body immediately following that decorator does NOT
#      reference public_notes_filter or effective_visibility
#
# Input: JSON on stdin from Claude Code hook system
# Exit 0: allow (warnings emitted on stderr — advisory only)
# Exit 2: block (not used)

set -euo pipefail

INPUT=$(cat)

# Only check knowledge/router.py
FILE_PATH=$(echo "$INPUT" | jq -r '.tool_input.file_path // empty')
if [[ -z "$FILE_PATH" ]]; then
	exit 0
fi
if [[ "$FILE_PATH" != */knowledge/router.py ]]; then
	exit 0
fi

# Get the content being written (Write tool) or the replacement string (Edit tool)
NEW_CONTENT=$(echo "$INPUT" | jq -r '.tool_input.new_string // .tool_input.content // empty')
if [[ -z "$NEW_CONTENT" ]]; then
	exit 0
fi

# Only relevant if a /public/ route is present
if ! echo "$NEW_CONTENT" | grep -qE '@router\.(get|post|put|patch|delete)\([^)]*['"'"'"/]public/'; then
	exit 0
fi

# Use Python to find /public/ route functions that lack the required filter call.
# Content is passed via HOOK_CONTENT env var to avoid stdin/heredoc conflicts.
WARNINGS=$(
	HOOK_CONTENT="$NEW_CONTENT" python3 - "$FILE_PATH" <<'PYTHON'
import os
import re
import sys

file_path = sys.argv[1]
content = os.environ['HOOK_CONTENT']

REQUIRED = ('public_notes_filter', 'effective_visibility')

# Split content into lines for processing.
lines = content.splitlines()

warnings = []
i = 0
while i < len(lines):
    line = lines[i]
    # Detect a route decorator that includes /public/
    if re.search(r'@router\.(get|post|put|patch|delete)\([^)]*[\'"/]public/', line):
        decorator_line = i + 1  # 1-based for display
        # Collect the function body: skip any additional decorator lines, then
        # grab lines until the next top-level def/class or end of content.
        j = i + 1
        # Skip additional decorators (lines starting with @)
        while j < len(lines) and lines[j].lstrip().startswith('@'):
            j += 1
        # j should now be the 'def ...' line
        func_start = j
        j += 1
        func_lines = [lines[func_start]] if func_start < len(lines) else []
        # Collect indented body lines
        while j < len(lines):
            l = lines[j]
            # Stop at next top-level definition (non-empty, non-indented)
            if l and not l[0].isspace() and not l.startswith('#'):
                break
            func_lines.append(l)
            j += 1

        func_body = '\n'.join(func_lines)

        # Check if either required helper is referenced
        has_filter = any(req in func_body for req in REQUIRED)
        if not has_filter:
            # Extract route path for the warning message
            route_match = re.search(r'[\'"]([^\'"]*public[^\'"]*)[\'"]', line)
            route_path = route_match.group(1) if route_match else line.strip()
            warnings.append(
                f'  - Route at line {decorator_line}: {route_path!r}'
            )

    i += 1

for w in warnings:
    print(w)
PYTHON
)

if [[ -n "$WARNINGS" ]]; then
	cat >&2 <<-EOF
		WARNING: /public/ route in knowledge/router.py missing visibility filter.

		File: $FILE_PATH

		Every route under /public/ that queries Note must call either
		public_notes_filter() or effective_visibility() to prevent leaking
		non-public notes to unauthenticated callers (PR #2298).

		Routes missing visibility guard:
		$WARNINGS

		Fix: add public_notes_filter() to the .where() clause, e.g.:
		  session.execute(select(Note).where(public_notes_filter()))
		or check note visibility before returning:
		  if effective_visibility(note) != "public": raise HTTPException(404)
	EOF
fi

exit 0
