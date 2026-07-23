#!/bin/bash
# Consolidated PreToolUse hook for Write|Edit operations.
# Runs all checks in a single process with one jq parse.
#
# Checks:
#   1. plan-worktree: blocks plan/design file writes to the main worktree (exit 2)
#   2. chart-version-sync: warns when editing Chart.yaml without application.yaml (warning)
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

		Record the decision + rationale in an ADR (docs/decisions/) if one is warranted.
	EOF
	exit 2
fi

# ── Check 2: chart-version-sync ─────────────────────────────────────────
# Editing Chart.yaml without updating deploy/application.yaml targetRevision.
if [[ "$FILE_PATH" == */chart/Chart.yaml ]]; then
	SERVICE_DIR=$(dirname "$(dirname "$FILE_PATH")")
	APP_YAML="$SERVICE_DIR/deploy/application.yaml"

	if [[ -f "$APP_YAML" ]]; then
		REPO_ROOT=$(git -C "$(dirname "$FILE_PATH")" rev-parse --show-toplevel 2>/dev/null || true)
		if [[ -n "$REPO_ROOT" ]]; then
			APP_YAML_REL="${APP_YAML#$REPO_ROOT/}"
			CHANGED=$(git -C "$REPO_ROOT" status --porcelain "$APP_YAML_REL" 2>/dev/null || true)
			DIFF_CHANGED=$(git -C "$REPO_ROOT" diff --name-only HEAD -- "$APP_YAML_REL" 2>/dev/null || true)

			if [[ -z "$CHANGED" ]] && [[ -z "$DIFF_CHANGED" ]]; then
				cat >&2 <<-EOF
					WARNING: Editing chart/Chart.yaml without updating deploy/application.yaml.

					When bumping the chart version in Chart.yaml, you MUST also update
					targetRevision in $APP_YAML_REL to match.

					ArgoCD pulls charts from OCI by version — a stale targetRevision means
					the new chart version never deploys.

					Please also edit: $APP_YAML
				EOF
			fi
		fi
	fi
fi

exit 0
