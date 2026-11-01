#!/usr/bin/env bash
# Regression tests for helm_lint_test's optional default values.yaml.
#
# Library charts (type: library) ship Chart.yaml and templates only. The lint
# macro must not require a default values.yaml input for them, while keeping
# Chart.yaml, templates, vendored subcharts, explicit extra_values, --strict
# and nonzero-exit propagation unchanged for application charts.
#
# The test executes the real genrule script text from bazel/helm/test.bzl
# (with genrule's documented $$ -> $ unescaping applied) against a stub helm,
# and checks the macro's data rule in that same file. HELM_LINT_TEST_BZL
# overrides which test.bzl is examined, so the pre-fix file fails this test
# while the fixed file passes it.
#
# Dual-mode like lint_test.sh: runs under `bazel test` (RUNFILES_DIR) and as
# `bash bazel/helm/lint-inputs_test.sh` from a workspace checkout.

set -o nounset -o pipefail

# Real helm for the malformed-chart case. Under `bazel test` the repo-declared
# tool arrives as $1 via $(rootpath @multitool//tools/helm); local `bash`
# runs fall back to helm on PATH. An empty value means "not located yet".
HELM_ARG="${1:-}"
shift $# 2>/dev/null || true

locate_helm() {
	local candidate
	if [[ -n "${HELM_ARG:-}" && -x "$HELM_ARG" ]]; then
		printf '%s\n' "$HELM_ARG"
		return 0
	fi
	for candidate in \
		"${RUNFILES_DIR:-}/_main/external/multitool/tools/helm" \
		"${RUNFILES_DIR:-}/external/multitool/tools/helm" \
		"${TEST_SRCDIR:-}/_main/external/multitool/tools/helm"; do
		if [[ -n "$candidate" && -x "$candidate" ]]; then
			printf '%s\n' "$candidate"
			return 0
		fi
	done
	if command -v helm >/dev/null 2>&1; then
		command -v helm
		return 0
	fi
	return 1
}

BZL_REL="bazel/helm/test.bzl"
BZL="${HELM_LINT_TEST_BZL:-}"
if [[ -z "$BZL" ]]; then
	for candidate in \
		"${RUNFILES_DIR:-}/_main/${BZL_REL}" \
		"${TEST_SRCDIR:-}/_main/${BZL_REL}"; do
		if [[ -f "$candidate" ]]; then
			BZL="$candidate"
			break
		fi
	done
fi
if [[ -z "$BZL" ]]; then
	REPO_ROOT="$(git rev-parse --show-toplevel 2>/dev/null)" || {
		echo "ERROR: cannot locate $BZL_REL" >&2
		exit 1
	}
	BZL="$REPO_ROOT/$BZL_REL"
fi
[[ -f "$BZL" ]] || {
	echo "ERROR: cannot locate $BZL_REL (tried $BZL)" >&2
	exit 1
}

TESTLIB=""
for candidate in \
	"${RUNFILES_DIR:-}/_main/bazel/helm/testlib.sh" \
	"${TEST_SRCDIR:-}/_main/bazel/helm/testlib.sh" \
	"${BASH_SOURCE[0]%/*}/testlib.sh"; do
	if [[ -f "$candidate" ]]; then
		TESTLIB="$candidate"
		break
	fi
done
# shellcheck source=bazel/helm/testlib.sh
source "${TESTLIB:?cannot locate bazel/helm/testlib.sh}" || exit 1

TMP="${TEST_TMPDIR:-$(mktemp -d)}"
CALLS="$TMP/calls"
mkdir -p "$CALLS"

failures=0
expect_absent() {
	local description="$1" text="$2" file="$3"
	if [[ ! -r "$file" ]]; then
		echo "FAIL: $description, cannot read $file" >&2
		failures=$((failures + 1))
		return
	fi
	local grep_rc
	set +e
	grep -Fq -- "$text" "$file"
	grep_rc=$?
	set -e
	if [[ $grep_rc -eq 0 ]]; then
		echo "FAIL: $description, unexpectedly found '$text' in $file" >&2
		failures=$((failures + 1))
	elif [[ $grep_rc -eq 1 ]]; then
		echo "PASS: $description"
	else
		echo "FAIL: $description, grep error $grep_rc on $file" >&2
		failures=$((failures + 1))
	fi
}

require_readable() {
	local description="$1" file="$2"
	if [[ -r "$file" ]]; then
		echo "PASS: $description"
	else
		echo "FAIL: $description, cannot read $file" >&2
		failures=$((failures + 1))
		return 1
	fi
}

