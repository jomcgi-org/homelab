#!/bin/bash
# PreToolUse hook: remind that an architecture doc is the source of truth
# for current state when covered configuration or build machinery changes.
#
# The hook emits an advisory prompt on writes to domain configuration and
# build trees whose mechanisms are described in an architecture document.
#
# Input: JSON on stdin from Claude Code hook system
# Exit 0: allow (warnings emitted on stderr, advisory only)
# Exit 2: block (not used)

set -euo pipefail

INPUT=$(cat)

FILE_PATH=$(echo "$INPUT" | jq -r '.tool_input.file_path // empty')
if [[ -z "$FILE_PATH" ]]; then
	exit 0
fi

# Covered paths: "<watched directory> <architecture doc>" pairs.
#
# Watched directories cover two kinds of mechanism:
#
#   - Chart and deploy trees. An architecture sweep found that most load-bearing architecture in this repo is recorded as
#     values-file comments, so a values edit is exactly when the architecture
#     doc needs re-reading.
#   - The build and CI trees behind bazel/ARCHITECTURE.md: the ci wrapper
#     and the BuildBuddy workflow file.
#     Matching is by substring, so a bare file name (buildbuddy.yaml) works
#     as a watched path too.
COVERAGE=(
	"bazel/tools/ci/ bazel/ARCHITECTURE.md"
	"buildbuddy.yaml bazel/ARCHITECTURE.md"
	"projects/platform/ projects/platform/ARCHITECTURE.md"
)

for pair in "${COVERAGE[@]}"; do
	watched="${pair%% *}"
	arch_doc="${pair##* }"
	# Editing the architecture doc itself is not drift.
	if [[ "$FILE_PATH" == *"$arch_doc" ]]; then
		continue
	fi
	if [[ "$FILE_PATH" == *"$watched"* ]]; then
		cat >&2 <<-EOF
			REMINDER: ${arch_doc} is the source of truth for current state;
			Architecture docs and config comments record the rationale. You are changing
			${watched}. If this edit changes a decision or a mechanism that
			document describes, amend ${arch_doc} in the same PR so the two
			do not drift.
		EOF
		break
	fi
done

exit 0
