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
# Locate hook from Bazel runfiles
# ---------------------------------------------------------------------------
HOOK_REL="bazel/tools/hooks/check-ci-pipe-mask.sh"
HOOK=""
for candidate in \
	"${RUNFILES_DIR:-}/_main/${HOOK_REL}" \
	"${TEST_SRCDIR:-}/_main/${HOOK_REL}" \
	"${BASH_SOURCE[0]%/*}/check-ci-pipe-mask.sh"; do
	if [[ -f "$candidate" ]]; then
		HOOK="$candidate"
		break
	fi
done
if [[ -z "$HOOK" ]]; then
	echo "ERROR: cannot locate check-ci-pipe-mask.sh in runfiles" >&2
	exit 1
fi

# ---------------------------------------------------------------------------
# Install a minimal jq stub so the hook runs in the hermetic sandbox.
# The hook uses exactly one expression:
#   jq -r '.tool_input.command // empty'
# ---------------------------------------------------------------------------
mkdir -p "${TEST_TMPDIR}/bin"
cat >"${TEST_TMPDIR}/bin/jq" <<'JQ_STUB'
#!/usr/bin/env python3
"""Minimal jq stub covering the expression used by check-ci-pipe-mask.sh."""
import json, sys

args = sys.argv[1:]
raw = False
if args and args[0] == "-r":
    raw = True
    args = args[1:]

expr = args[0] if args else "."
data = json.load(sys.stdin)

def jq_eval(obj, expr):
    """Evaluate '.a.b // .c.d // empty' style expressions."""
    for alt in expr.split("//"):
        alt = alt.strip()
        if alt == "empty":
            return None
        keys = [k for k in alt.lstrip(".").split(".") if k]
        val = obj
        try:
            for k in keys:
                val = val[k] if isinstance(val, dict) else None
                if val is None:
                    break
        except (KeyError, TypeError):
            val = None
        if val is not None:
            return val
    return None

result = jq_eval(data, expr)
if result is None:
    pass  # empty, print nothing
elif raw:
    print(result)
else:
    print(json.dumps(result))
JQ_STUB
chmod +x "${TEST_TMPDIR}/bin/jq"
export PATH="${TEST_TMPDIR}/bin:${PATH}"

# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------
PASS=0
FAIL=0

run_test() {
	local name="$1"
	local input_json="$2"
	local want_exit="$3"      # expected exit code (0=allow, 2=block)
	local want_stderr_re="$4" # regex that must match stderr (empty = no output expected)

	local stderr_out
	local got_exit=0
	stderr_out=$(printf '%s' "$input_json" | bash "$HOOK" 2>&1 >/dev/null) || got_exit=$?

	local ok=true

	if [[ "$got_exit" -ne "$want_exit" ]]; then
		echo "FAIL [$name]: exit $got_exit, want $want_exit"
		ok=false
	fi

	if [[ -n "$want_stderr_re" ]]; then
		if ! echo "$stderr_out" | grep -qE "$want_stderr_re"; then
			echo "FAIL [$name]: stderr $(printf '%q' "$stderr_out") did not match /$want_stderr_re/"
			ok=false
		fi
	else
		if [[ -n "$stderr_out" ]]; then
			echo "FAIL [$name]: unexpected stderr: $(printf '%q' "$stderr_out")"
			ok=false
		fi
	fi

	if $ok; then
		echo "PASS [$name]"
		PASS=$((PASS + 1))
	else
		FAIL=$((FAIL + 1))
	fi
}

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

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
echo ""
echo "Results: ${PASS} passed, ${FAIL} failed"
if [[ "$FAIL" -gt 0 ]]; then
	exit 1
fi
exit 0
