#!/usr/bin/env bash
# Unit tests for check-asyncio-mark-build-dep.sh PreToolUse hook.
#
# The hook:
#   - Reads JSON from stdin with .tool_input.file_path (and optionally .tool_input.content)
#   - Exits 0 always (warning-only, never blocks)
#   - Emits a WARNING on stderr when a *_test.py file contains @pytest.mark.asyncio
#     or async def test_ without @pip//pytest_asyncio in the nearest BUILD file
#   - Skips non-*_test.py files and files with no asyncio markers

set -euo pipefail

# ---------------------------------------------------------------------------
# Locate hook from Bazel runfiles
# ---------------------------------------------------------------------------
HOOK_REL="bazel/tools/hooks/check-asyncio-mark-build-dep.sh"
HOOK=""
for candidate in \
	"${RUNFILES_DIR:-}/_main/${HOOK_REL}" \
	"${TEST_SRCDIR:-}/_main/${HOOK_REL}" \
	"${BASH_SOURCE[0]%/*}/check-asyncio-mark-build-dep.sh"; do
	if [[ -f "$candidate" ]]; then
		HOOK="$candidate"
		break
	fi
done
if [[ -z "$HOOK" ]]; then
	echo "ERROR: cannot locate check-asyncio-mark-build-dep.sh in runfiles" >&2
	exit 1
fi

# ---------------------------------------------------------------------------
# Install a minimal jq stub so the hook runs in the hermetic sandbox.
# The hook uses exactly two expressions:
#   jq -r '.tool_input.file_path // empty'
#   jq -r '.tool_input.content // empty'
# ---------------------------------------------------------------------------
mkdir -p "${TEST_TMPDIR}/bin"
cat >"${TEST_TMPDIR}/bin/jq" <<'JQ_STUB'
#!/usr/bin/env python3
"""Minimal jq stub covering expressions used by check-asyncio-mark-build-dep.sh."""
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
# Helper: build a JSON payload with content inline (Write-style)
# ---------------------------------------------------------------------------
make_json() {
	local file_path="$1"
	local content="$2"
	# Use python3 to safely encode the content as a JSON string
	python3 -c "
import json, sys
print(json.dumps({'tool_input': {'file_path': sys.argv[1], 'content': sys.argv[2]}}))" \
		"$file_path" "$content"
}

# ---------------------------------------------------------------------------
# Test 1: _test.py with @pytest.mark.asyncio, BUILD missing dep → warns
# ---------------------------------------------------------------------------
mkdir -p "${TEST_TMPDIR}/t1"
cat >"${TEST_TMPDIR}/t1/BUILD" <<'EOF'
py_test(
    name = "test_suite",
    srcs = ["foo_test.py"],
    deps = ["@pip//pytest"],
)
EOF
run_test "asyncio_mark_missing_dep_warns" \
	"$(make_json "${TEST_TMPDIR}/t1/foo_test.py" "
import pytest

@pytest.mark.asyncio
async def test_something():
    pass
")" \
	0 "WARNING.*pytest_asyncio"

# ---------------------------------------------------------------------------
# Test 2: _test.py with @pytest.mark.asyncio, BUILD has dep → no warning
# ---------------------------------------------------------------------------
mkdir -p "${TEST_TMPDIR}/t2"
cat >"${TEST_TMPDIR}/t2/BUILD" <<'EOF'
py_test(
    name = "test_suite",
    srcs = ["foo_test.py"],
    deps = ["@pip//pytest", "@pip//pytest_asyncio"],
)
EOF
run_test "asyncio_mark_has_dep_no_warning" \
	"$(make_json "${TEST_TMPDIR}/t2/foo_test.py" "
import pytest

@pytest.mark.asyncio
async def test_something():
    pass
")" \
	0 ""

