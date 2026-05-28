#!/bin/bash
# PreToolUse hook: warn when _CLASSIFIER_PROMPT is modified in gap_classifier.py
# without also bumping CLASSIFIER_VERSION.
#
# CLASSIFIER_VERSION propagates into every stub's frontmatter as an audit trail.
# When the prompt changes, bumping the version ensures old classifications can be
# distinguished from new ones and stubs can be re-classified if needed.
#
# Input: JSON on stdin from Claude Code hook system
# Exit 0: allow (warnings emitted on stderr — advisory only)
# Exit 2: block (not used)

set -euo pipefail

INPUT=$(cat)

# Only check files matching **/gap_classifier.py
FILE_PATH=$(echo "$INPUT" | jq -r '.tool_input.file_path // empty')
if [[ -z "$FILE_PATH" ]]; then
	exit 0
fi
if [[ "$FILE_PATH" != */gap_classifier.py ]]; then
	exit 0
fi

TOOL_NAME=$(echo "$INPUT" | jq -r '.tool_name // empty')

# Get old and new content depending on the tool in use.
# Edit: old_string is the text being replaced, new_string is the replacement.
# Write: content is the full new file; compare against what's currently on disk.
if [[ "$TOOL_NAME" == "Edit" ]]; then
	OLD_CONTENT=$(echo "$INPUT" | jq -r '.tool_input.old_string // empty')
	NEW_CONTENT=$(echo "$INPUT" | jq -r '.tool_input.new_string // empty')
else
	# Write tool
	NEW_CONTENT=$(echo "$INPUT" | jq -r '.tool_input.content // empty')
	if [[ -f "$FILE_PATH" ]]; then
		OLD_CONTENT=$(cat "$FILE_PATH")
	else
		OLD_CONTENT=""
	fi
fi

if [[ -z "$NEW_CONTENT" ]]; then
	exit 0
fi

# Check if the edit touches prompt content (lines inside _CLASSIFIER_PROMPT).
# We consider an edit to touch the prompt if either old or new content contains
# text that looks like it's inside the _CLASSIFIER_PROMPT triple-quoted string.
# Heuristic: lines that are inside the prompt are not Python statements
# (they lack "=" assignments, "def ", "class ", "import " prefixes) and
# appear between the _CLASSIFIER_PROMPT assignment and the closing triple-quote.
#
# Simpler approach: check if _CLASSIFIER_PROMPT appears in old_content, OR
# if the old_content contains lines that look like prompt body text (non-empty,
# non-statement lines). We detect by looking for _CLASSIFIER_PROMPT in the
# surrounding context, or by checking if the new_content changes the actual
# prompt text (not just CLASSIFIER_VERSION).
prompt_modified=false

# If _CLASSIFIER_PROMPT is mentioned in the content being changed, it's prompt-adjacent.
if echo "$OLD_CONTENT" | grep -q '_CLASSIFIER_PROMPT'; then
	prompt_modified=true
fi
# Also catch edits that are purely inside the prompt body (no _CLASSIFIER_PROMPT
# assignment visible) by checking if the content contains typical prompt text patterns.
# We look for lines that start with ##, -, or "You are" (common in Claude prompts).
if echo "$OLD_CONTENT" | grep -qE '^(##|You are |[-*] \*\*)'; then
	prompt_modified=true
fi

if [[ "$prompt_modified" == "false" ]]; then
	exit 0
fi

# Check whether CLASSIFIER_VERSION is also being modified in this same edit.
# For Write: check if the new full file has a different CLASSIFIER_VERSION from disk.
# For Edit: check if CLASSIFIER_VERSION appears in either old_string or new_string.
version_bumped=false

if [[ "$TOOL_NAME" == "Edit" ]]; then
	# Edit tool: version is being touched if CLASSIFIER_VERSION appears in the change.
	if echo "$OLD_CONTENT$NEW_CONTENT" | grep -q 'CLASSIFIER_VERSION'; then
		version_bumped=true
	fi
else
	# Write tool: compare the CLASSIFIER_VERSION value in old file vs new content.
	OLD_VERSION=$(echo "$OLD_CONTENT" | grep -oE 'CLASSIFIER_VERSION = "[^"]+"' | head -1 || true)
	NEW_VERSION=$(echo "$NEW_CONTENT" | grep -oE 'CLASSIFIER_VERSION = "[^"]+"' | head -1 || true)
	if [[ -n "$OLD_VERSION" ]] && [[ -n "$NEW_VERSION" ]] && [[ "$OLD_VERSION" != "$NEW_VERSION" ]]; then
		version_bumped=true
	fi
fi

if [[ "$version_bumped" == "false" ]]; then
	cat >&2 <<-EOF
		WARNING: You are modifying _CLASSIFIER_PROMPT in gap_classifier.py without
		bumping CLASSIFIER_VERSION.

		File: $FILE_PATH

		CLASSIFIER_VERSION propagates into every gap stub's frontmatter as an audit
		trail. Bumping it when the prompt changes lets stale classifications be
		identified and re-run. Without a bump, old stub classifications will look
		identical to new ones even though the classification logic changed.

		Please also update CLASSIFIER_VERSION (e.g. bump the @vN suffix) in the
		same edit.
	EOF
fi

exit 0
