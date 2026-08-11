#!/usr/bin/env bash
# Unit tests for chart-version.sh.
#
# The script computes the next chart semver from the conventional commits since
# the commit that last set the current version, scoped to a dependency closure.
# These tests drive it against real throwaway git repositories: the behaviour
# under test IS the git history walk, so stubbing git would test nothing.
#
# The bazel dependency query is skipped by passing no package label, which puts
# the script in its documented chart-dir-only fallback. That keeps the test
# hermetic (no bazel inside the test sandbox) and is orthogonal to the version
# arithmetic being asserted here.
set -o errexit -o nounset -o pipefail

SCRIPT_REL="bazel/helm/chart-version.sh"
SCRIPT=""
for candidate in \
	"${RUNFILES_DIR:-}/_main/${SCRIPT_REL}" \
	"${TEST_SRCDIR:-}/_main/${SCRIPT_REL}" \
	"${BASH_SOURCE[0]%/*}/chart-version.sh"; do
	if [[ -f "$candidate" ]]; then
		SCRIPT="$candidate"
		break
	fi
done
if [[ -z "$SCRIPT" ]]; then
	echo "ERROR: cannot locate chart-version.sh in runfiles" >&2
	exit 1
fi
# Absolute, because every case runs the script from inside a throwaway repo: a
# relative path resolves against that repo and the script is simply not there.
SCRIPT="$(cd "$(dirname "$SCRIPT")" && pwd)/$(basename "$SCRIPT")"

TMP="${TEST_TMPDIR:-$(mktemp -d)}"
FAILURES=0

expect() {
	local what="$1" want="$2" got="$3" why="$4"
	if [[ "$want" == "$got" ]]; then
		echo "ok: ${what} (${why})"
	else
		echo "FAIL: ${what}: want '${want}', got '${got}' (${why})" >&2
		FAILURES=$((FAILURES + 1))
	fi
}

# Create a fresh repo whose chart is at the given version, and return its path.
# The Chart.yaml commit becomes the VERSION_COMMIT the script anchors to.
#
# The caller passes an explicit name because this runs in a command
# substitution: a counter incremented here lives in a subshell and every case
# would silently reuse the same directory.
new_repo() {
	local name="$1" version="$2"
	local repo="$TMP/$name"
	mkdir -p "$repo/chart"
	git -C "$repo" init --quiet
	git -C "$repo" config user.email "test@example.com"
	git -C "$repo" config user.name "test"
	git -C "$repo" config commit.gpgsign false
	printf 'name: demo\nversion: %s\n' "$version" >"$repo/chart/Chart.yaml"
	git -C "$repo" add chart/Chart.yaml
	git -C "$repo" commit --quiet -m "chore: set version ${version}"
	echo "$repo"
}

# Add one commit touching the chart dir, with the given subject and author.
commit_in() {
	local repo="$1" subject="$2" author="${3:-test}"
	echo "$RANDOM" >>"$repo/chart/values.yaml"
	git -C "$repo" add chart/values.yaml
	git -C "$repo" -c "user.name=${author}" commit --quiet -m "$subject"
}

run_version() {
	local repo="$1"
	(cd "$repo" && bash "$SCRIPT" chart 2>/dev/null)
}

# 1. No commits since the version was set: unchanged.
repo=$(new_repo unchanged 0.1.0)
expect "no commits returns current" "0.1.0" "$(run_version "$repo")" "nothing to bump"

# 2. Patch commits accumulate into the serial rather than a single +1. Three
# fixes since 0.1.0 is 0.1.3, NOT 0.1.1. This is the core of ADR platform/009
# decision 1: the version is a function of the commit, not of a read-then-add.
repo=$(new_repo patchcount 0.1.0)
commit_in "$repo" "fix: one"
commit_in "$repo" "fix: two"
commit_in "$repo" "chore: three"
expect "patch counts commits" "0.1.3" "$(run_version "$repo")" "3 qualifying commits"

# 3. THE CONCURRENCY PROPERTY. Two publishes racing on main read the same
# Chart.yaml but build different commits. Under the old +1 scheme both computed
# the same version, so the loser's images shipped under no version at all. Each
# additional commit must yield a strictly different version.
repo=$(new_repo concurrent 0.1.0)
commit_in "$repo" "fix: first merge"
v_first=$(run_version "$repo")
commit_in "$repo" "fix: second merge"
v_second=$(run_version "$repo")
expect "earlier commit version" "0.1.1" "$v_first" "one commit in range"
expect "later commit version" "0.1.2" "$v_second" "two commits in range"
if [[ "$v_first" != "$v_second" ]]; then
	echo "ok: concurrent publishes cannot collide (${v_first} != ${v_second})"
else
	echo "FAIL: concurrent publishes collide: both computed ${v_first}" >&2
	FAILURES=$((FAILURES + 1))
fi

# 4. A feat bumps the minor and the serial counts commits AFTER the feat, so a
# feat landing last is x.y.0 and further commits keep incrementing distinctly.
repo=$(new_repo featalone 0.1.0)
commit_in "$repo" "feat: new thing"
expect "feat alone" "0.2.0" "$(run_version "$repo")" "boundary is the last commit"

