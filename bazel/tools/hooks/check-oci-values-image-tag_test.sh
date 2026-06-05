#!/usr/bin/env bash
# Unit tests for check-oci-values-image-tag.sh PreToolUse hook.
#
# The hook:
#   - Reads JSON from stdin with .tool_name, .tool_input.file_path, and either
#     .tool_input.new_string (Edit) or .tool_input.content (Write)
#   - Only triggers on */projects/*/deploy/values.yaml file paths
#   - Skips when chart/BUILD does not exist or has no 'publish = True'
#   - Exits 0 always (warning-only, never blocks)
#   - Emits a WARNING on stderr when 'tag:' appears under an 'image:' block
#     in the deploy overlay content (using indent-aware awk detection)
#   - Skips silently when content has no image.tag, no chart BUILD, etc.

set -euo pipefail

# ---------------------------------------------------------------------------
# Locate hook from Bazel runfiles
# ---------------------------------------------------------------------------
HOOK_REL="bazel/tools/hooks/check-oci-values-image-tag.sh"
HOOK=""
for candidate in \
	"${RUNFILES_DIR:-}/_main/${HOOK_REL}" \
	"${TEST_SRCDIR:-}/_main/${HOOK_REL}" \
	"${BASH_SOURCE[0]%/*}/check-oci-values-image-tag.sh"; do
	if [[ -f "$candidate" ]]; then
		HOOK="$candidate"
		break
	fi
done
if [[ -z "$HOOK" ]]; then
	echo "ERROR: cannot locate check-oci-values-image-tag.sh in runfiles" >&2
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
"""Minimal jq stub covering expressions used by check-oci-values-image-tag.sh."""
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
# Set up fake project directory tree on disk.
#
# The hook derives CHART_BUILD from FILE_PATH:
#   FILE_PATH:   .../projects/<service>/deploy/values.yaml
#   CHART_BUILD: .../projects/<service>/chart/BUILD
# ---------------------------------------------------------------------------
FAKE_ROOT="${TEST_TMPDIR}/project"

# Service with OCI-published chart (publish = True)
OCI_SERVICE="${FAKE_ROOT}/projects/oci-service"
OCI_CHART_DIR="${OCI_SERVICE}/chart"
OCI_DEPLOY_DIR="${OCI_SERVICE}/deploy"
mkdir -p "${OCI_CHART_DIR}" "${OCI_DEPLOY_DIR}"
cat >"${OCI_CHART_DIR}/BUILD" <<'BUILD'
load("//bazel:helm.bzl", "helm_chart")
helm_chart(
    name = "chart",
    publish = True,
)
BUILD

# Service WITHOUT OCI publishing (no publish = True)
NON_OCI_SERVICE="${FAKE_ROOT}/projects/non-oci-service"
NON_OCI_CHART_DIR="${NON_OCI_SERVICE}/chart"
NON_OCI_DEPLOY_DIR="${NON_OCI_SERVICE}/deploy"
mkdir -p "${NON_OCI_CHART_DIR}" "${NON_OCI_DEPLOY_DIR}"
cat >"${NON_OCI_CHART_DIR}/BUILD" <<'BUILD'
load("//bazel:helm.bzl", "helm_chart")
helm_chart(
    name = "chart",
)
BUILD

# Service with no chart/BUILD at all
NO_BUILD_SERVICE="${FAKE_ROOT}/projects/no-build-service"
NO_BUILD_DEPLOY_DIR="${NO_BUILD_SERVICE}/deploy"
mkdir -p "${NO_BUILD_DEPLOY_DIR}"

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
# Shared content snippets
# ---------------------------------------------------------------------------
OCI_VALUES_PATH="${OCI_DEPLOY_DIR}/values.yaml"

# Content with image.tag under image: block
WITH_TAG='replicaCount: 1
image:
  repository: registry.example.com/myapp
  tag: "v1.2.3"
  pullPolicy: IfNotPresent
'

# Content with image block but no tag (safe)
WITHOUT_TAG='replicaCount: 1
image:
  repository: registry.example.com/myapp
  pullPolicy: IfNotPresent
'

# Content where "tag:" appears outside an image: block (should not trigger)
TAG_NOT_IN_IMAGE='metadata:
  labels:
    tag: myapp-label
