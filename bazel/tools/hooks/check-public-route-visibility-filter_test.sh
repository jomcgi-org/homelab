#!/usr/bin/env bash
# Unit tests for check-public-route-visibility-filter.sh PreToolUse hook.
#
# The hook:
#   - Reads JSON from stdin with .tool_input.file_path and .tool_input.content
#     (Write) or .tool_input.new_string (Edit)
#   - Exits 0 always (warning-only, never blocks)
#   - Emits a WARNING on stderr when knowledge/router.py adds a /public/ route
#     whose function body does not call public_notes_filter or effective_visibility
#   - Skips non-router files, non-public routes, and routes that already call
#     the required helpers

set -euo pipefail

# ---------------------------------------------------------------------------
# Locate hook from Bazel runfiles
# ---------------------------------------------------------------------------
HOOK_REL="bazel/tools/hooks/check-public-route-visibility-filter.sh"
HOOK=""
for candidate in \
	"${RUNFILES_DIR:-}/_main/${HOOK_REL}" \
	"${TEST_SRCDIR:-}/_main/${HOOK_REL}" \
	"${BASH_SOURCE[0]%/*}/check-public-route-visibility-filter.sh"; do
	if [[ -f "$candidate" ]]; then
		HOOK="$candidate"
		break
	fi
done
if [[ -z "$HOOK" ]]; then
	echo "ERROR: cannot locate check-public-route-visibility-filter.sh in runfiles" >&2
	exit 1
fi

# ---------------------------------------------------------------------------
# Install a minimal jq stub so the hook runs in the hermetic sandbox.
# The hook uses:
#   jq -r '.tool_input.file_path // empty'
#   jq -r '.tool_input.new_string // .tool_input.content // empty'
# ---------------------------------------------------------------------------
mkdir -p "${TEST_TMPDIR}/bin"
cat >"${TEST_TMPDIR}/bin/jq" <<'JQ_STUB'
#!/usr/bin/env python3
"""Minimal jq stub covering expressions used by check-public-route-visibility-filter.sh."""
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

# Build JSON inputs using Python (guaranteed available in the hermetic Bazel sandbox).
make_json() {
	local fp="$1" key="$2" val="$3"
	python3 - "$fp" "$key" "$val" <<'PY'
import json, sys
fp, key, val = sys.argv[1], sys.argv[2], sys.argv[3]
print(json.dumps({"tool_input": {"file_path": fp, key: val}}))
PY
}

ROUTER_PATH="/workspace/homelab/projects/monolith/knowledge/router.py"

# ---------------------------------------------------------------------------
# Content fixtures
# ---------------------------------------------------------------------------

# /public/ route WITHOUT either visibility helper → should warn
BAD_CONTENT='@router.get("/public/feed")
def get_public_feed(session: Session = Depends(get_session)):
    """Return recent public notes."""
    rows = session.execute(select(Note).where(Note.type == "atom")).all()
    return [r.note_id for r in rows]
'

# /public/ route WITH public_notes_filter → OK
GOOD_FILTER_CONTENT='@router.get("/public/feed")
def get_public_feed(session: Session = Depends(get_session)):
    """Return recent public notes."""
    rows = session.execute(
        select(Note).where(public_notes_filter())
    ).all()
    return [r.note_id for r in rows]
'

# /public/ route WITH effective_visibility → OK
GOOD_EFFECTIVE_CONTENT='@router.get("/public/notes/{note_id}")
def get_public_note(note_id: str, session: Session = Depends(get_session)):
    """Return a note iff public."""
    note = session.get(Note, note_id)
    if note is None or effective_visibility(note) != "public":
        raise HTTPException(404)
    return note
'

# Non-public route (no /public/ in path) → should NOT warn
NON_PUBLIC_CONTENT='@router.get("/internal/notes")
def get_notes(session: Session = Depends(get_session)):
    """Internal listing, auth required."""
    rows = session.execute(select(Note)).all()
    return rows
'

# Route with /public/ but double-guarded by both helpers → OK
BOTH_HELPERS_CONTENT='@router.get("/public/graph")
def get_public_graph(session: Session = Depends(get_session)):
    notes = session.execute(select(Note).where(public_notes_filter())).all()
    filtered = [n for n in notes if effective_visibility(n) == "public"]
    return filtered
'

# ---------------------------------------------------------------------------
# Test cases
# ---------------------------------------------------------------------------

# 1. /public/ route missing visibility guard → warns
run_test "missing_visibility_filter_warns" \
	"$(make_json "$ROUTER_PATH" "content" "$BAD_CONTENT")" \
	0 "WARNING.*visibility filter"

# 2. /public/ route with public_notes_filter → no warning
run_test "has_public_notes_filter_ok" \
	"$(make_json "$ROUTER_PATH" "content" "$GOOD_FILTER_CONTENT")" \
	0 ""

# 3. /public/ route with effective_visibility → no warning
run_test "has_effective_visibility_ok" \
	"$(make_json "$ROUTER_PATH" "content" "$GOOD_EFFECTIVE_CONTENT")" \
	0 ""

# 4. Non-public route → no warning
run_test "non_public_route_skipped" \
	"$(make_json "$ROUTER_PATH" "content" "$NON_PUBLIC_CONTENT")" \
	0 ""

# 5. Wrong file path → no warning (hook skips non-knowledge/router.py)
run_test "wrong_file_skipped" \
	"$(make_json "/workspace/homelab/projects/monolith/other/router.py" "content" "$BAD_CONTENT")" \
	0 ""

# 6. Edit tool (new_string) missing filter → warns
run_test "edit_tool_missing_filter_warns" \
	"$(make_json "$ROUTER_PATH" "new_string" "$BAD_CONTENT")" \
	0 "WARNING"

# 7. Route with both helpers → no warning
run_test "both_helpers_ok" \
	"$(make_json "$ROUTER_PATH" "content" "$BOTH_HELPERS_CONTENT")" \
	0 ""

# 8. Empty JSON → no crash, no warning
run_test "empty_json_allowed" \
	'{}' \
	0 ""

# 9. Empty content → no warning
run_test "empty_content_skipped" \
	"$(make_json "$ROUTER_PATH" "content" "")" \
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
