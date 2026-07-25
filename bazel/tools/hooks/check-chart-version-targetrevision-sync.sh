#!/bin/bash
# PreToolUse hook: warn when Chart.yaml version and deploy/application.yaml
# targetRevision are out of sync.
#
# ArgoCD pulls charts from OCI by the version string in application.yaml
# targetRevision.  If Chart.yaml version is bumped without updating
# targetRevision (or vice-versa), ArgoCD keeps deploying the old chart version
# and the new image digests never roll out.
#
# The chart-version-bot automates this for main-branch merges, but edits via
# Claude Code happen before the bot runs.  This hook catches the gap early.
#
# Input: JSON on stdin from Claude Code hook system
# Exit 0: allow (warnings emitted on stderr — advisory only)
# Exit 2: block (not used)

set -euo pipefail

INPUT=$(cat)

# Only trigger on */chart/Chart.yaml edits
FILE_PATH=$(echo "$INPUT" | jq -r '.tool_input.file_path // empty')
if [[ -z "$FILE_PATH" ]]; then
	exit 0
fi

if [[ "$FILE_PATH" != */chart/Chart.yaml ]]; then
	exit 0
fi

# Get the content being written/edited
TOOL_NAME=$(echo "$INPUT" | jq -r '.tool_name // empty')
if [[ "$TOOL_NAME" == "Edit" ]]; then
	NEW_CONTENT=$(echo "$INPUT" | jq -r '.tool_input.new_string // empty')
else
	NEW_CONTENT=$(echo "$INPUT" | jq -r '.tool_input.content // empty')
fi

if [[ -z "$NEW_CONTENT" ]]; then
	exit 0
fi

# Extract the new version from the content being written.
# For Edit, new_string may be just the changed snippet (e.g. the version line).
# For Write, it's the full file.
NEW_VERSION=$(echo "$NEW_CONTENT" | grep -E '^version:' | head -1 | awk '{print $2}' | tr -d "\"'" || true)
if [[ -z "$NEW_VERSION" ]]; then
	# version: line not in this edit — nothing to check
	exit 0
fi

# Derive the service root from the Chart.yaml path.
# FILE_PATH: .../projects/<service>/chart/Chart.yaml  (or similar)
CHART_DIR=$(dirname "$FILE_PATH")
SERVICE_DIR=$(dirname "$CHART_DIR")
APP_YAML="$SERVICE_DIR/deploy/application.yaml"

if [[ ! -f "$APP_YAML" ]]; then
	# No application.yaml found — nothing to compare
	exit 0
fi

# Read targetRevision from the application.yaml
TARGET_REVISION=$(grep -E 'targetRevision:' "$APP_YAML" | head -1 | awk '{print $2}' | tr -d "\"'" || true)
if [[ -z "$TARGET_REVISION" ]]; then
	exit 0
fi

if [[ "$NEW_VERSION" != "$TARGET_REVISION" ]]; then
	SERVICE_NAME=$(basename "$SERVICE_DIR")
	cat >&2 <<-EOF
		WARNING: Chart.yaml version and application.yaml targetRevision are out of sync.

		Service:          $SERVICE_NAME
		Chart.yaml:       version: $NEW_VERSION
		application.yaml: targetRevision: $TARGET_REVISION

		ArgoCD pulls charts from OCI by version.  A stale targetRevision means
		ArgoCD keeps deploying the old chart — the new version (and its updated
		image digests) never roll out.

		Update $APP_YAML so that
		  targetRevision: $NEW_VERSION
		matches the new Chart.yaml version before committing.

		(The chart-version-bot does this automatically on main-branch merges,
		but it hasn't run yet for this in-progress edit.)
	EOF
fi

exit 0
