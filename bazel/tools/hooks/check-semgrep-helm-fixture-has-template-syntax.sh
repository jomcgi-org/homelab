#!/bin/bash
# PreToolUse hook: warn when writing/editing a semgrep fixture YAML for a rule
# that uses languages: [generic] and targets chart/templates paths, but the
# fixture content contains no Helm template directives ({{ ... }}).
#
# Generic-mode semgrep rules that scan Helm templates must have fixtures that
# include {{ }} directives — otherwise the fixture tests YAML-only content and
# misses the critical Helm-specific patterns the rule is meant to catch.
#
# Input: JSON on stdin from Claude Code hook system
# Exit 0: allow (warnings emitted on stderr — advisory only)
# Exit 2: block (not used)

set -euo pipefail

INPUT=$(cat)

FILE_PATH=$(echo "$INPUT" | jq -r '.tool_input.file_path // empty')
if [[ -z "$FILE_PATH" ]]; then
	exit 0
fi

# Only check yaml files in bazel/semgrep/tests/fixtures/
if [[ "$FILE_PATH" != */bazel/semgrep/tests/fixtures/*.yaml ]]; then
	exit 0
fi

STEM=$(basename "$FILE_PATH" .yaml)
TOOL_NAME=$(echo "$INPUT" | jq -r '.tool_name // empty')

# Derive the project root and find the corresponding rule file
PROJECT_DIR=$(echo "$FILE_PATH" | sed 's|/bazel/semgrep/.*||')
RULE_FILE=""
while IFS= read -r candidate; do
	if [[ -f "$candidate" ]]; then
		RULE_FILE="$candidate"
		break
	fi
done < <(find "${PROJECT_DIR}/bazel/semgrep/rules" -name "${STEM}.yaml" 2>/dev/null || true)

# No rule file found — nothing to check
if [[ -z "$RULE_FILE" ]]; then
	exit 0
fi

# Check if the rule uses languages: [generic] AND targets chart/templates
RULE_CONTENT=$(cat "$RULE_FILE")
if ! echo "$RULE_CONTENT" | grep -qE 'languages:\s*\[.*generic'; then
	exit 0
fi
if ! echo "$RULE_CONTENT" | grep -q 'chart/templates'; then
	exit 0
fi

# Get the fixture content being written
FIXTURE_CONTENT=""
if [[ "$TOOL_NAME" == "Edit" ]]; then
	NEW_STRING=$(echo "$INPUT" | jq -r '.tool_input.new_string // empty')
	# Also check existing file content — if it already has {{ the edit is fine
	if [[ -f "$FILE_PATH" ]]; then
		if grep -q '{{' "$FILE_PATH"; then
			exit 0
		fi
	fi
	FIXTURE_CONTENT="$NEW_STRING"
else
	# Write tool — use the full new content
	FIXTURE_CONTENT=$(echo "$INPUT" | jq -r '.tool_input.content // empty')
fi

if [[ -z "$FIXTURE_CONTENT" ]]; then
	exit 0
fi

# Check for Helm template syntax
if echo "$FIXTURE_CONTENT" | grep -q '{{'; then
	exit 0
fi

cat >&2 <<EOF
WARNING: Fixture for '${STEM}' targets a generic-language rule that scans
chart/templates paths, but the fixture content has no Helm template
directives ({{ ... }}).

Generic semgrep rules over chart/templates must include {{ }} patterns in
their fixtures to ensure the rule fires correctly on Helm template syntax.

Rule: ${RULE_FILE}
Fixture: ${FILE_PATH}

Add at least one line with a Helm directive (e.g. {{ .Values.foo }}) to
validate the rule works in a realistic Helm template context.
EOF

exit 0
