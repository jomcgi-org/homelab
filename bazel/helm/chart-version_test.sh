#!/usr/bin/env bash
# Unit tests for chart-version.sh.
#
# The script computes the next chart semver from the conventional commits since
# the commit that last set the current version, scoped to a dependency closure.
# These tests drive it against real throwaway git repositories: the behaviour
# under test IS the git history walk, so stubbing git would test nothing.
#
# Arithmetic cases pass no package label and use the documented chart-only
# mode. Input-selection cases mock only the Bazel query boundary and still use
# real files, commits, deletions, renames and Git history walks.
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
	GIT_AUTHOR_NAME="$author" GIT_COMMITTER_NAME="$author" \
		git -C "$repo" commit --quiet -m "$subject"
}

run_version() {
	local repo="$1"
	(cd "$repo" && bash "$SCRIPT" chart 2>/dev/null)
}

# Query-boundary fixture. The mock reads its response from the throwaway Git
# repository, records the expression for assertions, and can emit partial
# stdout before failing. Everything after the query is the production Git walk.
MOCK_BIN="$TMP/mock-bin"
mkdir -p "$MOCK_BIN"
cat >"$MOCK_BIN/bazel" <<'MOCK_BAZEL'
#!/usr/bin/env bash
printf '%s\n' "$*" >.bazel-query-args
[[ -f .bazel-query-output ]] && cat .bazel-query-output
if [[ -f .bazel-query-status ]]; then
	exit "$(cat .bazel-query-status)"
fi
exit 0
MOCK_BAZEL
chmod +x "$MOCK_BIN/bazel"

write_query_output() {
	local repo="$1"
	cat >"$repo/.bazel-query-output" <<'QUERY_OUTPUT'
//projects/embervm/chart:BUILD
//projects/embervm/chart:Chart.yaml
//projects/embervm/chart:values.yaml
//projects/embervm/chart:templates/deployment.yaml
//projects/embervm/chart:templates/tokenbroker-deployment.yaml
//projects/embervm/chart:templates/notes/README.md
//projects/embervm/image:BUILD
//projects/embervm/image:app.py
//projects/embervm/image:embedded_test.py
//projects/embervm/proto/embervm/node/v1:node.proto
//projects/embervm/deploy:BUILD
//projects/embervm/deploy:values.yaml
//shared/runtime:BUILD
//shared/runtime:config.txt
//bazel/rules:image.bzl
@external_repo//:immutable_input
QUERY_OUTPUT
}

