#!/usr/bin/env bash
# Unit tests for check-semgrep-helm-fixture-has-template-syntax.sh PreToolUse hook.
#
# The hook:
#   - Reads JSON from stdin with .tool_name, .tool_input.file_path, and either
#     .tool_input.new_string (Edit) or .tool_input.content (Write)
#   - Exits 0 always (warning-only, never blocks)
#   - Emits a WARNING on stderr when a fixture for a generic+chart/templates rule
#     is written without any {{ Helm directive
#   - Skips non-fixture files, rules without generic language, and rules that
#     don't target chart/templates

set -euo pipefail

# ---------------------------------------------------------------------------
# Locate hook from Bazel runfiles
# ---------------------------------------------------------------------------
HOOK_REL="bazel/tools/hooks/check-semgrep-helm-fixture-has-template-syntax.sh"
HOOK=""
for candidate in \
	"${RUNFILES_DIR:-}/_main/${HOOK_REL}" \
	"${TEST_SRCDIR:-}/_main/${HOOK_REL}" \
	"${BASH_SOURCE[0]%/*}/check-semgrep-helm-fixture-has-template-syntax.sh"; do
	if [[ -f "$candidate" ]]; then
		HOOK="$candidate"
		break
	fi
done
if [[ -z "$HOOK" ]]; then
	echo "ERROR: cannot locate check-semgrep-helm-fixture-has-template-syntax.sh in runfiles" >&2
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
"""Minimal jq stub covering expressions used by the hook."""
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
# Set up fake project directory tree
# ---------------------------------------------------------------------------
FAKE_ROOT="${TEST_TMPDIR}/project"
FAKE_RULES="${FAKE_ROOT}/bazel/semgrep/rules/kubernetes"
FAKE_FIXTURES="${FAKE_ROOT}/bazel/semgrep/tests/fixtures"
mkdir -p "${FAKE_RULES}" "${FAKE_FIXTURES}"

# Create a generic+chart/templates rule
GENERIC_HELM_RULE="${FAKE_RULES}/require-component-label.yaml"
cat >"$GENERIC_HELM_RULE" <<'YAML'
rules:
  - id: require-component-label
    languages: [generic]
    paths:
      include:
        - "**/chart/templates/**"
    pattern-regex: 'matchLabels:'
    message: Test rule
    severity: WARNING
YAML

# Create a yaml (non-generic) rule that also targets chart/templates
YAML_RULE="${FAKE_RULES}/no-privileged.yaml"
cat >"$YAML_RULE" <<'YAML'
rules:
  - id: no-privileged
    languages: [yaml]
    paths:
      include:
        - "**/chart/templates/**"
    pattern: |
      privileged: true
    message: Test rule
    severity: ERROR
YAML

# Create a generic rule that does NOT target chart/templates
GENERIC_NON_HELM_RULE="${FAKE_RULES}/no-stale-paths.yaml"
cat >"$GENERIC_NON_HELM_RULE" <<'YAML'
rules:
  - id: no-stale-paths
    languages: [generic]
    paths:
      include:
        - "**/*.yaml"
    pattern-regex: 'old-service-name'
    message: Test rule
    severity: WARNING
YAML

# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------
PASS=0
FAIL=0

make_write_json() {
	local fp="$1" content="$2"
	python3 - "$fp" "$content" <<'PY'
import json, sys
fp, content = sys.argv[1], sys.argv[2]
print(json.dumps({"tool_name": "Write", "tool_input": {"file_path": fp, "content": content}}))
PY
}

make_edit_json() {
	local fp="$1" old_str="$2" new_str="$3"
	python3 - "$fp" "$old_str" "$new_str" <<'PY'
import json, sys
fp, old_str, new_str = sys.argv[1], sys.argv[2], sys.argv[3]
print(json.dumps({"tool_name": "Edit", "tool_input": {"file_path": fp, "old_string": old_str, "new_string": new_str}}))
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

FIXTURE_PATH="${FAKE_FIXTURES}/require-component-label.yaml"

# 1. Write: generic+helm rule fixture WITH {{ syntax → silent
run_test "write_generic_helm_fixture_with_helm_syntax_silent" \
	"$(make_write_json "$FIXTURE_PATH" \
		'# ruleid: require-component-label
    matchLabels:
      {{- include "app.selectorLabels" . | nindent 6 }}')" \
	0 ""

# 2. Write: generic+helm rule fixture WITHOUT {{ syntax → warns
run_test "write_generic_helm_fixture_missing_helm_syntax_warns" \
	"$(make_write_json "$FIXTURE_PATH" \
		'# ruleid: require-component-label
    matchLabels:
      app: myapp')" \
	0 "WARNING.*require-component-label"

# 3. Write: yaml-language rule fixture (not generic) → silent even without {{
YAML_FIXTURE="${FAKE_FIXTURES}/no-privileged.yaml"
run_test "write_yaml_rule_fixture_no_helm_syntax_silent" \
	"$(make_write_json "$YAML_FIXTURE" \
		'securityContext:
  privileged: true')" \
	0 ""

# 4. Write: generic rule but NOT chart/templates → silent even without {{
STALE_FIXTURE="${FAKE_FIXTURES}/no-stale-paths.yaml"
run_test "write_generic_non_helm_rule_fixture_silent" \
	"$(make_write_json "$STALE_FIXTURE" \
		'service: old-service-name')" \
	0 ""

# 5. Write: non-fixture file path → silent
run_test "write_non_fixture_path_silent" \
	"$(make_write_json "${FAKE_ROOT}/projects/myapp/chart/templates/deploy.yaml" \
		'no helm syntax here')" \
	0 ""

# 6. Empty JSON → silent
run_test "empty_json_silent" \
	'{}' \
	0 ""

# 7. Edit: fixture for generic+helm rule, new_string has {{ → silent
run_test "edit_with_helm_syntax_in_new_string_silent" \
	"$(make_edit_json "$FIXTURE_PATH" \
		'app: myapp' \
		'{{- include "app.labels" . | nindent 4 }}')" \
	0 ""

# 8. Edit: fixture for generic+helm rule, existing file already has {{ → silent
printf '# existing fixture\n{{ .Values.foo }}\n' >"$FIXTURE_PATH"
run_test "edit_existing_file_has_helm_syntax_silent" \
	"$(make_edit_json "$FIXTURE_PATH" \
		'app: myapp' \
		'app: newapp')" \
	0 ""
rm -f "$FIXTURE_PATH"

# 9. Write: fixture for rule that doesn't exist yet → silent (no rule to check)
MISSING_FIXTURE="${FAKE_FIXTURES}/nonexistent-rule.yaml"
run_test "write_fixture_no_matching_rule_silent" \
	"$(make_write_json "$MISSING_FIXTURE" \
		'no helm syntax here')" \
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
