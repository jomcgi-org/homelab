#!/usr/bin/env bash
# Unit tests for check-hpa-ignoredifferences-replicas.sh PreToolUse hook.
#
# The hook:
#   - Reads JSON from stdin with .tool_name, .tool_input.file_path, and either
#     .tool_input.new_string (Edit) or .tool_input.content (Write)
#   - Triggers on */chart/templates/hpa.yaml file paths
#   - Triggers on */values.yaml whose written content (or existing file) has
#     autoscaling.enabled: true
#   - Exits 0 always (warning-only, never blocks)
#   - Emits a WARNING on stderr when no /spec/replicas ignoreDifferences entry
#     exists in the corresponding deploy/application.yaml
#   - Skips silently when: path does not match, application.yaml absent, or
#     application.yaml already contains /spec/replicas

set -euo pipefail

# ---------------------------------------------------------------------------
# Locate hook from Bazel runfiles
# ---------------------------------------------------------------------------
HOOK_REL="bazel/tools/hooks/check-hpa-ignoredifferences-replicas.sh"
HOOK=""
for candidate in \
	"${RUNFILES_DIR:-}/_main/${HOOK_REL}" \
	"${TEST_SRCDIR:-}/_main/${HOOK_REL}" \
	"${BASH_SOURCE[0]%/*}/check-hpa-ignoredifferences-replicas.sh"; do
	if [[ -f "$candidate" ]]; then
		HOOK="$candidate"
		break
	fi
done
if [[ -z "$HOOK" ]]; then
	echo "ERROR: cannot locate check-hpa-ignoredifferences-replicas.sh in runfiles" >&2
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
"""Minimal jq stub covering expressions used by check-hpa-ignoredifferences-replicas.sh."""
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
# Set up a fake project directory tree
# ---------------------------------------------------------------------------
FAKE_ROOT="${TEST_TMPDIR}/project"
FAKE_SERVICE="${FAKE_ROOT}/projects/myservice"
FAKE_CHART_DIR="${FAKE_SERVICE}/chart"
FAKE_TEMPLATES_DIR="${FAKE_CHART_DIR}/templates"
FAKE_DEPLOY_DIR="${FAKE_SERVICE}/deploy"
mkdir -p "${FAKE_TEMPLATES_DIR}" "${FAKE_DEPLOY_DIR}"

FAKE_HPA="${FAKE_TEMPLATES_DIR}/hpa.yaml"
FAKE_CHART_VALUES="${FAKE_CHART_DIR}/values.yaml"
FAKE_DEPLOY_VALUES="${FAKE_DEPLOY_DIR}/values.yaml"
FAKE_APP_YAML="${FAKE_DEPLOY_DIR}/application.yaml"

# application.yaml WITHOUT /spec/replicas ignoreDifferences
write_app_yaml_no_ignore() {
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
  syncPolicy:
    automated:
      prune: true
      selfHeal: true
YAML
}

