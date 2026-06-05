#!/usr/bin/env bash
# Unit tests for check-bare-nosemgrep.sh PreToolUse hook.
#
# The hook:
#   - Reads JSON from stdin with .tool_name, .tool_input.file_path, and either
#     .tool_input.new_string (Edit) or .tool_input.content (Write)
#   - Exits 0 always (warning-only, never blocks)
#   - Emits a WARNING on stderr when content contains a bare ``# nosemgrep``
#     suppression (no colon + rule-id following)
#   - Allows ``# nosemgrep: rule-id`` suppressions silently

set -euo pipefail

# ---------------------------------------------------------------------------
# Locate hook from Bazel runfiles
# ---------------------------------------------------------------------------
HOOK_REL="bazel/tools/hooks/check-bare-nosemgrep.sh"
HOOK=""
for candidate in \
	"${RUNFILES_DIR:-}/_main/${HOOK_REL}" \
	"${TEST_SRCDIR:-}/_main/${HOOK_REL}" \
	"${BASH_SOURCE[0]%/*}/check-bare-nosemgrep.sh"; do
	if [[ -f "$candidate" ]]; then
		HOOK="$candidate"
		break
	fi
done
if [[ -z "$HOOK" ]]; then
	echo "ERROR: cannot locate check-bare-nosemgrep.sh in runfiles" >&2
	exit 1
fi

# ---------------------------------------------------------------------------
# Install a minimal jq stub so the hook runs in the hermetic Bazel sandbox.
# The hook uses:
#   jq -r '.tool_name // empty'
#   jq -r '.tool_input.new_string // empty'
#   jq -r '.tool_input.content // empty'
#   jq -r '.tool_input.file_path // empty'
# ---------------------------------------------------------------------------
mkdir -p "${TEST_TMPDIR}/bin"
cat >"${TEST_TMPDIR}/bin/jq" <<'JQ_STUB'
#!/usr/bin/env python3
"""Minimal jq stub covering expressions used by check-bare-nosemgrep.sh."""
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
	local want_exit="$3"      # expected exit code (always 0 for this hook)
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
# Helper to build JSON inputs
# ---------------------------------------------------------------------------
make_write_json() {
	local fp="$1" content="$2"
	python3 - "$fp" "$content" <<'PY'
import json, sys
fp, content = sys.argv[1], sys.argv[2]
print(json.dumps({"tool_name": "Write", "tool_input": {"file_path": fp, "content": content}}))
PY
}

make_edit_json() {
	local fp="$1" new_str="$2"
	python3 - "$fp" "$new_str" <<'PY'
import json, sys
fp, new_str = sys.argv[1], sys.argv[2]
print(json.dumps({"tool_name": "Edit", "tool_input": {"file_path": fp, "new_string": new_str}}))
PY
}

# ---------------------------------------------------------------------------
# Test cases
# ---------------------------------------------------------------------------

# 1. Bare # nosemgrep in Write content → warns
run_test "write_bare_nosemgrep_warns" \
	"$(make_write_json "src/foo.py" 'x = dangerous()  # nosemgrep')" \
	0 "WARNING.*[Bb]are.*nosemgrep|nosemgrep.*[Ww]arning"

# 2. Scoped # nosemgrep: rule-id in Write content → silent
run_test "write_scoped_nosemgrep_silent" \
	"$(make_write_json "src/foo.py" 'x = dangerous()  # nosemgrep: security.rule-name')" \
	0 ""

# 3. Bare # nosemgrep in Edit new_string → warns
run_test "edit_bare_nosemgrep_warns" \
	"$(make_edit_json "src/bar.py" 'y = risky()  # nosemgrep')" \
	0 "WARNING"

# 4. Scoped # nosemgrep: rule-id in Edit new_string → silent
run_test "edit_scoped_nosemgrep_silent" \
	"$(make_edit_json "src/bar.py" 'y = risky()  # nosemgrep: python.lang.security.audit.dangerous-spawn-process.dangerous-spawn-process')" \
	0 ""

# 5. # nosemgrep with trailing whitespace (still bare) → warns
run_test "bare_nosemgrep_trailing_space_warns" \
	"$(make_write_json "src/baz.py" "z = unsafe()  # nosemgrep   ")" \
	0 "WARNING"

# 6. Content with no nosemgrep at all → silent
run_test "no_nosemgrep_silent" \
	"$(make_write_json "src/clean.py" 'x = safe_function()')" \
	0 ""

# 7. Empty content → silent (no warning)
run_test "empty_content_silent" \
	"$(make_write_json "src/empty.py" "")" \
	0 ""

# 8. Empty JSON (no tool_input) → silent
run_test "empty_json_silent" \
	'{}' \
	0 ""

# 9. Multiple bare suppressions — all reported in one warning block
run_test "multiple_bare_nosemgrep_warns" \
	"$(make_write_json "src/multi.py" 'a = x()  # nosemgrep
b = y()  # nosemgrep: scoped.rule
c = z()  # nosemgrep')" \
	0 "WARNING"

# 10. Scoped suppression: colon with no rule-id after it does NOT count as bare
# (the hook checks only for lines where nosemgrep ends at whitespace/EOL)
run_test "colon_only_is_still_scoped" \
	"$(make_write_json "src/colon.py" 'x = fn()  # nosemgrep:')" \
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
