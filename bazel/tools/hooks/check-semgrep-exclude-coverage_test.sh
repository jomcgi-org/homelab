#!/usr/bin/env bash
# Unit tests for check-semgrep-exclude-coverage.sh PreToolUse hook.
#
# The hook:
#   - Reads JSON from stdin with .tool_name, .tool_input.file_path, and either
#     .tool_input.new_string (Edit) or .tool_input.content (Write)
#   - Exits 0 always (warning-only, never blocks)
#   - Emits a WARNING on stderr when a semgrep rule yaml has paths.exclude
#     entries that lack inline comments
#   - Skips non-rule files (not under bazel/semgrep/rules/<subdir>/)
#   - Skips rules with no exclude: block

set -euo pipefail

# ---------------------------------------------------------------------------
# Locate hook from Bazel runfiles
# ---------------------------------------------------------------------------
HOOK_REL="bazel/tools/hooks/check-semgrep-exclude-coverage.sh"
HOOK=""
for candidate in \
	"${RUNFILES_DIR:-}/_main/${HOOK_REL}" \
	"${TEST_SRCDIR:-}/_main/${HOOK_REL}" \
	"${BASH_SOURCE[0]%/*}/check-semgrep-exclude-coverage.sh"; do
	if [[ -f "$candidate" ]]; then
		HOOK="$candidate"
		break
	fi
done
if [[ -z "$HOOK" ]]; then
	echo "ERROR: cannot locate check-semgrep-exclude-coverage.sh in runfiles" >&2
	exit 1
fi

# ---------------------------------------------------------------------------
# Install a minimal jq stub so the hook runs in the hermetic Bazel sandbox.
# The hook uses:
#   jq -r '.tool_input.file_path // empty'
#   jq -r '.tool_name // empty'
#   jq -r '.tool_input.new_string // empty'
#   jq -r '.tool_input.content // empty'
# ---------------------------------------------------------------------------
mkdir -p "${TEST_TMPDIR}/bin"
cat >"${TEST_TMPDIR}/bin/jq" <<'JQ_STUB'
#!/usr/bin/env python3
"""Minimal jq stub covering expressions used by check-semgrep-exclude-coverage.sh."""
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
# Helpers
# ---------------------------------------------------------------------------
PASS=0
FAIL=0

FAKE_ROOT="${TEST_TMPDIR}/project"
FAKE_RULES="${FAKE_ROOT}/bazel/semgrep/rules"
mkdir -p "${FAKE_RULES}/python" "${FAKE_RULES}/kubernetes"

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
print(json.dumps({"tool_name": "Edit", "tool_input": {"file_path": fp, "old_string": "", "new_string": new_str}}))
PY
}

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

RULE_PATH="${FAKE_RULES}/python/no-sync-session.yaml"

# 1. Write: exclude entry WITH inline comment → silent
run_test "write_exclude_with_comment_silent" \
	"$(make_write_json "$RULE_PATH" \
		'rules:
  - id: no-sync-session
    paths:
      exclude:
        - "projects/legacy/**"  # legacy service tracked in #1234
    severity: WARNING')" \
	0 ""

# 2. Write: exclude entry WITHOUT inline comment → warns
run_test "write_exclude_without_comment_warns" \
	"$(make_write_json "$RULE_PATH" \
		'rules:
  - id: no-sync-session
    paths:
      exclude:
        - "projects/legacy/**"
    severity: WARNING')" \
	0 "WARNING.*no-sync-session"

# 3. Write: no exclude block at all → silent
run_test "write_no_exclude_block_silent" \
	"$(make_write_json "$RULE_PATH" \
		'rules:
  - id: no-sync-session
    paths:
      include:
        - "projects/**/*.py"
    severity: WARNING')" \
	0 ""

# 4. Edit: new_string has exclude entry with comment → silent
run_test "edit_exclude_with_comment_silent" \
	"$(make_edit_json "$RULE_PATH" \
		'      exclude:
        - "projects/vendor/**"  # vendored, not owned by this team')" \
	0 ""

# 5. Edit: new_string has exclude entry without comment → warns
run_test "edit_exclude_without_comment_warns" \
	"$(make_edit_json "$RULE_PATH" \
		'      exclude:
        - "projects/vendor/**"')" \
	0 "WARNING.*no-sync-session"

# 6. Non-rule file path (not under bazel/semgrep/rules/) → silent
run_test "non_rule_file_silent" \
	"$(make_write_json "${FAKE_ROOT}/projects/myapp/deploy/values.yaml" \
		'paths:
  exclude:
    - "old/**"')" \
	0 ""

# 7. Empty JSON → silent
run_test "empty_json_silent" \
	'{}' \
	0 ""

# 8. Write: multiple exclude entries, one missing comment → warns
run_test "write_mixed_comments_warns" \
	"$(make_write_json "$RULE_PATH" \
		'rules:
  - id: no-sync-session
    paths:
      exclude:
        - "projects/legacy/**"  # tracked in #1234
        - "projects/vendor/**"
    severity: WARNING')" \
	0 "WARNING.*no-sync-session"

# 9. Write: all exclude entries have comments → silent
run_test "write_all_entries_commented_silent" \
	"$(make_write_json "$RULE_PATH" \
		'rules:
  - id: no-sync-session
    paths:
      exclude:
        - "projects/legacy/**"  # tracked in #1234
        - "projects/vendor/**"  # vendored, not owned here
    severity: WARNING')" \
	0 ""

# 10. File in rules/ top level (no subdir) — pattern does not match → silent
TOP_RULE="${FAKE_RULES}/no-top-level.yaml"
run_test "top_level_rules_file_silent" \
	"$(make_write_json "$TOP_RULE" \
		'rules:
  - id: no-top-level
    paths:
      exclude:
        - "projects/skip/**"
    severity: WARNING')" \
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
