#!/bin/bash
# PreToolUse hook: remind that an architecture doc is the source of truth
# for current state when an ADR under a covered category changes.
#
# Categories with a rolled-up architecture document keep that document
# authoritative for what is true now; ADRs record the rationale. Editing an
# ADR without checking the architecture doc is how the two drift, so this
# hook emits an advisory prompt on any write into a covered category.
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
# Two kinds of watched directory, because a domain's current state is decided
# in two places:
#
#   - Its ADR category, where a decision changes. Only usable when the category
#     belongs to one domain. docs/decisions/agents/ spans mcp, the monolith
#     agents console, swarm and EmberVM, so it is deliberately NOT listed: it
#     would fire on every unrelated ADR and get ignored.
#   - Its chart and deploy trees, where the mechanism changes. The mcp rollup
#     found that most load-bearing architecture in this repo is recorded as
#     values-file comments, so a values edit is exactly when the architecture
#     doc needs re-reading.
COVERAGE=(
	"docs/decisions/embervm/ projects/embervm/ARCHITECTURE.md"
	"projects/embervm/chart/ projects/embervm/ARCHITECTURE.md"
	"projects/embervm/deploy/ projects/embervm/ARCHITECTURE.md"
	"projects/mcp/context-forge-gateway/chart/ projects/mcp/ARCHITECTURE.md"
	"projects/mcp/context-forge-gateway/deploy/ projects/mcp/ARCHITECTURE.md"
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
			ADRs and config comments record the rationale. You are changing
			${watched}. If this edit changes a decision or a mechanism that
			document describes, amend ${arch_doc} in the same PR so the two
			do not drift.
		EOF
		break
	fi
done

exit 0
