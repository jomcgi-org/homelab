#!/usr/bin/env bash
# Focused tests for changed-chart Helm linting.

set -o errexit -o nounset -o pipefail

SCRIPT_REL="bazel/helm/lint.sh"
SCRIPT=""
for candidate in \
	"${RUNFILES_DIR:-}/_main/${SCRIPT_REL}" \
	"${TEST_SRCDIR:-}/_main/${SCRIPT_REL}" \
	"${BASH_SOURCE[0]%/*}/lint.sh"; do
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
TOOLS="$TMP/tools"
CALLS="$TMP/calls"
mkdir -p "$TOOLS" "$CALLS"

cat >"$TOOLS/helm" <<'EOF'
#!/usr/bin/env bash
printf 'CALL\n' >>"$HELM_CALLS"
printf '%s\n' "$@" >>"$HELM_CALLS"
exit "${HELM_RC:-0}"
EOF
cat >"$TOOLS/bazel" <<'EOF'
#!/usr/bin/env bash
touch "$BAZEL_CALLED"
exit 99
EOF
chmod +x "$TOOLS/helm" "$TOOLS/bazel"

make_repo() {
	local repo="$1"
	mkdir -p "$repo/projects/alpha/chart/templates" \
		"$repo/projects/team/chart with space/templates" "$repo/docs"
	git -C "$repo" init -q
	git -C "$repo" config user.email test@example.com
	git -C "$repo" config user.name Test
	printf 'apiVersion: v2\nname: alpha\nversion: 0.1.0\n' >"$repo/projects/alpha/chart/Chart.yaml"
	printf 'kind: ConfigMap\n' >"$repo/projects/alpha/chart/templates/one.yaml"
	printf 'kind: Service\n' >"$repo/projects/alpha/chart/templates/deleted.yaml"
	printf 'apiVersion: v2\nname: spaced\nversion: 0.1.0\n' >"$repo/projects/team/chart with space/Chart.yaml"
	printf 'kind: ConfigMap\n' >"$repo/projects/team/chart with space/templates/one.yaml"
	printf 'docs\n' >"$repo/docs/readme.md"
	git -C "$repo" add .
	git -C "$repo" commit -qm base
	git -C "$repo" branch test-base
}

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

expect_contains() {
	local description="$1" text="$2" file="$3"
	if grep -Fq -- "$text" "$file"; then
		echo "PASS: $description"
	else
		echo "FAIL: $description, missing '$text' in $file" >&2
		failures=$((failures + 1))
	fi
}

REPO="$TMP/changed repository"
make_repo "$REPO"
printf 'kind: Secret\n' >>"$REPO/projects/alpha/chart/templates/one.yaml"
rm "$REPO/projects/alpha/chart/templates/deleted.yaml"
printf 'kind: Secret\n' >>"$REPO/projects/team/chart with space/templates/one.yaml"
printf 'unrelated\n' >>"$REPO/docs/readme.md"

set +e
(
	cd "$REPO/projects"
	PATH="$TOOLS:$PATH" \
		HELM="$TOOLS/helm" \
		HELM_CALLS="$CALLS/success.args" \
		BAZEL_CALLED="$CALLS/bazel-called" \
		"$SCRIPT" --base test-base
) >"$CALLS/success.out" 2>&1
lint_status=$?
set -e
expect_status "changed charts lint successfully from a nested directory" 0 "$lint_status"
expect_contains "strict lint flag is passed" "--strict" "$CALLS/success.args"
expect_contains "first changed chart is selected" "projects/alpha/chart" "$CALLS/success.args"
expect_contains "chart path with spaces stays one argument" "projects/team/chart with space" "$CALLS/success.args"
call_count="$(grep -c '^CALL$' "$CALLS/success.args")"
expect_status "each changed chart is linted once" 2 "$call_count"

DOCS_REPO="$TMP/docs repository"
make_repo "$DOCS_REPO"
printf 'changed\n' >>"$DOCS_REPO/docs/readme.md"
set +e
(
	cd "$DOCS_REPO"
	HELM=missing-helm-command "$SCRIPT" --base test-base
) >"$CALLS/docs.out" 2>&1
lint_status=$?
set -e
expect_status "unrelated changes need no Helm executable" 0 "$lint_status"
expect_contains "unrelated changes report no charts" "No changed Helm charts" "$CALLS/docs.out"

set +e
(
	cd "$REPO"
	HELM=missing-helm-command "$SCRIPT" --base test-base
) >"$CALLS/missing_tool.out" 2>&1
lint_status=$?
set -e
expect_status "missing Helm fails when a chart changed" 1 "$lint_status"
expect_contains "missing Helm names the prerequisite" "run ./bootstrap.sh" "$CALLS/missing_tool.out"

set +e
(
	cd "$REPO"
	PATH="$TOOLS:$PATH" \
		HELM="$TOOLS/helm" \
		HELM_RC=23 \
		HELM_CALLS="$CALLS/error.args" \
		BAZEL_CALLED="$CALLS/bazel-called" \
		"$SCRIPT" --base test-base
) >"$CALLS/error.out" 2>&1
lint_status=$?
set -e
expect_status "Helm lint failure status is preserved" 23 "$lint_status"
expect_contains "Helm error identifies the failed chart" "helm lint failed for projects/alpha/chart" "$CALLS/error.out"

set +e
(
	cd "$REPO"
	"$SCRIPT" --base missing-ref
) >"$CALLS/missing_base.out" 2>&1
lint_status=$?
set -e
expect_status "invalid base ref fails" 1 "$lint_status"
expect_contains "invalid base ref is reported" "base ref does not resolve" "$CALLS/missing_base.out"

set +e
(
	cd "$REPO"
	"$SCRIPT" unexpected
) >"$CALLS/arguments.out" 2>&1
lint_status=$?
set -e
expect_status "unexpected argument fails usage" 2 "$lint_status"

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
echo "All standalone lint tests passed"