# Materialize the helm_lint_test genrule script exactly as Bazel would: the
# cmd writes the lines between `<< 'EOF'` and `EOF`, with genrule's documented
# $$ escape collapsed to a single $.
SCRIPT="$TMP/lint-under-test.sh"
awk '/<< .EOF./{flag=1;next} /^EOF$/{flag=0} flag' "$BZL" | sed 's/\$\$/\$/g' >"$SCRIPT"
chmod +x "$SCRIPT"
[[ -s "$SCRIPT" ]] || {
	echo "FAIL: extracted lint script is empty" >&2
	exit 1
}
echo "PASS: lint script materializes from $BZL"

# Stub helm: records its arguments, exits with HELM_RC.
STUB="$TMP/stub-helm.sh"
cat >"$STUB" <<'EOF'
#!/usr/bin/env bash
printf '%s\n' "$@" >>"$HELM_CALLS"
exit "${HELM_RC:-0}"
EOF
chmod +x "$STUB"

make_chart() {
	local dir="$1" kind="$2" with_values="$3"
	mkdir -p "$dir/templates" "$dir/charts/vendored"
	printf 'apiVersion: v2\nname: vendored\nversion: 0.1.0\n' >"$dir/charts/vendored/Chart.yaml"
	if [[ "$kind" == "library" ]]; then
		printf 'apiVersion: v2\nname: fixture-lib\ndescription: fixture\ntype: library\nversion: 0.1.0\n' >"$dir/Chart.yaml"
		printf '{{/* helper */}}\n' >"$dir/templates/_helper.tpl"
	else
		printf 'apiVersion: v2\nname: fixture-app\ndescription: fixture\nversion: 0.1.0\n' >"$dir/Chart.yaml"
		printf 'kind: ConfigMap\n' >"$dir/templates/one.yaml"
	fi
	if [[ "$with_values" == "yes" ]]; then
		printf 'key: value\n' >"$dir/values.yaml"
	fi
	printf 'extra: 1\n' >"$dir/extra-a.yaml"
	printf 'extra: 2\n' >"$dir/extra-b.yaml"
}

LIB="$TMP/fixture-lib"
APP="$TMP/fixture-app"
make_chart "$LIB" library no
make_chart "$APP" application yes

# 1. Library chart without values.yaml lints cleanly, no -f flags.
set +e
HELM_RC=0 HELM_CALLS="$CALLS/lib.args" "$SCRIPT" "$STUB" "$LIB/Chart.yaml" >"$CALLS/lib.out" 2>&1
lib_status=$?
set -e
expect_status "library chart without values.yaml passes lint" 0 "$lib_status"
expect_contains "library lint invocation runs helm lint" "lint" "$CALLS/lib.args"
expect_contains "library lint keeps --strict" "--strict" "$CALLS/lib.args"
expect_contains "library lint targets the chart directory" "$LIB" "$CALLS/lib.args"
expect_absent "library lint passes no -f flags" "-f" "$CALLS/lib.args"
expect_contains "library lint reports PASSED" "PASSED" "$CALLS/lib.out"

# 2. Application chart keeps values.yaml; explicit extras become ordered -f flags.
set +e
HELM_RC=0 HELM_CALLS="$CALLS/app.args" "$SCRIPT" "$STUB" "$APP/Chart.yaml" "$APP/extra-a.yaml" "$APP/extra-b.yaml" >"$CALLS/app.out" 2>&1
app_status=$?
set -e
expect_status "application chart with extras passes lint" 0 "$app_status"
expect_contains "application lint keeps --strict" "--strict" "$CALLS/app.args"
first_a="$(grep -n -F -- "$APP/extra-a.yaml" "$CALLS/app.args" | head -1 | cut -d: -f1)"
first_b="$(grep -n -F -- "$APP/extra-b.yaml" "$CALLS/app.args" | head -1 | cut -d: -f1)"
if [[ -n "$first_a" && -n "$first_b" && "$first_a" -lt "$first_b" ]]; then
	echo "PASS: extra values keep input order"
else
	echo "FAIL: extra values keep input order (a=$first_a b=$first_b)" >&2
	failures=$((failures + 1))
fi
flag_count="$(grep -c -- '^-f$' "$CALLS/app.args")"
expect_status "one -f flag per extra values file" 2 "$flag_count"

