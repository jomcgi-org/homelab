#!/bin/bash
# Run format after rebase/amend to catch formatting drift.
# Installed via: pre-commit install --hook-type post-rewrite
set -euo pipefail

# "rebase" or "amend". Default to empty so `set -u` does not abort the hook
# when git or pre-commit invokes it without the positional arg (the non-rebase
# guard below then exits cleanly).
command="${1:-}"

# Only run after rebase (amend already triggers pre-commit)
if [ "$command" != "rebase" ]; then
	exit 0
fi

cd "$(git rev-parse --show-toplevel)"

echo "Running format after rebase..."
if command -v format >/dev/null; then
	format
else
	bazel/tools/format/fast-format.sh
fi

if ! git diff --quiet; then
	echo ""
	echo "⚠️  Format found changes after rebase. Stage and amend:"
	echo "   git add -u && git commit --amend --no-edit"
	echo ""
	git diff --stat
fi
