#!/usr/bin/env bash
# Unit tests for check-chart-version-targetrevision-sync.sh PreToolUse hook.
#
# The hook:
#   - Reads JSON from stdin with .tool_name, .tool_input.file_path, and either
#     .tool_input.new_string (Edit) or .tool_input.content (Write)
#   - Only triggers on */chart/Chart.yaml file paths
#   - Exits 0 always (warning-only, never blocks)
#   - Emits a WARNING on stderr when the version: line in the content being
#     written differs from the targetRevision: in the adjacent deploy/application.yaml
#   - Skips when: file path is wrong, content has no version line, or
#     application.yaml does not exist

set -euo pipefail

# ---------------------------------------------------------------------------
# Locate hook from Bazel runfiles
# ---------------------------------------------------------------------------
HOOK_REL="bazel/tools/hooks/check-chart-version-targetrevision-sync.sh"
HOOK=""
for candidate in \
	"${RUNFILES_DIR:-}/_main/${HOOK_REL}" \
	"${TEST_SRCDIR:-}/_main/${HOOK_REL}" \
	"${BASH_SOURCE[0]%/*}/check-chart-version-targetrevision-sync.sh"; do
	if [[ -f "$candidate" ]]; then
		HOOK="$candidate"
		break
	fi
done
if [[ -z "$HOOK" ]]; then
	echo "ERROR: cannot locate check-chart-version-targetrevision-sync.sh in runfiles" >&2
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
"""Minimal jq stub covering expressions used by check-chart-version-targetrevision-sync.sh."""
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
# Set up a fake project directory tree on disk.
#
# The hook derives APP_YAML from FILE_PATH:
#   FILE_PATH: .../projects/<service>/chart/Chart.yaml
#   APP_YAML:  .../projects/<service>/deploy/application.yaml
# ---------------------------------------------------------------------------
FAKE_ROOT="${TEST_TMPDIR}/project"
FAKE_SERVICE="${FAKE_ROOT}/projects/myservice"
FAKE_CHART_DIR="${FAKE_SERVICE}/chart"
FAKE_DEPLOY_DIR="${FAKE_SERVICE}/deploy"
mkdir -p "${FAKE_CHART_DIR}" "${FAKE_DEPLOY_DIR}"

FAKE_CHART_YAML="${FAKE_CHART_DIR}/Chart.yaml"
FAKE_APP_YAML="${FAKE_DEPLOY_DIR}/application.yaml"

# Write an application.yaml whose targetRevision is 0.1.0
cat >"${FAKE_APP_YAML}" <<'YAML'
apiVersion: argoproj.io/v1alpha1
kind: Application
metadata:
  name: myservice
spec:
  source:
    repoURL: oci://registry.example.com/charts
    chart: myservice
    targetRevision: 0.1.0
YAML

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
# Test cases
# ---------------------------------------------------------------------------

CHART_PATH="${FAKE_CHART_YAML}"

# 1. Version mismatch — Write tool → warns
run_test "write_version_mismatch_warns" \
	"$(make_write_json "$CHART_PATH" \
		'apiVersion: v2
name: myservice
version: 0.2.0
')" \
	0 "WARNING.*out of sync"

# 2. Versions match — Write tool → no warning
run_test "write_version_matches_silent" \
	"$(make_write_json "$CHART_PATH" \
		'apiVersion: v2
name: myservice
version: 0.1.0
')" \
	0 ""

# 3. Version mismatch — Edit tool (new_string carries only changed lines) → warns
run_test "edit_version_mismatch_warns" \
	"$(make_edit_json "$CHART_PATH" 'version: 0.3.0')" \
	0 "WARNING.*out of sync"

# 4. Edit with matching version → no warning
run_test "edit_version_matches_silent" \
	"$(make_edit_json "$CHART_PATH" 'version: 0.1.0')" \
	0 ""

# 5. Wrong file path (not Chart.yaml) → skip
run_test "wrong_file_path_skipped" \
	"$(make_write_json "${FAKE_DEPLOY_DIR}/values.yaml" 'version: 0.9.0')" \
	0 ""

# 6. Right filename but not under chart/ directory → skip
run_test "not_under_chart_dir_skipped" \
	"$(make_write_json "${FAKE_SERVICE}/deploy/Chart.yaml" 'version: 0.9.0')" \
	0 ""

# 7. Content has no version: line → skip
run_test "no_version_line_skipped" \
	"$(make_write_json "$CHART_PATH" \
		'apiVersion: v2
name: myservice
description: no version here
')" \
	0 ""

# 8. No application.yaml on disk → skip
FAKE_SERVICE2="${FAKE_ROOT}/projects/noapp"
FAKE_CHART_DIR2="${FAKE_SERVICE2}/chart"
mkdir -p "${FAKE_CHART_DIR2}"
run_test "no_application_yaml_skipped" \
	"$(make_write_json "${FAKE_CHART_DIR2}/Chart.yaml" 'version: 1.0.0')" \
	0 ""

# 9. application.yaml exists but has no targetRevision → skip
FAKE_SERVICE3="${FAKE_ROOT}/projects/notargetrev"
FAKE_CHART_DIR3="${FAKE_SERVICE3}/chart"
FAKE_DEPLOY_DIR3="${FAKE_SERVICE3}/deploy"
mkdir -p "${FAKE_CHART_DIR3}" "${FAKE_DEPLOY_DIR3}"
cat >"${FAKE_DEPLOY_DIR3}/application.yaml" <<'YAML'
apiVersion: argoproj.io/v1alpha1
kind: Application
metadata:
  name: notargetrev
spec:
  source:
    repoURL: oci://registry.example.com/charts
    chart: notargetrev
YAML
run_test "no_targetrevision_in_app_yaml_skipped" \
	"$(make_write_json "${FAKE_CHART_DIR3}/Chart.yaml" 'version: 1.0.0')" \
	0 ""

# 10. Empty JSON input → skip
run_test "empty_json_skipped" \
	'{}' \
	0 ""

# 11. Missing file_path in JSON → skip
run_test "missing_file_path_skipped" \
	'{"tool_name":"Write","tool_input":{"content":"version: 9.9.9"}}' \
	0 ""

# 12. Mismatch warning includes service name and both version values
run_test "warning_includes_service_name" \
	"$(make_write_json "$CHART_PATH" 'version: 0.2.0')" \
	0 "myservice"

run_test "warning_includes_chart_version" \
	"$(make_write_json "$CHART_PATH" 'version: 0.2.0')" \
	0 "0\.2\.0"

run_test "warning_includes_target_revision" \
	"$(make_write_json "$CHART_PATH" 'version: 0.2.0')" \
	0 "0\.1\.0"

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
echo ""
echo "Results: ${PASS} passed, ${FAIL} failed"
if [[ "$FAIL" -gt 0 ]]; then
	exit 1
fi
exit 0
