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

# Covered categories: "<ADR directory> <architecture doc>" pairs.
COVERAGE=(
	"docs/decisions/embervm/ projects/embervm/ARCHITECTURE.md"
)

for pair in "${COVERAGE[@]}"; do
	adr_dir="${pair%% *}"
	arch_doc="${pair##* }"
	if [[ "$FILE_PATH" == *"$adr_dir"* ]]; then
		cat >&2 <<-EOF
			REMINDER: ${arch_doc} is the source of truth for current state;
			ADRs record rationale. You are changing ${adr_dir}. If this edit
			changes a decision, amend ${arch_doc} in the same PR so the two
			do not drift.
		EOF
		break
	fi
done

exit 0
