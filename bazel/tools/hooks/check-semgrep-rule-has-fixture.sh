#!/bin/bash
# PreToolUse hook: warn when writing/editing a semgrep rule YAML file that has
# no corresponding test fixture.
#
# Fixtures can live in three places:
#   1. bazel/semgrep/tests/fixtures/<rule-stem>.<ext>  (central fixtures dir)
#   2. bazel/semgrep/rules/<subdir>/<rule-stem>.<ext>  (colocated, any non-yaml ext)
#      Python fixtures use underscore naming: <rule_stem>.py
#   3. bazel/semgrep/tests/yaml/<rule-stem>/  (directory with ok.yaml + bad.yaml)
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

# Only check semgrep rule yaml files (bazel/semgrep/rules/<subdir>/<name>.yaml)
if [[ "$FILE_PATH" != */bazel/semgrep/rules/*/*.yaml ]]; then
	exit 0
fi

STEM=$(basename "$FILE_PATH" .yaml)
RULE_DIR=$(dirname "$FILE_PATH")

# Derive the project root (everything before /bazel/semgrep/)
PROJECT_DIR=$(echo "$FILE_PATH" | sed 's|/bazel/semgrep/.*||')
FIXTURES_DIR="${PROJECT_DIR}/bazel/semgrep/tests/fixtures"

# Python colocated fixtures use underscores instead of dashes
STEM_UNDERSCORED=$(echo "$STEM" | tr '-' '_')

# Helper: check if any non-yaml file matches a glob prefix
has_non_yaml_fixture() {
	local prefix="$1"
	local found=false
	# Use a subshell with nullglob to safely iterate
	while IFS= read -r f; do
		[[ "$f" == *.yaml ]] && continue
		found=true
		break
	done < <(compgen -G "${prefix}."'*' 2>/dev/null || true)
	$found
}

# Check 1: central fixtures dir — any extension (including .yaml)
if compgen -G "${FIXTURES_DIR}/${STEM}."'*' >/dev/null 2>&1; then
	exit 0
fi

# Check 1b: tests/yaml/<stem>/ directory (ok.yaml + bad.yaml convention)
YAML_TEST_DIR="${PROJECT_DIR}/bazel/semgrep/tests/yaml/${STEM}"
if [[ -d "$YAML_TEST_DIR" ]]; then
	exit 0
fi

# Check 2: colocated fixture with dash-named stem (non-yaml)
if has_non_yaml_fixture "${RULE_DIR}/${STEM}"; then
	exit 0
fi

# Check 3: colocated fixture with underscore-named stem (Python convention)
if [[ "$STEM_UNDERSCORED" != "$STEM" ]] && has_non_yaml_fixture "${RULE_DIR}/${STEM_UNDERSCORED}"; then
	exit 0
fi

cat >&2 <<EOF
WARNING: No test fixture found for semgrep rule '${STEM}'.

Expected a fixture in one of:
  ${FIXTURES_DIR}/${STEM}.<ext>
  ${YAML_TEST_DIR}/  (directory with ok.yaml + bad.yaml)
  ${RULE_DIR}/${STEM}.<ext>  (colocated)
  ${RULE_DIR}/${STEM_UNDERSCORED}.<ext>  (colocated, Python naming)

Add a fixture file to validate the rule catches real violations.
See bazel/semgrep/tests/fixtures/ for examples.

File: ${FILE_PATH}
EOF

exit 0