image:
  repository: registry.example.com/myapp
  pullPolicy: IfNotPresent
'

# ---------------------------------------------------------------------------
# Test cases
# ---------------------------------------------------------------------------

# 1. OCI chart + image.tag in Write content → warns
run_test "write_oci_with_image_tag_warns" \
	"$(make_write_json "$OCI_VALUES_PATH" "$WITH_TAG")" \
	0 "WARNING.*OCI-pinned"

# 2. OCI chart + no image.tag in Write content → silent
run_test "write_oci_without_image_tag_silent" \
	"$(make_write_json "$OCI_VALUES_PATH" "$WITHOUT_TAG")" \
	0 ""

# 3. OCI chart + image.tag in Edit new_string → warns
run_test "edit_oci_with_image_tag_warns" \
	"$(make_edit_json "$OCI_VALUES_PATH" "$WITH_TAG")" \
	0 "WARNING"

# 4. OCI chart + no image.tag in Edit new_string → silent
run_test "edit_oci_without_image_tag_silent" \
	"$(make_edit_json "$OCI_VALUES_PATH" "$WITHOUT_TAG")" \
	0 ""

# 5. Non-OCI chart (publish not True) + image.tag → silent
NON_OCI_VALUES_PATH="${NON_OCI_DEPLOY_DIR}/values.yaml"
run_test "non_oci_with_image_tag_silent" \
	"$(make_write_json "$NON_OCI_VALUES_PATH" "$WITH_TAG")" \
	0 ""

# 6. No chart/BUILD at all + image.tag → silent
NO_BUILD_VALUES_PATH="${NO_BUILD_DEPLOY_DIR}/values.yaml"
run_test "no_build_file_with_image_tag_silent" \
	"$(make_write_json "$NO_BUILD_VALUES_PATH" "$WITH_TAG")" \
	0 ""

# 7. Wrong file path (not deploy/values.yaml) → skip
run_test "wrong_file_path_skipped" \
	"$(make_write_json "${OCI_DEPLOY_DIR}/other.yaml" "$WITH_TAG")" \
	0 ""

# 8. File not under projects/*/deploy/ pattern → skip
run_test "not_under_projects_deploy_skipped" \
	"$(make_write_json "${FAKE_ROOT}/config/values.yaml" "$WITH_TAG")" \
	0 ""

# 9. Empty JSON → skip
run_test "empty_json_skipped" \
	'{}' \
	0 ""

# 10. Missing file_path → skip
run_test "missing_file_path_skipped" \
	'{"tool_name":"Write","tool_input":{"content":"image:\n  tag: v1"}}' \
	0 ""

# 11. tag: key outside an image: block → silent (not a nested image.tag)
run_test "tag_outside_image_block_silent" \
	"$(make_write_json "$OCI_VALUES_PATH" "$TAG_NOT_IN_IMAGE")" \
	0 ""

# 12. Warning message includes service name
run_test "warning_includes_service_name" \
	"$(make_write_json "$OCI_VALUES_PATH" "$WITH_TAG")" \
	0 "oci-service"

# 13. Warning message mentions image.tag / tag
run_test "warning_mentions_image_tag" \
	"$(make_write_json "$OCI_VALUES_PATH" "$WITH_TAG")" \
	0 "image\.tag|tag.*overlay|overlay.*tag"

# 14. Multiple image blocks — tag in second block also triggers
MULTI_IMAGE='service1:
  image:
    repository: registry.example.com/svc1
    pullPolicy: IfNotPresent
service2:
  image:
    repository: registry.example.com/svc2
    tag: "v2.0.0"
    pullPolicy: IfNotPresent
'
run_test "tag_in_second_image_block_warns" \
	"$(make_write_json "$OCI_VALUES_PATH" "$MULTI_IMAGE")" \
	0 "WARNING"

# 15. Empty content (no new_string / content) → skip
run_test "empty_content_skipped" \
	"$(
		python3 - "$OCI_VALUES_PATH" <<'PY'
import json, sys
fp = sys.argv[1]
print(json.dumps({"tool_name": "Write", "tool_input": {"file_path": fp, "content": ""}}))
PY
	)" \
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
