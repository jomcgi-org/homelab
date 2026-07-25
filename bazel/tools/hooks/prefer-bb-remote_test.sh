#!/usr/bin/env bash
# Unit tests for prefer-bb-remote.sh PreToolUse hook.
#
# The hook:
#   - Reads JSON from stdin with .tool_input.command
#   - Exits 0 when the command is allowed (bb, git, format, no-bazel, etc.)
#   - Exits 2 and prints BLOCKED to stderr for direct bazel/bazelisk invocations
#
# This test mocks jq via a minimal Python3 stub placed earlier on PATH so
# the hook can run in the hermetic Bazel sandbox.

set -euo pipefail

# ---------------------------------------------------------------------------
# Locate hook from Bazel runfiles
# ---------------------------------------------------------------------------
HOOK_REL="bazel/tools/hooks/prefer-bb-remote.sh"
HOOK=""
for candidate in \
	"${RUNFILES_DIR:-}/_main/${HOOK_REL}" \
	"${TEST_SRCDIR:-}/_main/${HOOK_REL}" \
	"${BASH_SOURCE[0]%/*}/prefer-bb-remote.sh"; do
	if [[ -f "$candidate" ]]; then
		HOOK="$candidate"
		break
	fi
done
if [[ -z "$HOOK" ]]; then
	echo "ERROR: cannot locate prefer-bb-remote.sh in runfiles" >&2
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
"""Minimal jq stub covering the expression used by prefer-bb-remote.sh."""
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
    pass  # empty — print nothing
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

# 7. format command (legacy) → allowed
run_test "format_command_allowed" \
	'{"tool_input":{"command":"format"}}' \
	0 ""

# 8. format with flags → allowed
run_test "format_with_flags_allowed" \
	'{"tool_input":{"command":"format --check"}}' \
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
	'{"tool_input":{"command":"cd /tmp/claude-worktrees/wt && bazel/tools/format/fast-format.sh"}}' \
	0 ""

# 14. Semicolon-chained repo-relative script path under bazel/ → allowed
run_test "semicolon_bazel_dir_script_allowed" \
	'{"tool_input":{"command":"echo done; bazel/tools/hooks/validate-hooks-executable.sh"}}' \
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
