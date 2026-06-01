#!/bin/bash
# PreToolUse hook: warn when setting image.tag in a deploy overlay for an
# OCI-pinned chart.
#
# OCI-published charts (those with `publish = True` in chart/BUILD) have
# digest-pinned images baked in by helm_images_values at build time.  The
# deploy/values.yaml overlay is applied AFTER that base (Helm valueFiles are
# last-wins), so any `tag:` set here silently overrides the pinned digest and
# the container image becomes unpinned again.
#
# Input: JSON on stdin from Claude Code hook system
# Exit 0: allow (warnings emitted on stderr — advisory only)
# Exit 2: block (not used)

set -euo pipefail

INPUT=$(cat)

# Only trigger on projects/*/deploy/values.yaml edits
FILE_PATH=$(echo "$INPUT" | jq -r '.tool_input.file_path // empty')
if [[ -z "$FILE_PATH" ]]; then
	exit 0
fi

if [[ "$FILE_PATH" != */projects/*/deploy/values.yaml ]]; then
	exit 0
fi

# Derive the service root: projects/<service>
# FILE_PATH looks like .../projects/<service>/deploy/values.yaml
DEPLOY_DIR=$(dirname "$FILE_PATH")
SERVICE_DIR=$(dirname "$DEPLOY_DIR")
CHART_BUILD="$SERVICE_DIR/chart/BUILD"

# Only apply this check if the chart is OCI-published
if [[ ! -f "$CHART_BUILD" ]]; then
	exit 0
fi
if ! grep -q 'publish = True' "$CHART_BUILD" 2>/dev/null; then
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

# Detect tag: appearing under an image: block via awk.
# Tracks indentation: when a line ends with "image:", record its indent depth.
# On subsequent lines at a greater indent level, look for "tag:".
HAS_TAG=$(echo "$NEW_CONTENT" | awk '
  /image:[[:space:]]*$/ {
    img_line = $0
    sub(/[^[:space:]].*/, "", img_line)
    img_indent = length(img_line)
    in_image = 1
    next
  }
  in_image {
    if (/^[[:space:]]*$/) next
    curr_line = $0
    sub(/[^[:space:]].*/, "", curr_line)
    curr_indent = length(curr_line)
    if (curr_indent > img_indent) {
      if (/[[:space:]]tag:/) { print "yes"; exit }
    } else {
      in_image = 0
    }
  }
' || true)

if [[ "$HAS_TAG" == "yes" ]]; then
	SERVICE_NAME=$(basename "$SERVICE_DIR")
	cat >&2 <<-EOF
		WARNING: Setting image.tag in a deploy overlay for an OCI-pinned chart
		silently un-pins digest pinning.

		Service: $SERVICE_NAME
		File:    $FILE_PATH

		The chart at $CHART_BUILD has publish = True, which means the OCI
		chart ships with digest-pinned image tags baked in by helm_images_values
		at CI build time.

		Helm applies valueFiles last-wins: the deploy/values.yaml overlay is
		layered on top of the packaged chart values, so any image.tag set here
		overrides the pinned digest and the image becomes unpinned.

		Remove the image.tag line(s) from the overlay.  Only pullPolicy and
		other non-tag fields should appear under image: in the deploy overlay.

		If you intentionally want to pin a specific tag (e.g. for a rollback),
		proceed — but be aware this disables automatic digest pinning.
	EOF
fi

exit 0