# 3. Missing Chart.yaml still fails before helm runs.
set +e
HELM_RC=0 HELM_CALLS="$CALLS/missing.args" "$SCRIPT" "$STUB" "$TMP/does-not-exist/Chart.yaml" >"$CALLS/missing.out" 2>&1
missing_status=$?
set -e
expect_status "missing Chart.yaml fails" 1 "$missing_status"
expect_contains "missing Chart.yaml is reported" "ERROR: Chart.yaml not found" "$CALLS/missing.out"
if [[ -e "$CALLS/missing.args" ]]; then
	echo "FAIL: helm is not invoked without Chart.yaml" >&2
	failures=$((failures + 1))
else
	echo "PASS: helm is not invoked without Chart.yaml"
fi

# 4. Nonzero helm lint exit propagates; PASSED is withheld.
set +e
HELM_RC=3 HELM_CALLS="$CALLS/rc.args" "$SCRIPT" "$STUB" "$LIB/Chart.yaml" >"$CALLS/rc.out" 2>&1
rc_status=$?
set -e
expect_status "helm lint failure propagates its exit code" 3 "$rc_status"
expect_absent "failed lint withholds PASSED" "PASSED" "$CALLS/rc.out"

# 5. The macro's data rule: default values.yaml optional, everything else required.
expect_contains "default values.yaml resolves via optional glob" 'native.glob(["values.yaml"], allow_empty = True)' "$BZL"
expect_absent "no unconditional default values.yaml input remains" '":values.yaml"' "$BZL"
expect_contains "Chart.yaml stays a required input" '":Chart.yaml"' "$BZL"
expect_contains "templates stay lint inputs" 'native.glob(["templates/**"], allow_empty = True)' "$BZL"
expect_contains "vendored subcharts stay lint inputs" 'native.glob(["charts/**"], allow_empty = True)' "$BZL"
expect_contains "explicit extra values stay lint inputs" "+ extra_values" "$BZL"
expect_contains "strict lint is retained" "--strict" "$BZL"

# 6. Resolved input sets: application charts unchanged, library charts buildable.
#    This mirrors Bazel's data resolution: required labels must exist, the
#    default values.yaml is included only when the file exists.
resolve_data() {
	local dir="$1"
	[[ -f "$dir/Chart.yaml" ]] || {
		echo "missing-required:Chart.yaml"
		return 1
	}
	local inputs=("Chart.yaml")
	[[ -f "$dir/values.yaml" ]] && inputs+=("values.yaml")
	inputs+=($(cd "$dir" && ls templates/* 2>/dev/null))
	inputs+=($(cd "$dir" && ls charts/**/* 2>/dev/null || true))
	printf '%s\n' "${inputs[@]}"
}
resolve_data "$APP" >"$CALLS/app.inputs"
expect_contains "application inputs keep Chart.yaml" "Chart.yaml" "$CALLS/app.inputs"
expect_contains "application inputs keep default values.yaml" "values.yaml" "$CALLS/app.inputs"
expect_contains "application inputs keep templates" "templates/one.yaml" "$CALLS/app.inputs"
expect_contains "application inputs keep vendored subcharts" "charts/vendored/Chart.yaml" "$CALLS/app.inputs"
set +e
resolve_data "$LIB" >"$CALLS/lib.inputs" 2>"$CALLS/lib.resolve.err"
lib_resolve=$?
set -e
expect_status "library inputs resolve without values.yaml" 0 "$lib_resolve"
expect_absent "library inputs omit values.yaml" "values.yaml" "$CALLS/lib.inputs"
expect_contains "library inputs keep Chart.yaml" "Chart.yaml" "$CALLS/lib.inputs"
expect_contains "library inputs keep templates" "templates/_helper.tpl" "$CALLS/lib.inputs"