# ---------------------------------------------------------------------------
# Test 3: _test.py with async def test_, BUILD missing dep → warns
# ---------------------------------------------------------------------------
mkdir -p "${TEST_TMPDIR}/t3"
cat >"${TEST_TMPDIR}/t3/BUILD" <<'EOF'
py_test(
    name = "test_suite",
    srcs = ["bar_test.py"],
    deps = ["@pip//pytest"],
)
EOF
run_test "async_def_test_missing_dep_warns" \
	"$(make_json "${TEST_TMPDIR}/t3/bar_test.py" "
import asyncio

async def test_something():
    await asyncio.sleep(0)
")" \
	0 "WARNING.*pytest_asyncio"

# ---------------------------------------------------------------------------
# Test 4: plain def test_ using asyncio.run() → no warning
# ---------------------------------------------------------------------------
mkdir -p "${TEST_TMPDIR}/t4"
cat >"${TEST_TMPDIR}/t4/BUILD" <<'EOF'
py_test(
    name = "test_suite",
    srcs = ["baz_test.py"],
    deps = ["@pip//pytest"],
)
EOF
run_test "plain_def_asyncio_run_no_warning" \
	"$(make_json "${TEST_TMPDIR}/t4/baz_test.py" "
import asyncio

async def _do_thing():
    await asyncio.sleep(0)

def test_something():
    asyncio.run(_do_thing())
")" \
	0 ""

# ---------------------------------------------------------------------------
# Test 5: non-_test.py file → skip even if it has asyncio markers
# ---------------------------------------------------------------------------
mkdir -p "${TEST_TMPDIR}/t5"
run_test "non_test_py_skipped" \
	"$(make_json "${TEST_TMPDIR}/t5/helper.py" "
import pytest

@pytest.mark.asyncio
async def test_something():
    pass
")" \
	0 ""

# ---------------------------------------------------------------------------
# Test 6: empty JSON (no tool_input) → skip
# ---------------------------------------------------------------------------
run_test "empty_json_allowed" \
	'{}' \
	0 ""

# ---------------------------------------------------------------------------
# Test 7: Edit tool (no content field) — reads existing file on disk
# ---------------------------------------------------------------------------
mkdir -p "${TEST_TMPDIR}/t7"
cat >"${TEST_TMPDIR}/t7/BUILD" <<'EOF'
py_test(
    name = "test_suite",
    srcs = ["edit_test.py"],
    deps = ["@pip//pytest"],
)
EOF
cat >"${TEST_TMPDIR}/t7/edit_test.py" <<'EOF'
import pytest

@pytest.mark.asyncio
async def test_via_edit():
    pass
EOF
run_test "edit_tool_reads_file_warns" \
	"{\"tool_input\":{\"file_path\":\"${TEST_TMPDIR}/t7/edit_test.py\"}}" \
	0 "WARNING.*pytest_asyncio"

# ---------------------------------------------------------------------------
# Test 8: BUILD in parent directory (walk-up) — no dep → warns
# ---------------------------------------------------------------------------
mkdir -p "${TEST_TMPDIR}/t8/sub"
cat >"${TEST_TMPDIR}/t8/BUILD" <<'EOF'
py_test(
    name = "test_suite",
    srcs = glob(["**/*_test.py"]),
    deps = ["@pip//pytest"],
)
EOF
run_test "build_in_parent_dir_warns" \
	"$(make_json "${TEST_TMPDIR}/t8/sub/nested_test.py" "
import pytest

@pytest.mark.asyncio
async def test_nested():
    pass
")" \
	0 "WARNING.*pytest_asyncio"

# ---------------------------------------------------------------------------
# Test 9: _test.py with no asyncio markers → no warning
# ---------------------------------------------------------------------------
mkdir -p "${TEST_TMPDIR}/t9"
cat >"${TEST_TMPDIR}/t9/BUILD" <<'EOF'
py_test(
    name = "test_suite",
    srcs = ["plain_test.py"],
    deps = ["@pip//pytest"],
)
EOF
run_test "no_asyncio_no_warning" \
	"$(make_json "${TEST_TMPDIR}/t9/plain_test.py" "
def test_something():
    assert 1 + 1 == 2
")" \
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
