#!/bin/bash
# PreToolUse hook: warn when writing Python files that register MCP tools
# (@mcp.tool) containing Context Forge forbidden patterns in docstrings.
#
# Background: semicolons and other shell-like patterns in MCP tool docstrings
# cause Context Forge to silently drop tools during discovery (PR #2300).
# A CI test was added in PR #2301 (mcp_description_compliance_test.py), but
# this hook provides an earlier write-time guard.
#
# Forbidden patterns:
#   - Anywhere in the file:   &&  ||  $(  "> "  "< "
#   - Inside docstrings only: ;   |
#
# Input: JSON on stdin from Claude Code hook system
# Exit 0: allow (warnings emitted on stderr — advisory only, matching
#          the check-hardcoded-model-subprocess.sh pattern)
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

# Get the content being written (Write tool) or the replacement string (Edit tool)
NEW_CONTENT=$(echo "$INPUT" | jq -r '.tool_input.new_string // .tool_input.content // empty')
if [[ -z "$NEW_CONTENT" ]]; then
	exit 0
fi

# Only relevant if this file registers MCP tools
if ! echo "$NEW_CONTENT" | grep -qE '@mcp\.tool|mcp\.tool\(\)'; then
	exit 0
fi

# Use Python to detect forbidden patterns, with docstring-boundary awareness.
# Content is passed via MCP_CONTENT env var to avoid stdin/heredoc conflicts.
WARNINGS=$(
	MCP_CONTENT="$NEW_CONTENT" python3 - "$FILE_PATH" <<'PYTHON'
import os
import re
import sys

file_path = sys.argv[1]
content = os.environ['MCP_CONTENT']

warnings = []

# Patterns forbidden anywhere in an MCP tool Python file.
# (&&, ||, $( are not valid Python operators, so they only appear in strings
# or comments — still suspicious enough to flag.)
ANYWHERE_PATTERNS = ['&&', '||', '$(', '"> "', '"< "']
for pat in ANYWHERE_PATTERNS:
    if pat in content:
        warnings.append(f'  - {pat!r} (forbidden anywhere in MCP tool files)')

# Extract all triple-quoted strings so we can check docstring-only patterns.
# Handles both """ and ''' delimiters; re.DOTALL so . matches newlines.
docstring_content = ''
for m in re.finditer(r'""".*?"""|\'\'\'.*?\'\'\'', content, re.DOTALL):
    docstring_content += m.group(0) + '\n'

# Patterns only forbidden inside docstrings (too common elsewhere to flag broadly).
DOCSTRING_PATTERNS = [';', '|']
for pat in DOCSTRING_PATTERNS:
    if pat in docstring_content:
        warnings.append(
            f'  - {pat!r} inside a docstring (forbidden in MCP tool descriptions)'
        )

for w in warnings:
    print(w)
PYTHON
)

if [[ -n "$WARNINGS" ]]; then
	cat >&2 <<-EOF
		WARNING: MCP tool (@mcp.tool) file contains Context Forge forbidden patterns.

		File: $FILE_PATH

		Context Forge silently drops MCP tools whose descriptions contain
		shell-like syntax during discovery. See PR #2300 (root cause) and
		PR #2301 (mcp_description_compliance_test.py) for the CI test.

		Forbidden patterns detected:
		$WARNINGS

		Forbidden patterns reference:
		  Anywhere in file:   &&  ||  \$(  "> "  "< "
		  Inside docstrings:  ;   |

		Fix: rephrase the tool description to avoid shell-like syntax.
	EOF
fi

exit 0