repo=$(new_repo featthenfix 0.1.0)
commit_in "$repo" "feat: new thing"
commit_in "$repo" "fix: follow up"
commit_in "$repo" "fix: another"
expect "feat then fixes" "0.2.2" "$(run_version "$repo")" "2 commits after the feat"

# 5. A fix BEFORE the feat does not inflate the minor's serial: the boundary is
# the feat, not the start of the range.
repo=$(new_repo fixbeforefeat 0.1.0)
commit_in "$repo" "fix: before"
commit_in "$repo" "feat: the feature"
expect "fix before feat" "0.2.0" "$(run_version "$repo")" "boundary is the feat"

# 6. Pre-1.0 breaking changes bump the minor, not the major (semver 0.x).
repo=$(new_repo prebreaking 0.1.0)
commit_in "$repo" "feat!: breaking"
expect "pre-1.0 breaking is minor" "0.2.0" "$(run_version "$repo")" "major is 0"

# 7. Post-1.0 breaking changes bump the major and reset the minor.
repo=$(new_repo postbreaking 1.4.2)
commit_in "$repo" "feat!: breaking"
commit_in "$repo" "fix: after"
expect "post-1.0 breaking is major" "2.0.1" "$(run_version "$repo")" "1 commit after the break"

# 8. Bot commits are excluded from the count, not merely from the bump kind.
# The write-back commit this script now feeds is authored by chart-version-bot,
# so counting it would walk the version forward on every publish forever.
repo=$(new_repo botskip 0.1.0)
commit_in "$repo" "fix: real change"
commit_in "$repo" "chore(demo): bump chart version to 0.1.1" "chart-version-bot"
expect "bot commits are not counted" "0.1.1" "$(run_version "$repo")" "1 human commit"

# 9. CHART_VERSION_ALL_PATHS counts commits that the chart-dir scoping misses.
# This is the escalation path push.sh.tpl uses when the image digests prove the
# content changed but the dependency closure reported nothing: the query that
# just failed must not be able to veto the new version.
repo=$(new_repo allpaths 0.1.0)
mkdir -p "$repo/elsewhere"
echo "change" >>"$repo/elsewhere/file.txt"
git -C "$repo" add elsewhere/file.txt
git -C "$repo" commit --quiet -m "fix: outside the chart dir"
expect "chart-dir scoping misses it" "0.1.0" \
	"$(run_version "$repo")" "commit touched no chart path"
expect "all-paths scoping finds it" "0.1.1" \
	"$(cd "$repo" && CHART_VERSION_ALL_PATHS=1 bash "$SCRIPT" chart 2>/dev/null)" \
	"counted repo-wide"

# 10. The escalation stays commit-derived, so two concurrent publishes taking
# this path still cannot compute the same version. The commit has to touch a
# real path: `git log -- <paths>` excludes empty commits even when the path is
# the repo root, so an --allow-empty commit would not be counted here.
echo "more" >>"$repo/elsewhere/file.txt"
git -C "$repo" add elsewhere/file.txt
git -C "$repo" commit --quiet -m "fix: another one"
expect "all-paths stays commit-derived" "0.1.2" \
	"$(cd "$repo" && CHART_VERSION_ALL_PATHS=1 bash "$SCRIPT" chart 2>/dev/null)" \
	"strictly greater at the later commit"

# 11. A SHALLOW clone must fail LOUDLY rather than return a plausible number.
#
# This is the failure that took main's deploy down on 2026-08-10. BuildBuddy
# clones shallow, and in a shallow repo the -S search for the current version
# matches the graft boundary commit, because at the boundary every file reads
# as newly added. At depth 1 that boundary is HEAD, so the HEAD..HEAD range is
# empty and the script reported "no bump needed" for charts whose images had
# demonstrably changed.
#
# The fixture has to BE a shallow clone: the bug is invisible in every other
# case in this file, all of which build their history locally and therefore
# always have it complete. `file://` is load-bearing, because a plain path
# clone is a local hardlink copy that ignores --depth entirely.
repo=$(new_repo shallowsrc 0.1.0)
commit_in "$repo" "fix: one"
commit_in "$repo" "fix: two"
expect "same repo cloned deep" "0.1.2" "$(run_version "$repo")" "full history counts both"

shallow="$TMP/shallowclone"
git clone --depth=1 --quiet "file://$repo" "$shallow"
expect "fixture really is shallow" "true" \
	"$(git -C "$shallow" rev-parse --is-shallow-repository)" "clone --depth=1"

set +e
shallow_out=$(cd "$shallow" && bash "$SCRIPT" chart 2>/dev/null)
shallow_rc=$?
set -e
expect "shallow clone fails" "1" "$shallow_rc" "history is truncated"
# Assert the OLD behaviour specifically. Exiting non-zero is not enough on its
# own: the regression is the script confidently emitting the unchanged version,
# which push.sh.tpl reads as "nothing to deploy".
if [[ "$shallow_out" == "0.1.0" ]]; then
	echo "FAIL: shallow clone returned the unchanged version '0.1.0' instead of failing" >&2
	FAILURES=$((FAILURES + 1))
else
	echo "ok: shallow clone emitted no version (got '${shallow_out}')"
fi

if [[ "$FAILURES" -gt 0 ]]; then
	echo "${FAILURES} test(s) failed"
	exit 1
fi
echo "All chart-version tests passed"
