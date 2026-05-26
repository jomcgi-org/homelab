#!/usr/bin/env bash
# Unit tests for check-sveltekit-duplicate-api-base.sh PreToolUse hook.
#
# The hook:
#   - Reads JSON from stdin with .tool_input.file_path and either
#     .tool_input.new_string (Edit) or .tool_input.content (Write)
#   - Exits 0 always (warning-only, never blocks)
#   - Emits a WARNING on stderr when a +page.server.js or +server.js file
#     under projects/monolith/frontend/ declares `const API_BASE = process.env.API_BASE`
#   - Skips files outside projects/monolith/frontend/, non-matching basenames,
#     and content that does not contain the duplicate declaration

set -euo pipefail

# ---------------------------------------------------------------------------
# Locate hook from Bazel runfiles
# ---------------------------------------------------------------------------
HOOK_REL="bazel/tools/hooks/check-sveltekit-duplicate-api-base.sh"
HOOK=""
for candidate in \
	"${RUNFILES_DIR:-}/_main/${HOOK_REL}" \
	"${TEST_SRCDIR:-}/_main/${HOOK_REL}" \
	"${BASH_SOURCE[0]%/*}/check-sveltekit-duplicate-api-base.sh"; do
	if [[ -f "$candidate" ]]; then
		HOOK="$candidate"
		break
	fi
done
if [[ -z "$HOOK" ]]; then
	echo "ERROR: cannot locate check-sveltekit-duplicate-api-base.sh in runfiles" >&2
	exit 1
fi

# ---------------------------------------------------------------------------
# Install a minimal jq stub so the hook runs in the hermetic Bazel sandbox.
# The hook uses:
#   jq -r '.tool_input.file_path // empty'
#   jq -r '.tool_input.new_string // .tool_input.content // empty'
# ---------------------------------------------------------------------------
mkdir -p "${TEST_TMPDIR}/bin"
cat >"${TEST_TMPDIR}/bin/jq" <<'JQ_STUB'
#!/usr/bin/env python3
"""Minimal jq stub covering expressions used by check-sveltekit-duplicate-api-base.sh."""
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
	local want_exit="$3"
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

# Build JSON for a Write tool call
make_write_json() {
	local fp="$1" content="$2"
	python3 - "$fp" "$content" <<'PY'
import json, sys
fp, content = sys.argv[1], sys.argv[2]
print(json.dumps({
    "tool_input": {"file_path": fp, "content": content}
}))
PY
}

# Build JSON for an Edit tool call
make_edit_json() {
	local fp="$1" new_str="$2"
	python3 - "$fp" "$new_str" <<'PY'
import json, sys
fp, new_str = sys.argv[1], sys.argv[2]
print(json.dumps({
    "tool_input": {"file_path": fp, "new_string": new_str}
}))
PY
}

# ---------------------------------------------------------------------------
# Content fixtures
# ---------------------------------------------------------------------------

FRONTEND_BASE="/workspace/homelab/projects/monolith/frontend"

# Content with the forbidden inline declaration
BAD_CONTENT='import { json } from "@sveltejs/kit";
const API_BASE = process.env.API_BASE;

export async function load({ fetch }) {
    const res = await fetch(`${API_BASE}/api/notes`);
    return { notes: await res.json() };
}'

# Content using the correct shared import pattern — no inline decl
GOOD_CONTENT='import { json } from "@sveltejs/kit";
import { API_BASE } from "$lib/server/api.js";

export async function load({ fetch }) {
    const res = await fetch(`${API_BASE}/api/notes`);
    return { notes: await res.json() };
}'

# Content with no API_BASE reference at all
UNRELATED_CONTENT='import { json } from "@sveltejs/kit";

export async function load() {
    return { ok: true };
}'

# ---------------------------------------------------------------------------
# Test cases
# ---------------------------------------------------------------------------

# 1. Write to +page.server.js under frontend/ with inline declaration → warns
run_test "write_page_server_js_warns" \
	"$(make_write_json "${FRONTEND_BASE}/routes/notes/+page.server.js" "$BAD_CONTENT")" \
	0 "WARNING.*API_BASE"