# application.yaml WITH /spec/replicas in ignoreDifferences
write_app_yaml_with_ignore() {
	cat >"${FAKE_APP_YAML}" <<'YAML'
apiVersion: argoproj.io/v1alpha1
kind: Application
metadata:
  name: myservice
spec:
  ignoreDifferences:
    - group: apps
      kind: Deployment
      jsonPointers:
        - /spec/replicas
  source:
    repoURL: oci://registry.example.com/charts
    chart: myservice
    targetRevision: 0.1.0
  syncPolicy:
    automated:
      prune: true
      selfHeal: true
YAML
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

HPA_CONTENT='{{- if .Values.backend.autoscaling.enabled }}
apiVersion: autoscaling/v2
kind: HorizontalPodAutoscaler
{{- end }}'

AUTOSCALING_VALUES='backend:
  autoscaling:
    enabled: true
    minReplicas: 1
    maxReplicas: 3'

VALUES_NO_AUTOSCALING='backend:
  replicas: 1
  resources:
    requests:
      memory: 256Mi
    limits:
      memory: 256Mi'

# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------
PASS=0
FAIL=0

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

# 1. HPA template Write, no ignoreDifferences → warns
write_app_yaml_no_ignore
run_test "hpa_template_write_no_ignore_warns" \
	"$(make_write_json "$FAKE_HPA" "$HPA_CONTENT")" \
	0 "WARNING.*ignoreDifferences"

# 2. HPA template Write, ignoreDifferences present → silent
write_app_yaml_with_ignore
run_test "hpa_template_write_with_ignore_silent" \
	"$(make_write_json "$FAKE_HPA" "$HPA_CONTENT")" \
	0 ""

# 3. HPA template Edit, no ignoreDifferences → warns
write_app_yaml_no_ignore
run_test "hpa_template_edit_no_ignore_warns" \
	"$(make_edit_json "$FAKE_HPA" 'minReplicas: 2')" \
	0 "WARNING.*ignoreDifferences"

# 4. deploy/values.yaml with autoscaling.enabled: true Write, no ignore → warns
write_app_yaml_no_ignore
run_test "deploy_values_autoscaling_write_no_ignore_warns" \
	"$(make_write_json "$FAKE_DEPLOY_VALUES" "$AUTOSCALING_VALUES")" \
	0 "WARNING.*ignoreDifferences"

# 5. deploy/values.yaml with autoscaling.enabled: true, ignoreDifferences present → silent
write_app_yaml_with_ignore
run_test "deploy_values_autoscaling_write_with_ignore_silent" \
	"$(make_write_json "$FAKE_DEPLOY_VALUES" "$AUTOSCALING_VALUES")" \
	0 ""

# 6. deploy/values.yaml WITHOUT autoscaling → silent
write_app_yaml_no_ignore
run_test "deploy_values_no_autoscaling_silent" \
	"$(make_write_json "$FAKE_DEPLOY_VALUES" "$VALUES_NO_AUTOSCALING")" \
	0 ""

# 7. chart/values.yaml with autoscaling.enabled: true Write, no ignore → warns
write_app_yaml_no_ignore
run_test "chart_values_autoscaling_write_no_ignore_warns" \
	"$(make_write_json "$FAKE_CHART_VALUES" "$AUTOSCALING_VALUES")" \
	0 "WARNING.*ignoreDifferences"

# 8. Unrelated file path → skip
write_app_yaml_no_ignore
run_test "unrelated_file_path_silent" \
	"$(make_write_json "${FAKE_SERVICE}/chart/templates/deployment.yaml" "$HPA_CONTENT")" \
	0 ""

# 9. No application.yaml on disk → skip
FAKE_SERVICE2="${FAKE_ROOT}/projects/noapp"
mkdir -p "${FAKE_SERVICE2}/chart/templates" "${FAKE_SERVICE2}/deploy"
run_test "no_application_yaml_silent" \
	"$(make_write_json "${FAKE_SERVICE2}/chart/templates/hpa.yaml" "$HPA_CONTENT")" \
	0 ""

# 10. Empty JSON input → skip
run_test "empty_json_silent" \
	'{}' \
	0 ""

# 11. Warning includes service name
write_app_yaml_no_ignore
run_test "warning_includes_service_name" \
	"$(make_write_json "$FAKE_HPA" "$HPA_CONTENT")" \
	0 "myservice"

# 12. deploy/values.yaml Edit where autoscaling already enabled in file on disk
write_app_yaml_no_ignore
echo "$AUTOSCALING_VALUES" > "$FAKE_DEPLOY_VALUES"
run_test "deploy_values_edit_autoscaling_in_file_warns" \
	"$(make_edit_json "$FAKE_DEPLOY_VALUES" 'maxReplicas: 5')" \
	0 "WARNING.*ignoreDifferences"
rm -f "$FAKE_DEPLOY_VALUES"

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
echo ""
echo "Results: ${PASS} passed, ${FAIL} failed"
if [[ "$FAIL" -gt 0 ]]; then
	exit 1
fi
exit 0
