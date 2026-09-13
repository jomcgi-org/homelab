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

TRACKED_UNUSUAL_REPO="$TMP/tracked unusual repository"
make_repo "$TRACKED_UNUSUAL_REPO"
tracked_unusual_path="$TRACKED_UNUSUAL_REPO/projects/alpha/chart/templates/café.yaml"
printf 'kind: ConfigMap\n' >"$tracked_unusual_path"
git -C "$TRACKED_UNUSUAL_REPO" add .
git -C "$TRACKED_UNUSUAL_REPO" commit -qm 'add unusual tracked path'
git -C "$TRACKED_UNUSUAL_REPO" branch -f test-base
printf 'changed: true\n' >>"$tracked_unusual_path"
set +e
(
	cd "$TRACKED_UNUSUAL_REPO"
	HELM="$TOOLS/helm" \
		HELM_CALLS="$CALLS/tracked-unusual.args" \
		"$SCRIPT" --base test-base
) >"$CALLS/tracked-unusual.out" 2>&1
lint_status=$?
set -e
expect_status "tracked Unicode paths select their chart" 0 "$lint_status"
expect_contains "tracked Unicode path keeps its chart path" "projects/alpha/chart" "$CALLS/tracked-unusual.args"
call_count="$(grep -c '^CALL$' "$CALLS/tracked-unusual.args")"
expect_status "tracked unusual path lints its chart once" 1 "$call_count"

UNTRACKED_UNUSUAL_REPO="$TMP/untracked unusual repository"
make_repo "$UNTRACKED_UNUSUAL_REPO"
untracked_unusual_path="$UNTRACKED_UNUSUAL_REPO/projects/alpha/chart/templates/"$'tab\tquote"back\\slash\nline.yaml'
printf 'kind: ConfigMap\n' >"$untracked_unusual_path"
set +e
(
	cd "$UNTRACKED_UNUSUAL_REPO"
	HELM="$TOOLS/helm" \
		HELM_CALLS="$CALLS/untracked-unusual.args" \
		"$SCRIPT" --base test-base
) >"$CALLS/untracked-unusual.out" 2>&1
lint_status=$?
set -e
expect_status "untracked unusual paths select their chart" 0 "$lint_status"
expect_contains "untracked unusual path keeps its chart path" "projects/alpha/chart" "$CALLS/untracked-unusual.args"
call_count="$(grep -c '^CALL$' "$CALLS/untracked-unusual.args")"
expect_status "untracked unusual path lints its chart once" 1 "$call_count"

CROSS_CHART_RENAME_REPO="$TMP/cross chart rename repository"
make_repo "$CROSS_CHART_RENAME_REPO"
mkdir -p "$CROSS_CHART_RENAME_REPO/projects/beta/chart/templates"
printf 'apiVersion: v2\nname: beta\nversion: 0.1.0\n' >"$CROSS_CHART_RENAME_REPO/projects/beta/chart/Chart.yaml"
printf 'kind: ConfigMap\n' >"$CROSS_CHART_RENAME_REPO/projects/alpha/chart/templates/move.yaml"
git -C "$CROSS_CHART_RENAME_REPO" add .
git -C "$CROSS_CHART_RENAME_REPO" commit -qm 'add rename fixtures'
git -C "$CROSS_CHART_RENAME_REPO" branch -f test-base
git -C "$CROSS_CHART_RENAME_REPO" mv \
	projects/alpha/chart/templates/move.yaml \
	projects/beta/chart/templates/move.yaml
set +e
(
	cd "$CROSS_CHART_RENAME_REPO"
	HELM="$TOOLS/helm" \
		HELM_CALLS="$CALLS/cross-chart-rename.args" \
		"$SCRIPT" --base test-base
) >"$CALLS/cross-chart-rename.out" 2>&1
lint_status=$?
set -e
expect_status "cross-chart rename lints successfully" 0 "$lint_status"
expect_contains "cross-chart rename selects source chart" "projects/alpha/chart" "$CALLS/cross-chart-rename.args"
expect_contains "cross-chart rename selects destination chart" "projects/beta/chart" "$CALLS/cross-chart-rename.args"
call_count="$(grep -c '^CALL$' "$CALLS/cross-chart-rename.args")"
expect_status "cross-chart rename lints both charts" 2 "$call_count"

RENAME_OUT_REPO="$TMP/rename out repository"
make_repo "$RENAME_OUT_REPO"
printf 'kind: ConfigMap\n' >"$RENAME_OUT_REPO/projects/alpha/chart/templates/move.yaml"
git -C "$RENAME_OUT_REPO" add .
git -C "$RENAME_OUT_REPO" commit -qm 'add move-out fixture'
git -C "$RENAME_OUT_REPO" branch -f test-base
git -C "$RENAME_OUT_REPO" mv \
	projects/alpha/chart/templates/move.yaml \
	docs/move.yaml
set +e
(
	cd "$RENAME_OUT_REPO"
	HELM="$TOOLS/helm" \
		HELM_CALLS="$CALLS/rename-out.args" \
		"$SCRIPT" --base test-base
) >"$CALLS/rename-out.out" 2>&1
lint_status=$?
set -e
expect_status "chart-to-non-chart rename lints successfully" 0 "$lint_status"
expect_contains "chart-to-non-chart rename selects source chart" "projects/alpha/chart" "$CALLS/rename-out.args"
call_count="$(grep -c '^CALL$' "$CALLS/rename-out.args")"
expect_status "chart-to-non-chart rename lints source chart once" 1 "$call_count"

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
