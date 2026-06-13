#!/usr/bin/env bash
# Unit tests for check-lifespan-startup-mock-sync.sh PreToolUse hook.
#
# The hook:
#   - Only fires on files matching */projects/monolith/app/main.py
#   - For Write: scans .tool_input.content for <domain>.on_startup_jobs(session) calls
#   - For Edit: scans .tool_input.new_string for <domain>.on_startup_jobs(session) calls
#   - Collects all domains patched in adjacent main_*_test.py files
#   - Warns (stderr) if any domain from main.py is missing from ALL test patches
#   - Exits 0 always (advisory only)

set -euo pipefail

# ---------------------------------------------------------------------------
# Locate hook from Bazel runfiles
# ---------------------------------------------------------------------------
HOOK_REL="bazel/tools/hooks/check-lifespan-startup-mock-sync.sh"
HOOK=""
for candidate in \
	"${RUNFILES_DIR:-}/_main/${HOOK_REL}" \
	"${TEST_SRCDIR:-}/_main/${HOOK_REL}" \
	"${BASH_SOURCE[0]%/*}/check-lifespan-startup-mock-sync.sh"; do
	if [[ -f "$candidate" ]]; then
		HOOK="$candidate"
		break
	fi
done
if [[ -z "$HOOK" ]]; then
	echo "ERROR: cannot locate check-lifespan-startup-mock-sync.sh in runfiles" >&2
	exit 1
fi

# ---------------------------------------------------------------------------
# jq stub (hermetic Bazel sandbox has no jq)
# ---------------------------------------------------------------------------
mkdir -p "${TEST_TMPDIR}/bin"
cat >"${TEST_TMPDIR}/bin/jq" <<'JQ_STUB'
#!/usr/bin/env python3
"""Minimal jq stub for check-lifespan-startup-mock-sync.sh."""
import json, sys

args = sys.argv[1:]
raw = False
if args and args[0] == "-r":
    raw = True
    args = args[1:]

expr = args[0] if args else "."
data = json.load(sys.stdin)

def jq_eval(obj, expr):
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
    pass
elif raw:
    print(result)
else:
    print(json.dumps(result))
JQ_STUB
chmod +x "${TEST_TMPDIR}/bin/jq"
export PATH="${TEST_TMPDIR}/bin:${PATH}"

# ---------------------------------------------------------------------------
# Build a fake project tree with main.py and test files
# ---------------------------------------------------------------------------
APP_DIR="${TEST_TMPDIR}/project/projects/monolith/app"
mkdir -p "$APP_DIR"

MAIN_PY="${APP_DIR}/main.py"
TEST_A="${APP_DIR}/main_extra_test.py"
TEST_B="${APP_DIR}/main_lifespan_discord_test.py"

# Write test files that patch home, ships, hikes (but NOT stars)
cat >"$TEST_A" <<'PY'
from unittest.mock import patch
PATCHES = [
    patch("home.on_startup_jobs"),
    patch("ships.on_startup_jobs"),
    patch("hikes.on_startup_jobs"),
]
PY

cat >"$TEST_B" <<'PY'
from unittest.mock import patch
def _lifespan_patches():
    return [
        patch("home.on_startup_jobs"),
        patch("ships.on_startup_jobs"),
        patch("hikes.on_startup_jobs"),
    ]
PY

# ---------------------------------------------------------------------------
# JSON helpers
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
# Test runner
# ---------------------------------------------------------------------------
PASS=0
FAIL=0

run_test() {
	local name="$1" input_json="$2" want_exit="$3" want_stderr_re="$4"

	local stderr_out got_exit=0
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

# Content with all three already-patched domains (home, ships, hikes)
CONTENT_THREE_KNOWN="
async def lifespan(app):
    home.on_startup_jobs(session)
    ships.on_startup_jobs(session)
    hikes.on_startup_jobs(session)
"

# Content that also adds stars (not patched in any test)
CONTENT_WITH_STARS="
async def lifespan(app):
    home.on_startup_jobs(session)
    ships.on_startup_jobs(session)
    hikes.on_startup_jobs(session)
    stars.on_startup_jobs(session)
"

# ---------------------------------------------------------------------------
# Test cases
# ---------------------------------------------------------------------------

# 1. Write with known domains only -- no warning
run_test "write_known_domains_silent" \
	"$(make_write_json "$MAIN_PY" "$CONTENT_THREE_KNOWN")" \
	0 ""

# 2. Write adds stars (unmocked) -- warns
run_test "write_adds_unmocked_domain_warns" \
	"$(make_write_json "$MAIN_PY" "$CONTENT_WITH_STARS")" \
	0 "WARNING"

# 3. Warning message mentions the missing domain name
run_test "write_warning_mentions_domain_name" \
	"$(make_write_json "$MAIN_PY" "$CONTENT_WITH_STARS")" \
	0 "stars"

# 4. Edit new_string contains stars (unmocked) -- warns
run_test "edit_adds_unmocked_domain_warns" \
	"$(make_edit_json "$MAIN_PY" "    stars.on_startup_jobs(session)")" \
	0 "WARNING"

# 5. Edit new_string with known domain only -- no warning
run_test "edit_known_domain_silent" \
	"$(make_edit_json "$MAIN_PY" "    home.on_startup_jobs(session)")" \
	0 ""

# 6. Edit new_string with no on_startup_jobs calls -- skip
run_test "edit_no_startup_jobs_skipped" \
	"$(make_edit_json "$MAIN_PY" "    # just a comment")" \
	0 ""

# 7. Wrong file path -- skip entirely
run_test "wrong_file_path_skipped" \
	"$(make_write_json "${APP_DIR}/other.py" "$CONTENT_WITH_STARS")" \
	0 ""

# 8. Empty JSON -- skip
run_test "empty_json_skipped" \
	'{}' \
	0 ""

# 9. Missing file_path -- skip
run_test "missing_file_path_skipped" \
	'{"tool_name":"Write","tool_input":{"content":"stars.on_startup_jobs(session)"}}' \
	0 ""

# 10. Content with no on_startup_jobs calls -- skip
run_test "no_startup_calls_skipped" \
	"$(make_write_json "$MAIN_PY" "def main(): pass")" \
	0 ""

# 11. All patched domains present in test files -- no warning even with multiple
# Use $'...' quoting so \n becomes a real newline; plain double-quotes leave a
# literal backslash-n, causing grep to match the spurious domain "nships".
run_test "all_domains_patched_silent" \
	"$(make_write_json "$MAIN_PY" $'home.on_startup_jobs(session)\nships.on_startup_jobs(session)')" \
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
