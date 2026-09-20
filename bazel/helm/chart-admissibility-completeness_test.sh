#!/usr/bin/env bash
# Hermetic regression tests for chart-admissibility-completeness-test.sh.

set -o errexit -o nounset -o pipefail

SCRIPT_REL="bazel/helm/chart-admissibility-completeness-test.sh"
SCRIPT=""
for candidate in \
	"${RUNFILES_DIR:-}/_main/${SCRIPT_REL}" \
	"${TEST_SRCDIR:-}/_main/${SCRIPT_REL}" \
	"${BASH_SOURCE[0]%/*}/chart-admissibility-completeness-test.sh"; do
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

TMP="${TEST_TMPDIR:-$(mktemp -d)}"
failures=0

new_repo() {
	local name="$1"
	local repo="$TMP/$name"
	mkdir -p "$repo/bazel/helm" "$repo/projects"
	printf 'MANIFEST_SETS = []\nSKIPPED_MANIFEST_SETS = {}\n' >"$repo/bazel/helm/BUILD"
	echo "$repo"
}

add_chart() {
	local repo="$1" chart_dir="$2"
	mkdir -p "$repo/$chart_dir"
	printf 'apiVersion: v2\nname: fixture\nversion: 0.1.0\n' >"$repo/$chart_dir/Chart.yaml"
}

add_deploy_values() {
	local repo="$1" deploy_dir="$2" filename="${3:-values.yaml}"
	mkdir -p "$repo/$deploy_dir"
	printf 'fixture: true\n' >"$repo/$deploy_dir/$filename"
}

run_check() {
	local repo="$1" output="$2"
	set +e
	bash "$SCRIPT" "$SCRIPT" "$repo" >"$output" 2>&1
	local status=$?
	set -e
	echo "$status"
}

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

# A platform chart outside both registry lists must fail closed.
repo=$(new_repo missing-platform)
add_chart "$repo" projects/platform/scratch
status=$(run_check "$repo" "$repo/output")
expect_status "unregistered platform chart fails" 1 "$status"
expect_contains "missing platform chart is named" \
	"missing chart admissibility coverage: projects/platform/scratch" "$repo/output"

# A platform chart's direct render registration covers its Chart.yaml.
repo=$(new_repo registered-platform)
add_chart "$repo" projects/platform/registered
printf 'MANIFEST_SETS = [\n    "//projects/platform/registered:manifests/all.yaml",\n]\n' \
	>"$repo/bazel/helm/BUILD"
status=$(run_check "$repo" "$repo/output")
expect_status "registered platform render passes" 0 "$status"

# A directory-level documented skip covers a chart that renders nothing.
repo=$(new_repo skipped-platform)
add_chart "$repo" projects/platform/skipped
printf 'SKIPPED_MANIFEST_SETS = {\n    "projects/platform/skipped": "Fixture skip.",\n}\n' \
	>"$repo/bazel/helm/BUILD"
status=$(run_check "$repo" "$repo/output")
expect_status "directory skip passes" 0 "$status"

# Both real library layouts must honor their existing directory skip keys.
repo=$(new_repo library-skips)
add_chart "$repo" projects/platform/cf-ingress-library
add_chart "$repo" projects/shared/helm/homelab-library/chart
printf '%s\n' \
	'SKIPPED_MANIFEST_SETS = {' \
	'    "projects/platform/cf-ingress-library": "Library chart.",' \
	'    "projects/shared/helm/homelab-library": "Library chart.",' \
	'}' >"$repo/bazel/helm/BUILD"
status=$(run_check "$repo" "$repo/output")
expect_status "both library skips pass" 0 "$status"

# Helm dependencies downloaded below charts/ are not first-party sources.
repo=$(new_repo vendored-dependency)
add_chart "$repo" projects/platform/parent/charts/dependency
add_chart "$repo" projects/platform/parent/charts/chart
status=$(run_check "$repo" "$repo/output")
expect_status "vendored chart dependencies are excluded" 0 "$status"

# Existing named chart and deploy-value discovery still use the sibling render.
repo=$(new_repo named-application)
add_chart "$repo" projects/application/chart
add_deploy_values "$repo" projects/application/deploy
printf 'MANIFEST_SETS = [\n    "//projects/application/deploy:manifests/all.yaml",\n]\n' \
	>"$repo/bazel/helm/BUILD"
status=$(run_check "$repo" "$repo/output")
expect_status "registered named application chart passes" 0 "$status"

repo=$(new_repo missing-application)
add_chart "$repo" projects/application/chart
add_deploy_values "$repo" projects/application/deploy
status=$(run_check "$repo" "$repo/output")
expect_status "unregistered named application fails" 1 "$status"
expect_contains "missing deploy values are still found" \
	"missing chart admissibility coverage: projects/application/deploy/values.yaml" "$repo/output"
expect_contains "missing named chart is still found" \
	"missing chart admissibility coverage: projects/application/chart" "$repo/output"

# Published charts below helm/ use the owning application's deploy render.
repo=$(new_repo helm-application)
add_chart "$repo" projects/operators/cache/helm/cache-operator
add_deploy_values "$repo" projects/operators/cache/deploy
printf 'MANIFEST_SETS = [\n    "//projects/operators/cache/deploy:manifests/all.yaml",\n]\n' \
	>"$repo/bazel/helm/BUILD"
status=$(run_check "$repo" "$repo/output")
expect_status "helm application chart uses sibling deploy render" 0 "$status"

if [[ $failures -ne 0 ]]; then
	exit 1
fi
