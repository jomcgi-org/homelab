#!/usr/bin/env bash
# Unit tests for bump-chart.sh.
#
# The script under test:
#   - Resolves <project>/chart/Chart.yaml + <project>/deploy/application.yaml
#   - Computes the new version from max(local, origin/main) (main version is
#     injected via BUMP_CHART_MAIN_VERSION in these tests; no git needed)
#   - Rewrites Chart.yaml `version:` and the single semver `targetRevision:`
#     while leaving git-ref targetRevisions (HEAD/main) untouched
set -euo pipefail

SCRIPT_REL="bazel/tools/git/bump-chart.sh"
SCRIPT=""
for candidate in \
	"${RUNFILES_DIR:-}/_main/${SCRIPT_REL}" \
	"${TEST_SRCDIR:-}/_main/${SCRIPT_REL}" \
	"${BASH_SOURCE[0]%/*}/bump-chart.sh"; do
	if [[ -f "$candidate" ]]; then
		SCRIPT="$candidate"
		break
	fi
done
if [[ -z "$SCRIPT" ]]; then
	echo "ERROR: cannot locate bump-chart.sh in runfiles" >&2
	exit 1
fi

TMP="${TEST_TMPDIR:-$(mktemp -d)}"
FAILURES=0

setup_project() {
	# $1: root dir, $2: chart version, $3: targetRevision semver
	local root="$1" version="$2" tr="$3"
	rm -rf "$root"
	mkdir -p "$root/projects/foo/chart" "$root/projects/foo/deploy"
	cat >"$root/projects/foo/chart/Chart.yaml" <<-EOF
		apiVersion: v2
		name: foo
		version: ${version}
	EOF
	cat >"$root/projects/foo/deploy/application.yaml" <<-EOF
		apiVersion: argoproj.io/v1alpha1
		kind: Application
		spec:
		  sources:
		    - repoURL: ghcr.io/jomcgi/homelab/charts
		      chart: foo
		      targetRevision: ${tr}
		    - repoURL: https://github.com/jomcgi/homelab
		      targetRevision: HEAD
		      ref: values
	EOF
}

run_bump() {
	# $1: root, remaining: script args. Echoes exit code; output in $TMP/out.
	local root="$1"
	shift
	set +e
	BUMP_CHART_REPO_ROOT="$root" \
		BUMP_CHART_SKIP_REGISTRY_CHECK=1 \
		BUMP_CHART_SKIP_PR_CHECK=1 \
		BUMP_CHART_MAIN_VERSION="${MAIN_VERSION_OVERRIDE:?}" \
		bash "$SCRIPT" projects/foo "$@" >"$TMP/out" 2>&1
	echo $?
	set -e
}

assert_eq() {
	# $1: label, $2: expected, $3: actual
	if [[ "$2" != "$3" ]]; then
		echo "FAIL: $1: expected '$2', got '$3'" >&2
		FAILURES=$((FAILURES + 1))
	else
		echo "PASS: $1"
	fi
}

chart_version() { grep '^version:' "$1/projects/foo/chart/Chart.yaml" | awk '{print $2}'; }
semver_tr() { grep -E 'targetRevision: [0-9]' "$1/projects/foo/deploy/application.yaml" | awk '{print $2}'; }
head_tr_count() { grep -c 'targetRevision: HEAD' "$1/projects/foo/deploy/application.yaml"; }

# --- Test 1: fresh patch bump, local == main -------------------------------
ROOT="$TMP/t1"
setup_project "$ROOT" 0.1.0 0.1.0
MAIN_VERSION_OVERRIDE=0.1.0
rc=$(run_bump "$ROOT")
assert_eq "t1 exit code" 0 "$rc"
assert_eq "t1 chart version" 0.1.1 "$(chart_version "$ROOT")"
assert_eq "t1 targetRevision" 0.1.1 "$(semver_tr "$ROOT")"
assert_eq "t1 HEAD ref untouched" 1 "$(head_tr_count "$ROOT")"

# --- Test 2: no-op when the branch already carries an unpublished bump -----
ROOT="$TMP/t2"
setup_project "$ROOT" 0.1.1 0.1.1
MAIN_VERSION_OVERRIDE=0.1.0
rc=$(run_bump "$ROOT")
assert_eq "t2 exit code" 0 "$rc"
assert_eq "t2 chart version unchanged" 0.1.1 "$(chart_version "$ROOT")"
grep -q "Nothing to do" "$TMP/out" || {
	echo "FAIL: t2 expected 'Nothing to do' message" >&2
	FAILURES=$((FAILURES + 1))
}

