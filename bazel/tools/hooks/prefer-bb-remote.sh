#!/bin/bash
# PreToolUse hook: blocks direct bazel/bazelisk invocations on the Mac.
#
# Local verification is `ci` (bazel/tools/ci/ci): selective lint/regen, then
# `bb remote` Linux tests (BuildBuddy Remote Bazel, same backend as Workflows).
# Direct `bazel` on darwin has no workflow executors and is the wrong loop.
#
# Exit 0: allow; exit 2: block (reason on stderr).

set -euo pipefail

INPUT=$(cat)
COMMAND=$(echo "$INPUT" | jq -r '.tool_input.command // empty')

# Only check commands that contain bazel
if [[ ! "$COMMAND" == *bazel* ]]; then
	exit 0
fi

# Allow git commands (commit messages may mention bazel)
if [[ "$COMMAND" == git\ * ]]; then
	exit 0
fi

# Allow the unified local gate
if [[ "$COMMAND" == ci\ * ]] || [[ "$COMMAND" == ci ]] || [[ "$COMMAND" == */ci\ * ]] || [[ "$COMMAND" == */ci ]]; then
	exit 0
fi

# Allow format / fast-format (legacy; prefer `ci lint`)
if [[ "$COMMAND" == format* ]] || [[ "$COMMAND" == *fast-format.sh* ]]; then
	exit 0
fi

# Allow bb commands (BuildBuddy CLI, including `bb remote`)
if [[ "$COMMAND" == bb\ * ]] || [[ "$COMMAND" == */bb\ * ]]; then
	exit 0
fi

# Block direct bazel/bazelisk. Trailing space so paths under bazel/ are ok.
if [[ "$COMMAND" == bazel\ * ]] || [[ "$COMMAND" == bazelisk\ * ]] || [[ "$COMMAND" == *"&& bazel "* ]] || [[ "$COMMAND" == *"; bazel "* ]]; then
	cat >&2 <<-'EOF'
		BLOCKED: Direct bazel/bazelisk on the workstation is not the feedback loop.

		Use the unified gate (BuildBuddy Remote Bazel under the hood):
		  ci              # lint changed files + selective regen + bb remote test
		  ci lint         # format only files changed vs origin/main
		  ci regen        # generators/gazelle only when inputs changed
		  ci test         # bb remote --os=linux --arch=amd64 test //... --config=ci

		PR Workflows use the same remote cache; a green `ci test` should make
		the PR "Test" action mostly cache-hit.

		Inspect CI: mcp__buildbuddy__get_invocation (commitSha or invocationId)
	EOF
	exit 2
fi

exit 0
