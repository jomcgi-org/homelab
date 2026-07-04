#!/bin/bash
# Validate that every hook command registered in .claude/settings.json exists
# on disk and is executable.
#
# Hook scripts are tracked in git. If a script is committed with mode 100644
# (non-executable), every fresh clone or worktree gets a copy that fails with
# exit 126 on the very first PreToolUse invocation, silently and repeatedly,
# since Claude Code hooks fail closed but their stderr is easy to miss in a
# long session. Nothing else in CI checks this, so a bad commit mode ships
# unnoticed until someone notices the hook never fired.
#
# Usage: validate-hooks-executable.sh [repo-root]
#   repo-root defaults to `git rev-parse --show-toplevel`.
#
# Exit 0: every hook command exists and is executable.
# Exit 1: at least one hook command is missing or not executable; every
#         failure is listed (path + problem).

set -euo pipefail

REPO_ROOT="${1:-$(git rev-parse --show-toplevel)}"
SETTINGS="$REPO_ROOT/.claude/settings.json"

if [[ ! -f "$SETTINGS" ]]; then
	echo "ERROR: settings file not found: $SETTINGS" >&2
	exit 1
fi

# Extract every hooks[].hooks[].command across all events (PreToolUse,
# PostToolUse, etc.) and matchers. `.hooks // {}` tolerates a settings.json
# with no hooks configured at all.
COMMANDS=$(jq -r '.hooks // {} | to_entries[] | .value[]? | .hooks[]? | .command // empty' "$SETTINGS")

if [[ -z "$COMMANDS" ]]; then
	exit 0
fi

FAILURES=""
while IFS= read -r command; do
	[[ -z "$command" ]] && continue

	# Commands are plain paths (with $CLAUDE_PROJECT_DIR substitution), not
	# arbitrary shell, so a literal string replace is sufficient here.
	path="${command//\$CLAUDE_PROJECT_DIR/$REPO_ROOT}"

	if [[ ! -e "$path" ]]; then
		FAILURES="${FAILURES}  ${path}: does not exist\n"
	elif [[ ! -x "$path" ]]; then
		FAILURES="${FAILURES}  ${path}: not executable (fix with: chmod +x, then git update-index --chmod=+x)\n"
	fi
done <<<"$COMMANDS"

if [[ -n "$FAILURES" ]]; then
	echo "ERROR: hook command(s) referenced in .claude/settings.json are broken:" >&2
	echo "" >&2
	echo -e "$FAILURES" >&2
	echo "A non-executable or missing hook script fails silently (exit 126) on" >&2
	echo "every matching tool call in every fresh clone or worktree." >&2
	exit 1
fi

exit 0