# --- Test 3: collision recovery, main advanced past local ------------------
ROOT="$TMP/t3"
setup_project "$ROOT" 0.1.1 0.1.1
MAIN_VERSION_OVERRIDE=0.2.0
rc=$(run_bump "$ROOT")
assert_eq "t3 exit code" 0 "$rc"
assert_eq "t3 chart version" 0.2.1 "$(chart_version "$ROOT")"
assert_eq "t3 targetRevision" 0.2.1 "$(semver_tr "$ROOT")"

# --- Test 4: explicit version wins, must be greater than base --------------
ROOT="$TMP/t4"
setup_project "$ROOT" 0.1.0 0.1.0
MAIN_VERSION_OVERRIDE=0.1.0
rc=$(run_bump "$ROOT" --version 1.0.0)
assert_eq "t4 exit code" 0 "$rc"
assert_eq "t4 chart version" 1.0.0 "$(chart_version "$ROOT")"
rc=$(run_bump "$ROOT" --version 0.0.1)
assert_eq "t4 too-small version rejected" 1 "$rc"

# --- Test 5: minor bump ------------------------------------------------------
ROOT="$TMP/t5"
setup_project "$ROOT" 0.1.5 0.1.5
MAIN_VERSION_OVERRIDE=0.1.5
rc=$(run_bump "$ROOT" --minor)
assert_eq "t5 exit code" 0 "$rc"
assert_eq "t5 chart version" 0.2.0 "$(chart_version "$ROOT")"

# --- Test 6: mismatched targetRevision is healed to the new version --------
ROOT="$TMP/t6"
setup_project "$ROOT" 0.3.0 0.2.9
MAIN_VERSION_OVERRIDE=0.3.0
rc=$(run_bump "$ROOT")
assert_eq "t6 exit code" 0 "$rc"
assert_eq "t6 chart version" 0.3.1 "$(chart_version "$ROOT")"
assert_eq "t6 targetRevision healed" 0.3.1 "$(semver_tr "$ROOT")"

# --- Test 7: skip a version an open PR already claimed ----------------------
# Natural next patch is 0.1.1, but two open PRs already claim 0.1.1 and 0.1.2
# (injected). The bump must skip both and land on 0.1.3, so two concurrent PRs
# never pick the same number (the collision git's rebase would silently merge).
ROOT="$TMP/t7"
setup_project "$ROOT" 0.1.0 0.1.0
set +e
BUMP_CHART_REPO_ROOT="$ROOT" \
	BUMP_CHART_SKIP_REGISTRY_CHECK=1 \
	BUMP_CHART_MAIN_VERSION=0.1.0 \
	BUMP_CHART_CLAIMED_VERSIONS=$'0.1.1\n0.1.2' \
	bash "$SCRIPT" projects/foo >"$TMP/out" 2>&1
rc=$?
set -e
assert_eq "t7 exit code" 0 "$rc"
assert_eq "t7 skips claimed versions" 0.1.3 "$(chart_version "$ROOT")"
assert_eq "t7 targetRevision skips too" 0.1.3 "$(semver_tr "$ROOT")"
grep -q "already claimed by an open PR" "$TMP/out" || {
	echo "FAIL: t7 expected 'already claimed by an open PR' message" >&2
	FAILURES=$((FAILURES + 1))
}

# --- Test 8: a claimed version equal to the base is irrelevant --------------
# An open PR sitting at an OLDER/equal version (0.1.0, e.g. an un-bumped branch)
# must not push our bump around: only claims on the candidate matter.
ROOT="$TMP/t8"
setup_project "$ROOT" 0.1.0 0.1.0
set +e
BUMP_CHART_REPO_ROOT="$ROOT" \
	BUMP_CHART_SKIP_REGISTRY_CHECK=1 \
	BUMP_CHART_MAIN_VERSION=0.1.0 \
	BUMP_CHART_CLAIMED_VERSIONS="0.1.0" \
	bash "$SCRIPT" projects/foo >"$TMP/out" 2>&1
rc=$?
set -e
assert_eq "t8 exit code" 0 "$rc"
assert_eq "t8 ignores equal/older claim" 0.1.1 "$(chart_version "$ROOT")"

if [[ "$FAILURES" -gt 0 ]]; then
	echo "${FAILURES} test(s) failed" >&2
	exit 1
fi
echo "All tests passed"
