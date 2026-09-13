#!/usr/bin/env bash
# Focused tests for the standalone Helm render entry point.

set -o errexit -o nounset -o pipefail

SCRIPT_REL="bazel/helm/render.sh"
SCRIPT=""
for candidate in \
	"${RUNFILES_DIR:-}/_main/${SCRIPT_REL}" \
	"${TEST_SRCDIR:-}/_main/${SCRIPT_REL}" \
	"${BASH_SOURCE[0]%/*}/render.sh"; do
	if [[ -f "$candidate" ]]; then
		SCRIPT="$candidate"
		break
	fi
done
[[ -n "$SCRIPT" ]] || {
	echo "ERROR: cannot locate $SCRIPT_REL" >&2
	exit 1
}
SCRIPT="$(cd "$(dirname "$SCRIPT")" && pwd -P)/$(basename "$SCRIPT")"
[[ -x "$SCRIPT" ]] || {
	echo "ERROR: $SCRIPT_REL is not executable" >&2
	exit 1
}

TMP="${TEST_TMPDIR:-$(mktemp -d)}"
REPO="$TMP/repository with space"
TOOLS="$TMP/tools"
CALLS="$TMP/calls"
mkdir -p "$REPO/projects/team/demo service/chart/config" "$REPO/work/elsewhere" "$TOOLS" "$CALLS"
git -C "$REPO" init -q
git -C "$REPO" config user.email test@example.com
git -C "$REPO" config user.name Test

cat >"$REPO/projects/team/demo service/chart/Chart.yaml" <<'EOF'
apiVersion: v2
name: 'demo-service'
version: 0.1.0
EOF
printf 'replicas: 2\n' >"$REPO/projects/team/demo service/chart/config/values with space.yaml"
git -C "$REPO" add .
git -C "$REPO" commit -qm base

cat >"$TOOLS/helm" <<'EOF'
#!/usr/bin/env bash
printf '%s\n' "$@" >"$HELM_ARGS_FILE"
printf 'partial output\n'
if [[ "${HELM_RC:-0}" -ne 0 ]]; then
	exit "$HELM_RC"
fi
printf 'apiVersion: v1\nkind: ConfigMap\n'
EOF
cat >"$TOOLS/bazel" <<'EOF'
#!/usr/bin/env bash
touch "$BAZEL_CALLED"
exit 99
EOF
chmod +x "$TOOLS/helm" "$TOOLS/bazel"

failures=0
expect_status() {
	local description="$1" want="$2" got="$3"
	if [[ "$want" == "$got" ]]; then
		echo "PASS: $description"
	else
		echo "FAIL: $description, want $want, got $got" >&2
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

expect_contains() {
	local description="$1" text="$2" file="$3"
	if grep -Fq -- "$text" "$file"; then
		echo "PASS: $description"
	else
		echo "FAIL: $description, missing '$text' in $file" >&2
		failures=$((failures + 1))
	fi
}

run_render() {
	local name="$1"
	shift
	set +e
	(
		cd "$REPO/work/elsewhere"
		PATH="$TOOLS:$PATH" \
			HELM="$TOOLS/helm" \
			HELM_ARGS_FILE="$CALLS/$name.args" \
			BAZEL_CALLED="$CALLS/bazel-called" \
			"$SCRIPT" "$@"
	) >"$CALLS/$name.out" 2>&1
	render_status=$?
	set -e
}

run_render success "team/demo service" \
	--release "demo release" \
	--namespace "demo namespace" \
	--values "projects/team/demo service/chart/config/values with space.yaml"
expect_status "render succeeds from a nested working directory" 0 "$render_status"
OUTPUT="$REPO/projects/team/demo service/chart/manifests/all.yaml"
expect_contains "default output contains rendered resources" "kind: ConfigMap" "$OUTPUT"
expect_line "release name stays one argument" "demo release" "$CALLS/success.args"
expect_line "chart path with spaces stays one argument" "$REPO/projects/team/demo service/chart" "$CALLS/success.args"
expect_line "namespace stays one argument" "demo namespace" "$CALLS/success.args"
expect_line "values path with spaces stays one argument" "$REPO/projects/team/demo service/chart/config/values with space.yaml" "$CALLS/success.args"

run_render stdout "$REPO/projects/team/demo service/chart" --output -
expect_status "absolute in-repository chart path is accepted" 0 "$render_status"
expect_contains "Chart.yaml supplies the default release" "demo-service" "$CALLS/stdout.args"
expect_contains "stdout output is emitted" "kind: ConfigMap" "$CALLS/stdout.out"

printf 'previous complete output\n' >"$OUTPUT"
set +e
(
	cd "$REPO"
	PATH="$TOOLS:$PATH" \
		HELM="$TOOLS/helm" \
		HELM_RC=17 \
		HELM_ARGS_FILE="$CALLS/failure.args" \
		BAZEL_CALLED="$CALLS/bazel-called" \
		"$SCRIPT" "team/demo service"
) >"$CALLS/failure.out" 2>&1
render_status=$?
set -e
expect_status "Helm failure status is preserved" 17 "$render_status"
expect_line "failed render preserves prior output" "previous complete output" "$OUTPUT"

run_render missing_values "team/demo service" --values missing.yaml
expect_status "missing values file fails" 1 "$render_status"
expect_contains "missing values path is reported" "values file not found: missing.yaml" "$CALLS/missing_values.out"

set +e
(
	cd "$REPO"
	HELM=missing-helm-command "$SCRIPT" "team/demo service"
) >"$CALLS/missing_tool.out" 2>&1
render_status=$?
set -e
expect_status "missing Helm fails" 1 "$render_status"
expect_contains "missing Helm names the prerequisite" "run ./bootstrap.sh" "$CALLS/missing_tool.out"

run_render outside_output "team/demo service" --output "$TMP/outside.yaml"
expect_status "output outside the repository is rejected" 1 "$render_status"

run_render traversal_output "team/demo service" --output projects/../../outside.yaml
expect_status "output path traversal is rejected" 1 "$render_status"

run_render extra_argument "team/demo service" second
expect_status "extra positional argument is rejected" 2 "$render_status"

if [[ -e "$CALLS/bazel-called" ]]; then
	echo "FAIL: a local Bazel executable was invoked" >&2
	failures=$((failures + 1))
else
	echo "PASS: no local Bazel executable is invoked"
fi

if [[ $failures -ne 0 ]]; then
	echo "$failures test(s) failed" >&2
	exit 1
fi
echo "All standalone render tests passed"
