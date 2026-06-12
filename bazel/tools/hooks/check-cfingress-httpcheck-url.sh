#!/bin/bash
# PreToolUse hook: warn when a cfIngress values file is being written/edited
# but the sibling *-httpcheck-alert.yaml URL filter doesn't match the
# cfIngress hostname + pathPrefix.
#
# Background: when a service migrates to a new hostname or sub-path via
# cfIngress, the httpcheck alert must also be updated to monitor the new
# URL. A stale alert silently monitors the old endpoint and misses outages
# on the new route.
#
# The hook fires on Write|Edit when:
#   1. The file path matches */projects/platform/*/values*.yaml
#   2. The file content contains cfIngress: with enabled: true
#   3. A sibling *-httpcheck-alert.yaml exists in the same directory
#   4. The http.url filter expression in that alert does not start with
#      https://<hostname><pathPrefix>
#
# Input: JSON on stdin from Claude Code hook system
# Exit 0: allow (warnings emitted on stderr -- advisory only)
# Exit 2: block (not used)

set -euo pipefail

INPUT=$(cat)

FILE_PATH=$(echo "$INPUT" | jq -r '.tool_input.file_path // empty')
if [[ -z "$FILE_PATH" ]]; then
	exit 0
fi

# Only check platform service values files
if [[ "$FILE_PATH" != */projects/platform/*/values*.yaml ]]; then
	exit 0
fi

# For Edit, read the full existing file to get the complete cfIngress block.
# For Write, use the incoming content directly.
TOOL_NAME=$(echo "$INPUT" | jq -r '.tool_name // empty')
if [[ "$TOOL_NAME" == "Edit" ]]; then
	if [[ -f "$FILE_PATH" ]]; then
		FULL_CONTENT=$(cat "$FILE_PATH")
	else
		FULL_CONTENT=$(echo "$INPUT" | jq -r '.tool_input.new_string // empty')
	fi
else
	FULL_CONTENT=$(echo "$INPUT" | jq -r '.tool_input.content // empty')
fi

if [[ -z "$FULL_CONTENT" ]]; then
	exit 0
fi

# Quick pre-filter: skip if cfIngress block is absent or not enabled
if ! echo "$FULL_CONTENT" | grep -qE '^\s*cfIngress:'; then
	exit 0
fi
if ! echo "$FULL_CONTENT" | grep -qE '^\s*enabled:\s*true'; then
	exit 0
fi

# Extract hostname and pathPrefix from the cfIngress block via Python.
# Python handles the indentation parsing more reliably than awk/sed.
CFINGRESS_INFO=$(echo "$FULL_CONTENT" | python3 - <<'PYEOF' || true)
import sys
import re

content = sys.stdin.read()

# Find the cfIngress: block start
m = re.search(r'^cfIngress:\s*$', content, re.MULTILINE)
if not m:
    sys.exit(0)

block = content[m.start():]

# Check that enabled: true is present in this block before another top-level key
enabled = re.search(r'^\s{1,}enabled:\s*true', block, re.MULTILINE)
if not enabled:
    sys.exit(0)

hostname_m = re.search(r'^\s{1,}hostname:\s*(.+)$', block, re.MULTILINE)
prefix_m = re.search(r'^\s{1,}pathPrefix:\s*(.+)$', block, re.MULTILINE)

if not hostname_m:
    sys.exit(0)

hostname = hostname_m.group(1).strip().strip('"\'')
path_prefix = prefix_m.group(1).strip().strip('"\'') if prefix_m else ''

print(f"{hostname}|{path_prefix}")
PYEOF

if [[ -z "$CFINGRESS_INFO" ]]; then
	exit 0
fi

HOSTNAME=$(echo "$CFINGRESS_INFO" | cut -d'|' -f1)
PATH_PREFIX=$(echo "$CFINGRESS_INFO" | cut -d'|' -f2)

if [[ -z "$HOSTNAME" ]]; then
	exit 0
fi

EXPECTED_PREFIX="https://${HOSTNAME}${PATH_PREFIX}"
SERVICE_DIR=$(dirname "$FILE_PATH")

# Find sibling httpcheck alert files
ALERT_FILES=()
while IFS= read -r f; do
	ALERT_FILES+=("$f")
done < <(compgen -G "${SERVICE_DIR}/*-httpcheck-alert.yaml" 2>/dev/null || true)

if [[ ${#ALERT_FILES[@]} -eq 0 ]]; then
	exit 0
fi

MISMATCHES=()
for ALERT_FILE in "${ALERT_FILES[@]}"; do
	# Extract the URL from expressions like: http.url = 'https://...'
	ALERT_URL=$(grep -oE "http\.url = '[^']+'" "$ALERT_FILE" |
		grep -oE "'[^']+'" |
		tr -d "'" |
		head -1 || true)
	if [[ -z "$ALERT_URL" ]]; then
		continue
	fi
	if [[ "$ALERT_URL" != "${EXPECTED_PREFIX}"* ]]; then
		MISMATCHES+=("$(basename "$ALERT_FILE"): monitors $ALERT_URL, expected prefix $EXPECTED_PREFIX")
	fi
done

if [[ ${#MISMATCHES[@]} -gt 0 ]]; then
	SERVICE_NAME=$(basename "$SERVICE_DIR")
	cat >&2 <<-EOF
		WARNING: cfIngress hostname/pathPrefix may be out of sync with httpcheck alert URL.

		File:           $FILE_PATH
		Service:        $SERVICE_NAME
		cfIngress URL:  $EXPECTED_PREFIX

		Mismatched alert(s):
	EOF
	for m in "${MISMATCHES[@]}"; do
		echo "  - $m" >&2
	done
	cat >&2 <<-EOF

		If cfIngress is now the canonical route, update the *-httpcheck-alert.yaml
		file to monitor $EXPECTED_PREFIX.
		If the old URL is still authoritative (e.g. a direct tunnel that has not
		been decommissioned), this warning is expected -- add a comment in
		$FILE_PATH explaining the intentional divergence.
	EOF
fi

exit 0