# 2. Write to +server.js under frontend/ with inline declaration → warns
run_test "write_server_js_warns" \
	"$(make_write_json "${FRONTEND_BASE}/routes/api/notes/+server.js" "$BAD_CONTENT")" \
	0 "WARNING.*API_BASE"

# 3. Edit (new_string) with inline declaration → warns
run_test "edit_page_server_js_warns" \
	"$(make_edit_json "${FRONTEND_BASE}/routes/notes/+page.server.js" "$BAD_CONTENT")" \
	0 "WARNING.*API_BASE"

# 4. Write with correct shared-import pattern → no warning
run_test "write_correct_import_no_warning" \
	"$(make_write_json "${FRONTEND_BASE}/routes/notes/+page.server.js" "$GOOD_CONTENT")" \
	0 ""

# 5. Write unrelated content (no API_BASE at all) → no warning
run_test "write_no_api_base_no_warning" \
	"$(make_write_json "${FRONTEND_BASE}/routes/index/+page.server.js" "$UNRELATED_CONTENT")" \
	0 ""

# 6. File outside projects/monolith/frontend/ → no warning (different project)
run_test "outside_frontend_skipped" \
	"$(make_write_json "/workspace/homelab/projects/other/frontend/routes/+page.server.js" "$BAD_CONTENT")" \
	0 ""

# 7. Wrong basename (+layout.server.js) even though path matches → no warning
run_test "wrong_basename_skipped" \
	"$(make_write_json "${FRONTEND_BASE}/routes/notes/+layout.server.js" "$BAD_CONTENT")" \
	0 ""

# 8. Wrong basename (+page.js — not server) even though path matches → no warning
run_test "non_server_file_skipped" \
	"$(make_write_json "${FRONTEND_BASE}/routes/notes/+page.js" "$BAD_CONTENT")" \
	0 ""

# 9. Empty file_path → no crash, no warning
run_test "empty_file_path_skipped" \
	"$(python3 -c "import json; print(json.dumps({'tool_input': {'file_path': '', 'content': 'const API_BASE = process.env.API_BASE;'}}))")" \
	0 ""

# 10. Missing file_path key → no crash, no warning
run_test "missing_file_path_skipped" \
	'{"tool_input": {"content": "const API_BASE = process.env.API_BASE;"}}' \
	0 ""

# 11. Empty JSON → no crash, no warning
run_test "empty_json_allowed" \
	'{}' \
	0 ""

# 12. Warning message mentions the shared lib module path
WARN_OUT=$(make_write_json "${FRONTEND_BASE}/routes/notes/+page.server.js" "$BAD_CONTENT" |
	bash "$HOOK" 2>&1 >/dev/null || true)
if echo "$WARN_OUT" | grep -qF '$lib/server/api.js'; then
	echo "PASS [warning_mentions_shared_module]"
	PASS=$((PASS + 1))
else
	echo "FAIL [warning_mentions_shared_module]: stderr=$(printf '%q' "$WARN_OUT")"
	FAIL=$((FAIL + 1))
fi

# 13. Deeply nested route under frontend/ → warns
run_test "deeply_nested_route_warns" \
	"$(make_write_json "${FRONTEND_BASE}/routes/knowledge/notes/detail/+page.server.js" "$BAD_CONTENT")" \
	0 "WARNING"

# 14. Hook always exits 0 (advisory-only, never blocks)
GOT_EXIT=0
make_write_json "${FRONTEND_BASE}/routes/+page.server.js" "$BAD_CONTENT" |
	bash "$HOOK" >/dev/null 2>&1 || GOT_EXIT=$?
if [[ "$GOT_EXIT" -eq 0 ]]; then
	echo "PASS [always_exits_zero]"
	PASS=$((PASS + 1))
else
	echo "FAIL [always_exits_zero]: got exit $GOT_EXIT"
	FAIL=$((FAIL + 1))
fi

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
echo ""
echo "Results: ${PASS} passed, ${FAIL} failed"
if [[ "$FAIL" -gt 0 ]]; then
	exit 1
fi
exit 0
