#!/usr/bin/env bash
# Unit tests for check-ci-pipe-mask.sh PreToolUse hook.
#
# The hook:
#   - Reads JSON from stdin with .tool_input.command
#   - Exits 0 when ci or bb remote output is preserved
#   - Exits 2 and prints BLOCKED to stderr when output is masked or discarded
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
locate_hook check-ci-pipe-mask.sh
install_jq_stub

# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------
run_test "ci_test_allowed" \
	'{"tool_input":{"command":"ci test"}}' \
	0 ""

run_test "ci_test_tee_allowed" \
	'{"tool_input":{"command":"ci test 2>&1 | tee /tmp/ci.log"}}' \
	0 ""

run_test "ci_test_file_redirect_allowed" \
	'{"tool_input":{"command":"ci test > /tmp/ci.log 2>&1"}}' \
	0 ""

run_test "git_log_tail_allowed" \
	'{"tool_input":{"command":"git log | tail -5"}}' \
	0 ""

run_test "tail_log_allowed" \
	'{"tool_input":{"command":"tail -f /var/log/foo"}}' \
	0 ""

run_test "echo_ci_tail_allowed" \
	'{"tool_input":{"command":"echo ci | tail"}}' \
	0 ""

run_test "git_commit_ci_message_allowed" \
	'{"tool_input":{"command":"git commit -m \"ci: fix thing\""}}' \
	0 ""

run_test "ci_test_tail_blocked" \
	'{"tool_input":{"command":"ci test 2>&1 | tail -20"}}' \
	2 "BLOCKED"

run_test "ci_test_head_blocked" \
	'{"tool_input":{"command":"ci test | head"}}' \
	2 "BLOCKED"

run_test "ci_script_tail_blocked" \
	'{"tool_input":{"command":"bazel/tools/ci/ci test | tail -5"}}' \
	2 "BLOCKED"

run_test "ci_test_grep_blocked" \
	'{"tool_input":{"command":"ci test | grep -q FAILED"}}' \
	2 "BLOCKED"

run_test "ci_test_dev_null_blocked" \
	'{"tool_input":{"command":"ci test >/dev/null 2>&1"}}' \
	2 "BLOCKED"

run_test "bb_remote_tail_blocked" \
	'{"tool_input":{"command":"bb remote test //... 2>&1 | tail"}}' \
	2 "BLOCKED"

run_test "ci_lint_head_blocked" \
	'{"tool_input":{"command":"ci lint | head -3"}}' \
	2 "BLOCKED"

run_test "empty_json_allowed" \
	'{}' \
	0 ""

run_test "ci_chain_git_pipe_allowed" \
	'{"tool_input":{"command":"ci lint && git status --short | head -20"}}' \
	0 ""

run_test "ci_tee_filter_allowed" \
	'{"tool_input":{"command":"ci test 2>&1 | tee /tmp/ci.log | grep -E \"Executed|FAILED\""}}' \
	0 ""

run_test "ci_tee_then_grep_allowed" \
	'{"tool_input":{"command":"ci test 2>&1 | tee /tmp/ci.log; grep Executed /tmp/ci.log | head -1"}}' \
	0 ""

run_test "bb_remote_allowed" \
	'{"tool_input":{"command":"bb remote test //... --config=ci"}}' \
	0 ""

run_test "gh_pr_body_allowed" \
	'{"tool_input":{"command":"gh pr create --body \"never run ci test | tail -20\""}}' \
	0 ""

run_test "git_commit_pipe_message_allowed" \
	'{"tool_input":{"command":"git commit -m \"docs: ci test | tail is banned\""}}' \
	0 ""

run_test "ci_then_gh_pipe_allowed" \
	'{"tool_input":{"command":"ci && gh pr checks 4394 | grep -i fail"}}' \
	0 ""

run_test "ci_sed_blocked" \
	'{"tool_input":{"command":"ci test | sed -n '\''1,20p'\''"}}' \
	2 "BLOCKED"

run_test "ci_awk_blocked" \
	'{"tool_input":{"command":"ci test | awk '\''{print $1}'\''"}}' \
	2 "BLOCKED"

run_test "ci_wc_blocked" \
	'{"tool_input":{"command":"ci test | wc -l"}}' \
	2 "BLOCKED"

run_test "ci_pipe_amp_tail_blocked" \
	'{"tool_input":{"command":"ci test |& tail -20"}}' \
	2 "BLOCKED"

run_test "timeout_ci_tail_blocked" \
	'{"tool_input":{"command":"timeout 900 ci test | tail -20"}}' \
	2 "BLOCKED"

run_test "env_ci_tail_blocked" \
	'{"tool_input":{"command":"CI=1 ci test | tail -5"}}' \
	2 "BLOCKED"

run_test "parenthesized_ci_tail_blocked" \
	'{"tool_input":{"command":"( ci test ) | tail -20"}}' \
	2 "BLOCKED"

run_test "ci_tee_dev_null_tail_blocked" \
	'{"tool_input":{"command":"ci test 2>&1 | tee /dev/null | tail -3"}}' \
	2 "BLOCKED"

run_test "git_chain_ci_tail_blocked" \
	'{"tool_input":{"command":"git add -A && ci test | tail -20"}}' \
	2 "BLOCKED"

run_test "gh_chain_ci_tail_blocked" \
	'{"tool_input":{"command":"gh pr checkout 4394 && ci test | tail -20"}}' \
	2 "BLOCKED"

run_test "ci_tee_dev_null_blocked" \
	'{"tool_input":{"command":"ci test | tee /dev/null"}}' \
	2 "BLOCKED"

run_test "nohup_ci_tail_blocked" \
	'{"tool_input":{"command":"nohup ci test | tail -20"}}' \
	2 "BLOCKED"

run_test "time_ci_tail_blocked" \
	'{"tool_input":{"command":"time ci test | tail -3"}}' \
	2 "BLOCKED"

run_test "timeout_flags_ci_tail_blocked" \
	'{"tool_input":{"command":"timeout -k 10 900 ci test | tail -20"}}' \
	2 "BLOCKED"

run_test "git_commit_message_prose_allowed" \
	'{"tool_input":{"command":"git commit -m \"never pipe ci test | tail\""}}' \
	0 ""

run_test "gh_body_prose_allowed" \
	'{"tool_input":{"command":"gh pr create --body \"ci test | tail -20 is banned\""}}' \
	0 ""

run_test "git_prose_chain_allowed" \
	'{"tool_input":{"command":"git add -A && git commit -m \"ci: fix\" && git push"}}' \
	0 ""

finish_tests
