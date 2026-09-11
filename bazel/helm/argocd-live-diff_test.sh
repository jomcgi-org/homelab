#!/usr/bin/env bash
# Hermetic regression tests for argocd-live-diff.sh.

set -o errexit -o nounset -o pipefail

SCRIPT_REL="bazel/helm/argocd-live-diff.sh"
SCRIPT=""
for candidate in \
	"${RUNFILES_DIR:-}/_main/${SCRIPT_REL}" \
	"${TEST_SRCDIR:-}/_main/${SCRIPT_REL}" \
	"${BASH_SOURCE[0]%/*}/argocd-live-diff.sh"; do
	if [[ -f "$candidate" ]]; then
		SCRIPT="$candidate"
		break
	fi
done
[[ -n "$SCRIPT" ]] || { echo "ERROR: cannot locate $SCRIPT_REL" >&2; exit 1; }
[[ -x "$SCRIPT" ]] || { echo "ERROR: $SCRIPT_REL is not executable" >&2; exit 1; }

TMP="${TEST_TMPDIR:-$(mktemp -d)}"
FAKE_RUNFILES="$TMP/runfiles"
TOOLS="$FAKE_RUNFILES/multitool/tools"
CHART="$FAKE_RUNFILES/_main/projects/demo/chart"
OVERLAY="$FAKE_RUNFILES/_main/projects/demo/deploy"
UNRELATED="$TMP/unrelated working directory"
mkdir -p "$TOOLS" "$CHART/templates" "$OVERLAY" "$UNRELATED"
printf 'apiVersion: v2\nname: demo\nversion: 0.1.0\n' >"$CHART/Chart.yaml"
printf 'replicas: 1\n' >"$CHART/values.yaml"
printf 'feature: enabled\n' >"$OVERLAY/values with space.yaml"

cat >"$TOOLS/helm" <<'EOF'
#!/usr/bin/env bash
printf '%s\n' "$@" >"$CALLS/helm.args"
if [[ "${HELM_RC:-0}" -ne 0 ]]; then
	exit "$HELM_RC"
fi
printf 'apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: rendered\n'
EOF

cat >"$TOOLS/argocd" <<'EOF'
#!/usr/bin/env bash
printf '%s\n' "$@" >"$CALLS/argocd.args"
local_dir=""
while [[ "$#" -gt 0 ]]; do
	if [[ "$1" == "--local" ]]; then
		local_dir="$2"
		shift 2
		continue
	fi
	shift
done
[[ -f "$local_dir/all.yaml" ]] || exit 90
grep -q '^kind: ConfigMap$' "$local_dir/all.yaml" || exit 91
exit "${ARGOCD_RC:-0}"
EOF

cat >"$TOOLS/op" <<'EOF'
#!/usr/bin/env bash
exit 1
EOF
chmod +x "$TOOLS/helm" "$TOOLS/argocd" "$TOOLS/op"

failures=0
expect_equal() {
	local description="$1" want="$2" got="$3"
	if [[ "$want" == "$got" ]]; then
		echo "PASS: $description"
	else
		echo "FAIL: $description, want '$want', got '$got'" >&2
		failures=$((failures + 1))
	fi
}

expect_line() {
	local description="$1" line="$2" file="$3"
	if grep -Fxq -- "$line" "$file"; then
		echo "PASS: $description"
	else
		echo "FAIL: $description, missing '$line' in $file" >&2
		failures=$((failures + 1))
	fi
}

run_case() {
	local name="$1" helm_rc="$2" argocd_rc="$3"
	local case_tmp="$TMP/$name-tmp"
	CALLS="$TMP/$name-calls"
	mkdir -p "$case_tmp" "$CALLS"
	rm -f "$CALLS/helm.args" "$CALLS/argocd.args"

	set +e
	(
		cd "$UNRELATED"
		RUNFILES_DIR="$FAKE_RUNFILES" \
		TEST_TMPDIR="$case_tmp" \
		CALLS="$CALLS" \
		HELM_RC="$helm_rc" \
		ARGOCD_RC="$argocd_rc" \
		HELM="../multitool/tools/helm" \
		ARGOCD="../multitool/tools/argocd" \
		OP="../multitool/tools/op" \
		ARGOCD_APP_NAME="demo app" \
		CHART_FILE="projects/demo/chart/Chart.yaml" \
		RELEASE_NAME="demo release" \
		NAMESPACE="demo namespace" \
		VALUES_FILES=$'projects/demo/chart/values.yaml\nprojects/demo/deploy/values with space.yaml' \
			"$SCRIPT"
	) >"$TMP/$name.out" 2>&1
	case_rc=$?
	set -e

	if find "$case_tmp" -mindepth 1 -maxdepth 1 -name 'argocd-live-diff.*' | grep -q .; then
		echo "FAIL: $name left rendered manifests behind" >&2
		failures=$((failures + 1))
	fi
}

run_case success 0 0
expect_equal "successful comparison exits zero" 0 "$case_rc"
expect_line "release name survives argument boundaries" "demo release" "$CALLS/helm.args"
expect_line "namespace survives argument boundaries" "demo namespace" "$CALLS/helm.args"
expect_line "chart resolves from runfiles" "$CHART" "$CALLS/helm.args"
expect_line "spaced values path survives argument boundaries" "$OVERLAY/values with space.yaml" "$CALLS/helm.args"
expect_line "application name survives argument boundaries" "demo app" "$CALLS/argocd.args"
expect_line "diff requests meaningful exit codes" "--exit-code" "$CALLS/argocd.args"
expect_line "diff exit status is one" "1" "$CALLS/argocd.args"

run_case differences 0 1
expect_equal "differences propagate exit one" 1 "$case_rc"
expect_line "differences are reported" "Differences found" "$TMP/differences.out"

run_case argocd_failure 0 23
expect_equal "ArgoCD failures preserve their exit status" 23 "$case_rc"
expect_line "ArgoCD failure status is reported" "ArgoCD diff failed with exit status 23" "$TMP/argocd_failure.out"

run_case helm_failure 17 0
expect_equal "Helm failures preserve their exit status" 17 "$case_rc"
if [[ -e "$CALLS/argocd.args" ]]; then
	echo "FAIL: ArgoCD ran after Helm failed" >&2
	failures=$((failures + 1))
else
	echo "PASS: ArgoCD does not run after Helm fails"
fi

if [[ "$failures" -ne 0 ]]; then
	echo "$failures test(s) failed" >&2
	exit 1
fi

echo "All ArgoCD live diff tests passed"