new_input_repo() {
	local name="$1"
	local repo="$TMP/$name"
	mkdir -p \
		"$repo/projects/embervm/chart/templates/notes" \
		"$repo/projects/embervm/image" \
		"$repo/projects/embervm/proto/embervm/node/v1" \
		"$repo/projects/embervm/deploy" \
		"$repo/projects/embervm/docs" \
		"$repo/projects/embervm/specs" \
		"$repo/shared/runtime" \
		"$repo/bazel/rules"
	git -C "$repo" init --quiet
	git -C "$repo" config user.email "test@example.com"
	git -C "$repo" config user.name "test"
	git -C "$repo" config commit.gpgsign false
	printf 'name: embervm\nversion: 0.1.0\n' >"$repo/projects/embervm/chart/Chart.yaml"
	printf 'replicas: 1\n' >"$repo/projects/embervm/chart/values.yaml"
	printf 'kind: Deployment\n' >"$repo/projects/embervm/chart/templates/deployment.yaml"
	printf 'kind: Deployment\n' >"$repo/projects/embervm/chart/templates/tokenbroker-deployment.yaml"
	printf 'packaged operator notes\n' >"$repo/projects/embervm/chart/templates/notes/README.md"
	printf 'print("image")\n' >"$repo/projects/embervm/image/app.py"
	printf 'print("packaged test fixture")\n' >"$repo/projects/embervm/image/embedded_test.py"
	printf 'removed input\n' >"$repo/projects/embervm/image/removed.py"
	printf 'renamed input\n' >"$repo/projects/embervm/image/renamed.py"
	printf 'syntax = "proto3";\n' >"$repo/projects/embervm/proto/embervm/node/v1/node.proto"
	printf 'shared = true\n' >"$repo/shared/runtime/config.txt"
	printf 'def image_rule():\n    pass\n' >"$repo/bazel/rules/image.bzl"
	printf 'load("//bazel/rules:image.bzl", "image_rule")\n' >"$repo/projects/embervm/image/BUILD"
	printf 'exports_files(["config.txt"])\n' >"$repo/shared/runtime/BUILD"
	printf 'exports_files(["Chart.yaml"])\n' >"$repo/projects/embervm/chart/BUILD"
	cat >"$repo/projects/embervm/deploy/BUILD" <<'DEPLOY_BUILD'
genrule(
    name = "render_manifests",
    srcs = ["values.yaml"],
    outs = ["manifests/all.yaml"],
    cmd = "cp $< $@",
)
DEPLOY_BUILD
	printf 'replicas: 2\n' >"$repo/projects/embervm/deploy/values.yaml"
	printf 'kind: Application\n' >"$repo/projects/embervm/deploy/application.yaml"
	printf 'resources: []\n' >"$repo/projects/embervm/deploy/kustomization.yaml"
	printf 'architecture notes\n' >"$repo/projects/embervm/ARCHITECTURE.md"
	printf 'user documentation\n' >"$repo/projects/embervm/docs/README.md"
	printf 'test only\n' >"$repo/projects/embervm/image/app_test.py"
	printf 'STPA fragment\n' >"$repo/projects/embervm/specs/control-loop.md"
	printf 'build --stamp=no\n' >"$repo/.bazelrc"
	git -C "$repo" add -A
	git -C "$repo" commit --quiet -m "chore: set version 0.1.0"
	write_query_output "$repo"
	echo "$repo"
}

commit_path() {
	local repo="$1" path="$2" subject="$3"
	mkdir -p "$(dirname "$repo/$path")"
	printf 'change %s\n' "$RANDOM" >>"$repo/$path"
	git -C "$repo" add "$path"
	git -C "$repo" commit --quiet -m "$subject"
}

