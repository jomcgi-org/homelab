#!/bin/bash
# PreToolUse hook: warn when writing/editing a semgrep rule YAML file that adds
# or expands a paths.exclude: list without inline documentation comments.
#
# Exclusions silently shrink a rule's blast radius. Without comments explaining
# WHY a path is excluded, future authors have no signal about whether to extend,
# remove, or copy the exclusion. Five consecutive fix-PRs (#2325–#2329) each
# addressed a single undocumented exclusion — this hook catches them at authoring
# time rather than requiring a dedicated remediation PR per rule.
#
# Trigger: Write|Edit targeting bazel/semgrep/rules/**/*.yaml
# Logic:   Warn if the content contains a `paths: … exclude:` block
#          with at least one entry that has no accompanying inline comment.
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

TOOL_NAME=$(echo "$INPUT" | jq -r '.tool_name // empty')

# Get the content being written/edited
CONTENT=""
if [[ "$TOOL_NAME" == "Edit" ]]; then
	CONTENT=$(echo "$INPUT" | jq -r '.tool_input.new_string // empty')
else
	CONTENT=$(echo "$INPUT" | jq -r '.tool_input.content // empty')
fi

if [[ -z "$CONTENT" ]]; then
	exit 0
fi

# Check whether the content contains a paths.exclude block with any entries
if ! echo "$CONTENT" | grep -q 'exclude:'; then
	exit 0
fi

STEM=$(basename "$FILE_PATH" .yaml)

# Check each exclude entry: look for lines with a quoted or unquoted path pattern
# under `exclude:` that lack an inline `#` comment on the same line.
#
# Strategy: extract lines that look like exclude list items (leading dashes
# inside an exclude block) and check whether any lacks an inline comment.
# We use a small Python script so we can parse YAML-ish indented list items
# without a full YAML parser dependency.
UNDOCUMENTED=$(
	RULE_CONTENT="$CONTENT" python3 <<'PY'
import re, os

lines = os.environ.get('RULE_CONTENT', '').splitlines()
in_exclude = False
found_undocumented = False

for line in lines:
    stripped = line.strip()
    # Detect start of an exclude block
    if re.match(r'exclude\s*:', stripped):
        in_exclude = True
        continue
    # Detect end of exclude block (next key at same or lower indentation level
    # — look for a non-list, non-empty, non-comment line)
    if in_exclude:
        if stripped == '' or stripped.startswith('#'):
            continue
        if stripped.startswith('-'):
            # This is an exclude list item. Check for inline comment.
            if '#' not in line:
                found_undocumented = True
            continue
        # Non-list, non-empty, non-comment line — we've left the exclude block
        in_exclude = False

if found_undocumented:
    print("yes")
PY
)

if [[ -z "$UNDOCUMENTED" ]]; then
	exit 0
fi

cat >&2 <<EOF
WARNING: semgrep rule '${STEM}' adds paths.exclude entries without inline
comments explaining why the path is excluded.

Exclusions silently shrink a rule's blast radius. Each exclusion should have
an inline comment documenting the reason, e.g.:

    paths:
      exclude:
        - "projects/legacy/**"  # legacy service uses raw SQL, tracked in #1234

Without inline comments, future authors have no signal about whether to extend,
remove, or copy the exclusion — and remediation requires a dedicated follow-up
PR per rule (see PRs #2325–#2329).

File: ${FILE_PATH}
EOF

exit 0
