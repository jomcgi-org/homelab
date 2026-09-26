#!/bin/bash
# Consolidated PreToolUse hook for Write|Edit operations.
# Runs all checks in a single process with one jq parse.
#
# Checks:
#   1. plan-worktree: blocks plan/design file writes to the main worktree (exit 2)
#
# Input: JSON on stdin from Claude Code hook system
# Exit 0: allow (warnings may be emitted on stderr)
# Exit 2: block the operation

set -euo pipefail

INPUT=$(cat)
FILE_PATH=$(echo "$INPUT" | jq -r '.tool_input.file_path // empty')

# No file path — nothing to check
if [[ -z "$FILE_PATH" ]]; then
	exit 0
fi

# ── Check 1: plans-are-retired ──────────────────────────────────────────
# docs/plans/ is retired. Plans are no longer committed to the repo; the plan
# for a piece of work lives in its GitHub issue (or the feature's tracking
# issue), or as an uncommitted working file. GitHub Issues are the source of
# truth for outstanding work.
if [[ "$FILE_PATH" == *"/docs/plans/"* ]]; then
	cat >&2 <<-EOF
		BLOCKED: docs/plans/ is retired. Do not commit plan/design docs to the repo.
		File: $FILE_PATH

		Instead:
		  - Put the plan in the feature's GitHub issue (or a parent tracking issue
		    with sub-issues); GitHub Issues are the source of truth for outstanding work.
		  - Or keep it as an uncommitted working file (e.g. under /tmp), not in docs/plans/.

		Record the decision and rationale in the domain's ARCHITECTURE.md if warranted.
	EOF
	exit 2
fi

exit 0
