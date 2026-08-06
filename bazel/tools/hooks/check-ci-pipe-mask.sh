#!/bin/bash
# PreToolUse hook: blocks pipe-masked ci and bb remote invocations.
#
# Truncating or discarding gate output can mask a remote-Bazel failure and
# produce a false-green report, as in GitHub issue #4118. A residual false
# positive is accepted for a non-git/gh multi-line command whose embedded text
# has a line starting with a piped ci invocation.
#
# Exit 0: allow; exit 2: block (reason on stderr).

set -euo pipefail

INPUT=$(cat)
COMMAND=$(echo "$INPUT" | jq -r '.tool_input.command // empty')

# Empty command.
if [[ -z "$COMMAND" ]]; then
	exit 0
fi

# Allow git and gh commands (commit messages, PR bodies, and issue comments
# may legitimately mention piped-ci examples; git/gh never run the ci gate).
if [[ "$COMMAND" == git\ * ]] || [[ "$COMMAND" == */git\ * ]] ||
	[[ "$COMMAND" == gh\ * ]] || [[ "$COMMAND" == */gh\ * ]]; then
	exit 0
fi

# Only check commands that contain ci or bb.
if [[ "$COMMAND" != *ci* && "$COMMAND" != *bb* ]]; then
	exit 0
fi

INVOCATION_RE='^((timeout[[:space:]]+[^[:space:]]+|[A-Za-z_][A-Za-z0-9_]*=[^[:space:]]*)[[:space:]]+)*([^[:space:]]*/)?(ci|bb)([[:space:]]|$)'
FILTER_RE='\|&?[[:space:]]*(tail|head|grep|sed|awk|wc|less|more)([[:space:]]|$)'

# Split command control operators, keeping single pipes and |& in segments.
while IFS= read -r segment || [[ -n "$segment" ]]; do
	segment=$(printf '%s' "$segment" | sed -E 's/^[[:space:]]+//; s/^\(+[[:space:]]*//')

	if [[ ! "$segment" =~ $INVOCATION_RE ]]; then
		continue
	fi

	# tee preserves the complete gate output unless it writes to /dev/null.
	if [[ "$segment" =~ \|[[:space:]]*tee[[:space:]] ]] &&
		[[ ! "$segment" =~ \|[[:space:]]*tee[[:space:]]+/dev/null ]]; then
		continue
	fi

	if [[ "$segment" =~ $FILTER_RE ]] ||
		[[ "$segment" =~ \>[[:space:]]*/dev/null ]] ||
		[[ "$segment" =~ \&\>[[:space:]]*/dev/null ]]; then
		cat >&2 <<-'EOF'
			BLOCKED: Piping ci / bb remote output straight into a filter (tail, head, grep, sed, awk, wc) or discarding it can mask a failed run (#4118, exit 0 does not prove tests ran).

			Run the ci or bb remote command unpiped, or use 2>&1 | tee /tmp/ci.log. Further pipes after tee are fine since the full log is preserved. Judge the run by the "Executed N out of M tests" summary and grep the saved log for FAILED.

		EOF
		exit 2
	fi
done < <(printf '%s\n' "$COMMAND" | awk '{ gsub(/&&/, "\n"); gsub(/\|\|/, "\n"); gsub(/;/, "\n"); print }')

exit 0
