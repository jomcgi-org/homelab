#!/bin/bash
# PreToolUse hook: blocks direct bazel/bazelisk invocations.
# All real Bazel work happens in CI (push the branch). Locally, use `format`
# for formatting, gazelle, and apko lockfile updates; use the
# `mcp__buildbuddy__*` tools for inspecting CI runs.
#
# (Filename retained for backwards-compat with .claude/settings.json and
# the BUILD/test wiring; rename is a follow-up cleanup.)
#
# Input: JSON on stdin from Claude Code hook system
# Exit 0: allow the command
# Exit 2: block the command (reason shown to Claude)

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

# Allow format command (runs gazelle which wraps bazel internally)
if [[ "$COMMAND" == format* ]]; then
	exit 0
fi

# Allow bb commands (already using BuildBuddy CLI)
if [[ "$COMMAND" == bb\ * ]] || [[ "$COMMAND" == */bb\ * ]]; then
	exit 0
fi

# Block direct bazel/bazelisk invocations. The chained patterns require a
# trailing space so repo-relative script paths under bazel/ (e.g.
# "cd /tmp/wt && bazel/tools/format/fast-format.sh") are not false positives.
if [[ "$COMMAND" == bazel\ * ]] || [[ "$COMMAND" == bazelisk\ * ]] || [[ "$COMMAND" == *"&& bazel "* ]] || [[ "$COMMAND" == *"; bazel "* ]]; then
	cat >&2 <<-'EOF'
		BLOCKED: Direct bazel/bazelisk invocations are not allowed locally.

		All Bazel work happens in CI:
		  - Tests / builds / image pushes: commit + push the branch, watch via
		    `gh pr checks <number> --watch`.
		  - Apko lockfile updates: run `format` (regenerates all locks via
		    bazel internally; allowed by this hook).
		  - Gazelle / BUILD file generation: also part of `format`.

		For CI debugging, use the BuildBuddy MCP tools:
		  mcp__buildbuddy__get_invocation   # Look up by commitSha or invocationId
		  mcp__buildbuddy__get_target       # Find failing targets in an invocation
		  mcp__buildbuddy__get_log          # Read the build/test log
		  mcp__buildbuddy__get_file_range   # Range-read CAS blob artifacts
	EOF
	exit 2
fi

exit 0
