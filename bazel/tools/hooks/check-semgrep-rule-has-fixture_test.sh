#!/usr/bin/env bash
# Unit tests for check-semgrep-rule-has-fixture.sh PreToolUse hook.
#
# The hook:
#   - Reads JSON from stdin with .tool_input.file_path
#   - Exits 0 always (warning-only, never blocks)
#   - Emits a WARNING on stderr when a semgrep rule yaml has no fixture in
#     either bazel/semgrep/tests/fixtures/ or colocated in the rule directory
#   - Skips non-rule files (not under bazel/semgrep/rules/<subdir>/)

set -euo pipefail

# ---------------------------------------------------------------------------
# Locate hook from Bazel runfiles
# ---------------------------------------------------------------------------
HOOK_REL="bazel/tools/hooks/check-semgrep-rule-has-fixture.sh"
HOOK=""
for candidate in \
	"${RUNFILES_DIR:-}/_main/${HOOK_REL}" \
	"${TEST_SRCDIR:-}/_main/${HOOK_REL}" \
	"${BASH_SOURCE[0]%/*}/check-semgrep-rule-has-fixture.sh"; do
	if [[ -f "$candidate" ]]; then
		HOOK="$candidate"
		break
	fi
done
if [[ -z "$HOOK" ]]; then
	echo "ERROR: cannot locate check-semgrep-rule-has-fixture.sh in runfiles" >&2
	exit 1
fi

# ---------------------------------------------------------------------------
# Install a minimal jq stub so the hook runs in the hermetic Bazel sandbox.
# The hook uses:
#   jq -r '.tool_input.file_path // empty'
# ---------------------------------------------------------------------------
mkdir -p "${TEST_TMPDIR}/bin"
cat >"${TEST_TMPDIR}/bin/jq" <<'JQ_STUB'
#!/usr/bin/env python3
"""Minimal jq stub covering expressions used by check-semgrep-rule-has-fixture.sh."""
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
# Set up a fake project directory tree that mirrors the real layout
# ---------------------------------------------------------------------------
FAKE_ROOT="${TEST_TMPDIR}/project"
FAKE_RULES="${FAKE_ROOT}/bazel/semgrep/rules"
FAKE_FIXTURES="${FAKE_ROOT}/bazel/semgrep/tests/fixtures"
FAKE_YAML_TESTS="${FAKE_ROOT}/bazel/semgrep/tests/yaml"
mkdir -p "${FAKE_RULES}/kubernetes" "${FAKE_RULES}/python" "${FAKE_RULES}/yaml" "${FAKE_FIXTURES}"

# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------
PASS=0
FAIL=0

make_json() {
	local fp="$1"
	python3 - "$fp" <<'PY'
import json, sys
print(json.dumps({"tool_name": "Write", "tool_input": {"file_path": sys.argv[1], "content": "rules: []"}}))
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

# ---------------------------------------------------------------------------
# Test cases
# ---------------------------------------------------------------------------

# 1. Rule has a fixture in tests/fixtures/ → silent
RULE_A="${FAKE_RULES}/kubernetes/no-privileged.yaml"
touch "$RULE_A"
touch "${FAKE_FIXTURES}/no-privileged.yaml"
run_test "rule_with_central_fixture_silent" \
	"$(make_json "$RULE_A")" \
	0 ""

# 2. Rule has NO fixture anywhere → warns
RULE_B="${FAKE_RULES}/kubernetes/no-orphan-resource.yaml"
touch "$RULE_B"
run_test "rule_without_fixture_warns" \
	"$(make_json "$RULE_B")" \
	0 "WARNING.*no-orphan-resource"

# 3. Non-rule file (wrong path) → silent
run_test "non_rule_file_silent" \
	"$(make_json "${FAKE_ROOT}/projects/myapp/chart/templates/deployment.yaml")" \
	0 ""

# 4. Empty JSON → silent
run_test "empty_json_silent" \
	'{}' \
	0 ""

# 5. Rule has colocated non-yaml fixture (dash-named) → silent
RULE_C="${FAKE_RULES}/yaml/no-httproute-timeout.yaml"
touch "$RULE_C"
touch "${FAKE_RULES}/yaml/no-httproute-timeout.txt"
run_test "rule_with_colocated_fixture_silent" \
	"$(make_json "$RULE_C")" \
	0 ""

# 6. Rule has colocated fixture with Python underscore naming → silent
RULE_D="${FAKE_RULES}/python/session-add-in-loop.yaml"
touch "$RULE_D"
touch "${FAKE_RULES}/python/session_add_in_loop.py"
run_test "rule_with_colocated_underscore_fixture_silent" \
	"$(make_json "$RULE_D")" \
	0 ""

# 7. Rule has a central fixture with non-yaml extension → silent
RULE_E="${FAKE_RULES}/yaml/no-unquoted-args.yaml"
touch "$RULE_E"
touch "${FAKE_FIXTURES}/no-unquoted-args.go"
run_test "rule_with_central_non_yaml_fixture_silent" \
	"$(make_json "$RULE_E")" \
	0 ""

# 8. File in rules/ top level (no subdir) — does NOT match the pattern → silent
TOP_RULE="${FAKE_RULES}/no-top-level.yaml"
touch "$TOP_RULE"
run_test "top_level_rules_file_silent" \
	"$(make_json "$TOP_RULE")" \
	0 ""

# 9. Rule has a colocated .yaml sibling (e.g. another YAML rule or config) but
# no non-yaml fixture. has_non_yaml_fixture filters .yaml files, so the
# sibling must NOT satisfy Check 2 → warns.
# This explicitly exercises the `[[ "$f" == *.yaml ]] && continue` branch.
RULE_F="${FAKE_RULES}/kubernetes/no-yaml-only-fixture.yaml"
touch "$RULE_F"
touch "${FAKE_RULES}/kubernetes/no-yaml-only-fixture.config.yaml"
# The .yaml sibling is filtered out by has_non_yaml_fixture; no non-yaml
# fixture exists → WARNING expected.
run_test "rule_colocated_yaml_only_fixture_warns" \
	"$(make_json "$RULE_F")" \
	0 "WARNING.*no-yaml-only-fixture"

# 10. Rule whose stem has no dashes (STEM_UNDERSCORED == STEM) and no fixture → warns.
# Check 3 is skipped because the stem contains no dashes; warning must still fire.
RULE_G="${FAKE_RULES}/kubernetes/noprivileged.yaml"
touch "$RULE_G"
run_test "rule_no_dashes_in_stem_no_fixture_warns" \
	"$(make_json "$RULE_G")" \
	0 "WARNING.*noprivileged"

# 11. Rule has a tests/yaml/<stem>/ directory (ok.yaml + bad.yaml convention) → silent
RULE_H="${FAKE_RULES}/yaml/memory-request-ne-limit.yaml"
touch "$RULE_H"
mkdir -p "${FAKE_YAML_TESTS}/memory-request-ne-limit"
touch "${FAKE_YAML_TESTS}/memory-request-ne-limit/ok.yaml"
touch "${FAKE_YAML_TESTS}/memory-request-ne-limit/bad.yaml"
run_test "rule_with_yaml_test_dir_silent" \
	"$(make_json "$RULE_H")" \
	0 ""

# 12. Rule with tests/yaml/<stem>/ directory that is empty still counts → silent
RULE_I="${FAKE_RULES}/yaml/another-rule.yaml"
touch "$RULE_I"
mkdir -p "${FAKE_YAML_TESTS}/another-rule"
run_test "rule_with_empty_yaml_test_dir_silent" \
	"$(make_json "$RULE_I")" \
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