run_input_version() {
	local repo="$1"
	(
		cd "$repo" &&
			PATH="$MOCK_BIN:$PATH" bash "$SCRIPT" \
				projects/embervm/chart //projects/embervm/chart:chart.package 2>/dev/null
	)
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

# 12. The query requests concrete sources and build definitions, including the
# explicit deploy render target. This protects the contract independently of
# the individual path-behaviour cases below.
repo=$(new_input_repo querycontract)
expect "query-backed baseline" "0.1.0" "$(run_input_version "$repo")" \
	"no release input changed"
query_args=$(cat "$repo/.bazel-query-args")
for query_term in 'kind("source file"' 'buildfiles(' \
	'deps(//projects/embervm/chart:chart.package)' \
	'deps(//projects/embervm/deploy:render_manifests)'; do
	if grep -Fq "$query_term" <<<"$query_args"; then
		echo "ok: query contains ${query_term}"
	else
		echo "FAIL: query omitted ${query_term}: ${query_args}" >&2
		FAILURES=$((FAILURES + 1))
	fi
done

# 13. Historical replay fixture for 091d574fcd5346b3959a5033cd2b5be0320067e5.
# That commit changed only projects/embervm/ARCHITECTURE.md. The fixture keeps
# the real subject and path but does not need repository history or network.
repo=$(new_input_repo replay091)
commit_path "$repo" projects/embervm/ARCHITECTURE.md \
	"docs(embervm): correct CPU pivot status"
expect "091d574f-like architecture edit" "0.1.0" "$(run_input_version "$repo")" \
	"non-input documentation is outside the source closure"

# Unrelated tests, STPA and docs in a dependency package remain non-inputs.
commit_path "$repo" projects/embervm/image/app_test.py "test(embervm): add image case"
commit_path "$repo" projects/embervm/specs/control-loop.md "docs(embervm): clarify STPA"
commit_path "$repo" projects/embervm/docs/README.md "docs(embervm): clarify usage"
expect "unrelated project support files" "0.1.0" "$(run_input_version "$repo")" \
	"test, STPA and docs paths are not selected"

# 14. Historical replay fixture for ff558852efcf72f410310ec4d135dc33c6e697e6.
# The real commit changed both tokenbroker-deployment.yaml and node.proto, so it
# is a release input despite its docs subject. This is intentionally a bump.
repo=$(new_input_repo replayff)
printf '# chart change\n' >>"$repo/projects/embervm/chart/templates/tokenbroker-deployment.yaml"
printf '// proto change\n' >>"$repo/projects/embervm/proto/embervm/node/v1/node.proto"
git -C "$repo" add \
	projects/embervm/chart/templates/tokenbroker-deployment.yaml \
	projects/embervm/proto/embervm/node/v1/node.proto
git -C "$repo" commit --quiet -m "docs(embervm): mark unused node RPCs reserved"
expect "ff558852-like chart and proto edits" "0.1.1" "$(run_input_version "$repo")" \
	"one docs-subject commit changes two release inputs"

# 15. Subjects do not override actual chart, deploy or pinned-image inputs.
repo=$(new_input_repo templateinput)
commit_path "$repo" projects/embervm/chart/templates/deployment.yaml \
	"docs(embervm): explain deployment"
expect "independent template edit" "0.1.1" "$(run_input_version "$repo")" \
	"chart template is packaged"

repo=$(new_input_repo deployinput)
commit_path "$repo" projects/embervm/deploy/values.yaml \
	"docs(embervm): explain deploy value"
expect "deploy values edit" "0.1.1" "$(run_input_version "$repo")" \
	"render target selects active deploy values"

repo=$(new_input_repo imageinput)
commit_path "$repo" projects/embervm/image/app.py \
	"docs(embervm): annotate image source"
expect "pinned image source edit" "0.1.1" "$(run_input_version "$repo")" \
	"image source is in chart.package closure"

repo=$(new_input_repo sharedinput)
commit_path "$repo" shared/runtime/config.txt \
	"docs(shared): clarify runtime config"
expect "shared source dependency edit" "0.1.1" "$(run_input_version "$repo")" \
	"closure is not restricted to the chart project"

repo=$(new_input_repo buildinput)
commit_path "$repo" bazel/rules/image.bzl \
	"docs(build): clarify image rule"
expect "build definition edit" "0.1.1" "$(run_input_version "$repo")" \
	"buildfiles closure is a release input"

repo=$(new_input_repo buildconfig)
commit_path "$repo" .bazelrc "docs(build): clarify shared build setting"
expect "build configuration edit" "0.1.1" "$(run_input_version "$repo")" \
	"repository build configuration affects selected targets"

# A documentation-looking file can still be real packaged content. Selection
# comes from the build graph, never from its extension or basename.
repo=$(new_input_repo packageddoc)
commit_path "$repo" projects/embervm/chart/templates/notes/README.md \
	"docs(chart): update packaged notes"
expect "packaged documentation edit" "0.1.1" "$(run_input_version "$repo")" \
	"doc-looking chart input remains selected"

repo=$(new_input_repo packagedtest)
commit_path "$repo" projects/embervm/image/embedded_test.py \
	"test(image): update embedded fixture"
expect "packaged test-looking input edit" "0.1.1" "$(run_input_version "$repo")" \
	"graph selection wins over a test-looking basename"

# 16. Files removed from the current closure cannot be named by the query. The
# package-directory D/R guard conservatively preserves those history entries.
repo=$(new_input_repo deletedinput)
git -C "$repo" rm --quiet projects/embervm/image/removed.py
git -C "$repo" commit --quiet -m "fix(embervm): remove image input"
expect "deleted former input" "0.1.1" "$(run_input_version "$repo")" \
	"deletion is retained even though the current query cannot name it"

repo=$(new_input_repo renamedinput)
mkdir -p "$repo/projects/embervm/archive"
git -C "$repo" mv projects/embervm/image/renamed.py projects/embervm/archive/renamed.py
git -C "$repo" commit --quiet -m "fix(embervm): rename image input out of target"
expect "renamed former input" "0.1.1" "$(run_input_version "$repo")" \
	"rename is retained after leaving the current closure"

# 17. Partial stdout is not a usable closure. A query failure switches to the
# conservative repo-wide walk, so even a path missing from partial output bumps.
repo=$(new_input_repo queryfailure)
commit_path "$repo" projects/embervm/docs/README.md "fix(embervm): query failure fixture"
printf '//projects/embervm/chart:Chart.yaml\n' >"$repo/.bazel-query-output"
printf '7\n' >"$repo/.bazel-query-status"
expect "partial query failure" "0.1.1" "$(run_input_version "$repo")" \
	"failed query counts repo-wide instead of trusting partial stdout"

repo=$(new_input_repo unsupportedquery)
commit_path "$repo" projects/embervm/docs/README.md "fix(embervm): unsupported query fixture"
printf 'not-a-bazel-label\n' >"$repo/.bazel-query-output"
expect "unsupported query output" "0.1.1" "$(run_input_version "$repo")" \
	"unsupported closure counts repo-wide"

# 18. A fallback release can cross a semantic boundary outside the closure.
# A subsequent scoped release must use the same allocation history, even if
# the earlier publisher has not written its version back to Chart.yaml yet.
for boundary in feature breaking; do
	base=0.536.39
	expected_first=0.537.0
	expected_second=0.537.1
	subject="feat(other): new capability"
	if [[ "$boundary" == breaking ]]; then
		base=1.8.4
		expected_first=2.0.0
		expected_second=2.0.1
		subject="feat(other)!: incompatible capability"
	fi
	repo=$(new_repo "fallback-${boundary}" "$base")
	commit_path "$repo" other/source.py "$subject"
	expect "unrelated ${boundary} stays quiet" "$base" "$(run_version "$repo")" \
		"global allocation does not force an unrelated release"
	first=$(cd "$repo" && CHART_VERSION_ALL_PATHS=1 bash "$SCRIPT" chart 2>/dev/null)
	expect "fallback ${boundary} boundary" "$expected_first" "$first" \
		"digest authority sees the repository-wide boundary"
	commit_in "$repo" "fix: subsequent chart change"
	second=$(run_version "$repo")
	expect "scoped fix retains ${boundary} boundary" "$expected_second" "$second" \
		"later source publishes above the earlier fallback"
	expect "normal and fallback agree for ${boundary}" "$second" \
		"$(cd "$repo" && CHART_VERSION_ALL_PATHS=1 bash "$SCRIPT" chart 2>/dev/null)" \
		"one allocation function regardless of release trigger"
done

# Repository-wide patch serials must not collide after fallback either.
repo=$(new_repo fallback-patches 0.1.0)
commit_path "$repo" other/source.py "fix(other): one"
commit_path "$repo" other/source.py "fix(other): two"
expect "fallback patch allocation" "0.1.2" \
	"$(cd "$repo" && CHART_VERSION_ALL_PATHS=1 bash "$SCRIPT" chart 2>/dev/null)" \
	"two changes outside the closure"
commit_in "$repo" "fix: chart follows"
expect "scoped patch follows fallback" "0.1.3" "$(run_version "$repo")" \
	"all commits reserve their position"

if [[ "$FAILURES" -gt 0 ]]; then
	echo "${FAILURES} test(s) failed"
	exit 1
fi
echo "All chart-version tests passed"
