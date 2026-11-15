# Shared helpers for the PreToolUse hook sh_tests. Each test sources this
# file, calls locate_hook and install_jq_stub, then runs its cases and calls
# finish_tests.

PASS=0
FAIL=0

# locate_hook <file>: set HOOK to bazel/tools/hooks/<file> from Bazel runfiles.
locate_hook() {
	local rel="bazel/tools/hooks/$1" candidate
	HOOK=""
	for candidate in \
		"${RUNFILES_DIR:-}/_main/${rel}" \
		"${TEST_SRCDIR:-}/_main/${rel}" \
		"${BASH_SOURCE[0]%/*}/$1"; do
		if [[ -f "$candidate" ]]; then
			HOOK="$candidate"
			return 0
		fi
	done
	echo "ERROR: cannot locate $1 in runfiles" >&2
	exit 1
}

# install_jq_stub: put a minimal Python3 jq first on PATH so the hook runs in
# the hermetic Bazel sandbox. It covers '.a.b // .c.d // empty' expressions.
install_jq_stub() {
	mkdir -p "${TEST_TMPDIR}/bin"
	cat >"${TEST_TMPDIR}/bin/jq" <<'JQ_STUB'
#!/usr/bin/env python3
"""Minimal jq stub covering the expressions used by the hooks."""
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
    pass  # empty: print nothing
elif raw:
    print(result)
else:
    print(json.dumps(result))
JQ_STUB
	chmod +x "${TEST_TMPDIR}/bin/jq"
	export PATH="${TEST_TMPDIR}/bin:${PATH}"
}

# make_write_json <file_path> <content>: JSON for a Write tool call.
make_write_json() {
	local fp="$1" content="$2"
	python3 - "$fp" "$content" <<'PY'
import json, sys
fp, content = sys.argv[1], sys.argv[2]
print(json.dumps({"tool_name": "Write", "tool_input": {"file_path": fp, "content": content}}))
PY
}

# make_edit_json <file_path> [old_string] <new_string>: JSON for an Edit tool
# call. old_string is included only when given.
make_edit_json() {
	python3 - "$@" <<'PY'
import json, sys
args = sys.argv[1:]
tool_input = {"file_path": args[0]}
if len(args) == 3:
    tool_input["old_string"] = args[1]
tool_input["new_string"] = args[-1]
print(json.dumps({"tool_name": "Edit", "tool_input": tool_input}))
PY
}

# run_test <name> <json> <want_exit> <stderr_re>: pipe json into $HOOK and
# check its exit code and stderr. An empty stderr_re means no stderr expected.
run_test() {
	local name="$1"
	local input_json="$2"
	local want_exit="$3"
	local want_stderr_re="$4"

	local stderr_out
	local got_exit=0
	stderr_out=$(printf '%s' "$input_json" | bash "$HOOK" 2>&1 >/dev/null) || got_exit=$?

	local ok=true

	if [[ "$got_exit" -ne "$want_exit" ]]; then
		echo "FAIL [$name]: exit $got_exit, want $want_exit"
		ok=false
	fi

	if [[ -n "$want_stderr_re" ]]; then
		if ! printf '%s\n' "$stderr_out" | grep -qE "$want_stderr_re"; then
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

# finish_tests: print the summary and exit 1 when any case failed.
finish_tests() {
	echo ""
	echo "Results: ${PASS} passed, ${FAIL} failed"
	if [[ "$FAIL" -gt 0 ]]; then
		exit 1
	fi
	exit 0
}
