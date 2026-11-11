#!/usr/bin/env bash
# Unit tests for prefer-bb-remote.sh PreToolUse hook.
#
# The hook:
#   - Reads JSON from stdin with .tool_input.command
#   - Exits 0 when the command is allowed (bb, git, ci, no-bazel, etc.)
#   - Exits 2 and prints BLOCKED to stderr for direct bazel/bazelisk invocations
#
# This test mocks jq via a minimal Python3 stub placed earlier on PATH so
# the hook can run in the hermetic Bazel sandbox.

set -euo pipefail

# ---------------------------------------------------------------------------
# Locate the shared test helpers and the hook from Bazel runfiles
# ---------------------------------------------------------------------------
TESTLIB=""
for candidate in \
	"${RUNFILES_DIR:-}/_main/bazel/tools/hooks/testlib.sh" \
	"${TEST_SRCDIR:-}/_main/bazel/tools/hooks/testlib.sh" \
	"${BASH_SOURCE[0]%/*}/testlib.sh"; do
	if [[ -f "$candidate" ]]; then
		TESTLIB="$candidate"
		break
	fi
done
# shellcheck source=bazel/tools/hooks/testlib.sh
source "${TESTLIB:?cannot locate testlib.sh in runfiles}"
locate_hook prefer-bb-remote.sh
install_jq_stub

# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

# 1. Command with no bazel mention → allowed immediately
run_test "non_bazel_command_allowed" \
	'{"tool_input":{"command":"kubectl get pods"}}' \
	0 ""

# 2. Direct bazel invocation → blocked
run_test "direct_bazel_blocked" \
	'{"tool_input":{"command":"bazel test //..."}}' \
	2 "BLOCKED"

# 3. bazelisk invocation → blocked
run_test "bazelisk_blocked" \
	'{"tool_input":{"command":"bazelisk build //path/to:target"}}' \
	2 "BLOCKED"

# 4. bb remote → allowed (already using BuildBuddy CLI)
run_test "bb_remote_allowed" \
	'{"tool_input":{"command":"bb remote test //..."}}' \
	0 ""

# 5. Absolute-path bb → allowed (*/bb pattern)
run_test "absolute_path_bb_allowed" \
	'{"tool_input":{"command":"/usr/local/bin/bb remote build //path/to:target"}}' \
	0 ""

# 6. git commit whose message mentions bazel → allowed
run_test "git_commit_mentioning_bazel_allowed" \
	'{"tool_input":{"command":"git commit -m \"fix bazel build\""}}' \
	0 ""

# 7. Bare unified gate is allowed
run_test "bare_ci_allowed" \
	'{"tool_input":{"command":"ci"}}' \
	0 ""

# 8. Absolute-path gate with a Bazel directory exercises the explicit allowance
run_test "absolute_ci_allowed" \
	'{"tool_input":{"command":"/workspace/bazel/tools/ci/ci test"}}' \
	0 ""

# 8b. unified `ci` gate → allowed (may appear with bazel in script path only)
run_test "ci_command_allowed" \
	'{"tool_input":{"command":"ci test"}}' \
	0 ""

run_test "ci_script_path_allowed" \
	'{"tool_input":{"command":"bazel/tools/ci/ci lint"}}' \
	0 ""
# 9. cat a file whose path contains "bazel" → allowed (not a bazel invocation)
run_test "cat_file_with_bazel_in_path_allowed" \
	'{"tool_input":{"command":"cat some/bazel/BUILD"}}' \
	0 ""

# 10. bazel after && in chained command → blocked
run_test "bazel_after_double_ampersand_blocked" \
	'{"tool_input":{"command":"echo done && bazel test //..."}}' \
	2 "BLOCKED"

# 11. bazel after ; in chained command → blocked
run_test "bazel_after_semicolon_blocked" \
	'{"tool_input":{"command":"echo done; bazel test //..."}}' \
	2 "BLOCKED"

# 12. Empty JSON object (no tool_input) → allowed
run_test "empty_json_allowed" \
	'{}' \
	0 ""

# 13. Chained repo-relative script path under bazel/ → allowed (not a bazel
# invocation; the block patterns require "bazel " with a trailing space)
run_test "chained_bazel_dir_script_allowed" \
	'{"tool_input":{"command":"cd /tmp/claude-worktrees/wt && bazel/tools/format/run-generators.sh"}}' \
	0 ""

# 14. Semicolon-chained repo-relative script path under bazel/ → allowed
run_test "semicolon_bazel_dir_script_allowed" \
	'{"tool_input":{"command":"echo done; bazel/tools/hooks/validate-hooks-executable.sh"}}' \
	0 ""

finish_tests
