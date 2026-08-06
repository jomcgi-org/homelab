#!/bin/bash
# PreToolUse hook: blocks pipe-masked ci and bb remote invocations.
#
# Truncating or discarding gate output can mask a remote-Bazel failure and
# produce a false-green report, as in GitHub issue #4118.
#
# Exit 0: allow; exit 2: block (reason on stderr).

set -euo pipefail

INPUT=$(cat)
COMMAND=$(echo "$INPUT" | jq -r '.tool_input.command // empty')

# Only check commands that contain ci or bb.
if [[ "$COMMAND" != *ci* && "$COMMAND" != *"bb "* ]]; then
	exit 0
fi

# Check for a ci gate or bb invocation at a command segment boundary.
if ! printf '%s' "$COMMAND" | grep -qE '(^|[;&|][[:space:]]*)([^[:space:]]*/)?(ci|bb)([[:space:]]|$)'; then
	exit 0
fi

# Block output that is truncated, searched, or discarded.
if printf '%s' "$COMMAND" | grep -qE '\|[[:space:]]*(tail|head|grep)([[:space:]]|$)' ||
	printf '%s' "$COMMAND" | grep -qE '>[[:space:]]*/dev/null|&>[[:space:]]*/dev/null'; then
	cat >&2 <<-'EOF'
		BLOCKED: Truncating or discarding ci / bb remote output can cause false-green reports, issue #4118, because exit 0 does not prove tests ran.

		Run the ci or bb remote command unpiped, or use | tee to save its output to a file. Judge the run by the "Executed N out of M tests" summary line, then grep the saved log for FAILED.

		If the pipe belongs to a different command in a chain, split the ci invocation into its own Bash call.
	EOF
	exit 2
fi

exit 0