# 7. Both restored library lint targets stay enabled via the macro default.
# Resolve each BUILD via Bazel runfiles first, then the source tree. A missing
# or unreadable BUILD fails closed: grep's nonzero-no-match exit must never
# mask a read error as a pass.
resolve_build() {
	local rel="$1"
	local candidate
	for candidate in \
		"${RUNFILES_DIR:-}/_main/$rel" \
		"${TEST_SRCDIR:-}/_main/$rel"; do
		if [[ -n "$candidate" && -r "$candidate" ]]; then
			printf '%s\n' "$candidate"
			return 0
		fi
	done
	local root
	root="$(git rev-parse --show-toplevel 2>/dev/null || pwd)"
	if [[ -r "$root/$rel" ]]; then
		printf '%s\n' "$root/$rel"
		return 0
	fi
	return 1
}
for build in "projects/platform/cf-ingress-library/BUILD" "projects/shared/helm/homelab-library/chart/BUILD"; do
	build_path="$(resolve_build "$build")" || {
		echo "FAIL: $build is not readable via runfiles or source tree" >&2
		failures=$((failures + 1))
		continue
	}
	require_readable "$build is readable" "$build_path" || continue
	set +e
	grep -Eq '^[[:space:]]*lint[[:space:]]*=[[:space:]]*False' "$build_path"
	grep_rc=$?
	set -e
	if [[ $grep_rc -eq 0 ]]; then
		echo "FAIL: $build disables lint" >&2
		failures=$((failures + 1))
	elif [[ $grep_rc -eq 1 ]]; then
		echo "PASS: $build keeps lint enabled"
	else
		echo "FAIL: $build grep error $grep_rc" >&2
		failures=$((failures + 1))
	fi
done

# 8. Declared runfiles are readable, and the repository Helm fails a
# malformed chart with --strict. This is the end-to-end guard the stub-only
# nonzero test cannot provide: it proves helm itself rejects bad input.
for runfiles_rel in \
	"bazel/helm/test.bzl" \
	"projects/platform/cf-ingress-library/Chart.yaml" \
	"projects/shared/helm/homelab-library/chart/Chart.yaml"; do
	found=""
	for candidate in \
		"${RUNFILES_DIR:-}/_main/$runfiles_rel" \
		"${TEST_SRCDIR:-}/_main/$runfiles_rel"; do
		if [[ -r "$candidate" ]]; then
			found="$candidate"
			break
		fi
	done
	if [[ -n "${RUNFILES_DIR:-}${TEST_SRCDIR:-}" ]]; then
		if [[ -n "$found" ]]; then
			echo "PASS: runfile $runfiles_rel is readable"
		else
			echo "FAIL: runfile $runfiles_rel missing under Bazel" >&2
			failures=$((failures + 1))
		fi
	else
		root="$(git rev-parse --show-toplevel 2>/dev/null || pwd)"
		require_readable "$runfiles_rel is readable" "$root/$runfiles_rel" || true
	fi
done

HELM_REAL=""
if HELM_REAL="$(locate_helm)"; then
	require_readable "repository helm tool is readable" "$HELM_REAL" || true
	BAD="$TMP/malformed-chart"
	mkdir -p "$BAD/templates"
	printf 'apiVersion: v2\nname: malformed-fixture\ndescription: fixture\nversion: 0.1.0\n' >"$BAD/Chart.yaml"
	printf '{{ .Values.unclosed\n' >"$BAD/templates/bad.yaml"
	set +e
	"$HELM_REAL" lint --strict "$BAD" >"$CALLS/real-helm-malformed.out" 2>&1
	real_rc=$?
	set -e
	if [[ $real_rc -ne 0 ]]; then
		echo "PASS: real helm lint --strict rejects malformed chart (rc=$real_rc)"
	else
		echo "FAIL: real helm lint --strict rejects malformed chart, want nonzero, got $real_rc" >&2
		failures=$((failures + 1))
	fi
	if grep -Eq 'Error|error|failed|parse|template' "$CALLS/real-helm-malformed.out"; then
		echo "PASS: malformed chart reports a lint error"
	else
		echo "FAIL: malformed chart reports no lint error" >&2
		failures=$((failures + 1))
	fi
	# Both real library charts still pass under the real helm.
	set +e
	ROOT="$(git rev-parse --show-toplevel 2>/dev/null || pwd)"
	"$HELM_REAL" lint --strict "$ROOT/projects/platform/cf-ingress-library" >"$CALLS/real-helm-lib-a.out" 2>&1
	real_lib_a_rc=$?
	"$HELM_REAL" lint --strict "$ROOT/projects/shared/helm/homelab-library/chart" >"$CALLS/real-helm-lib-b.out" 2>&1
	real_lib_b_rc=$?
	set -e
	expect_status "real helm lint --strict accepts cf-ingress-library" 0 "$real_lib_a_rc"
	expect_status "real helm lint --strict accepts homelab-library" 0 "$real_lib_b_rc"
else
	echo "FAIL: repository helm tool not found (arg, runfiles, or PATH)" >&2
	failures=$((failures + 1))
fi

if [[ $failures -ne 0 ]]; then
	echo "$failures test(s) failed" >&2
	exit 1
fi
echo "All helm lint input tests passed"
