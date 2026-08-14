#!/usr/bin/env bash
# Unit tests for write-back-versions.sh.
#
# The script commits the versions main just published back to main, monotonically
# and in one commit. The behaviour under test is git ref manipulation against a
# real remote, so these drive it against throwaway repositories: a bare repo
# standing in for origin, and a clone standing in for the CI workspace.
set -o errexit -o nounset -o pipefail

SCRIPT_REL="bazel/helm/write-back-versions.sh"
SCRIPT=""
for candidate in \
	"${RUNFILES_DIR:-}/_main/${SCRIPT_REL}" \
	"${TEST_SRCDIR:-}/_main/${SCRIPT_REL}" \
	"${BASH_SOURCE[0]%/*}/write-back-versions.sh"; do
	if [[ -f "$candidate" ]]; then
		SCRIPT="$candidate"
		break
	fi
done
if [[ -z "$SCRIPT" ]]; then
	echo "ERROR: cannot locate write-back-versions.sh in runfiles" >&2
	exit 1
fi
# Absolute: every case runs the script from inside a throwaway clone.
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

# Build an origin (bare) plus a clone, with one chart at the given version.
# Echoes the clone path.
new_env() {
	local name="$1" version="$2"
	local origin="$TMP/${name}.git" seed="$TMP/${name}-seed" clone="$TMP/${name}"

	# --initial-branch is load-bearing on BOTH repos. Without it the branch name
	# comes from the ambient init.defaultBranch, which is `main` on a configured
	# dev machine and `master` in the CI sandbox. The bare repo's HEAD then
	# points at a ref that never gets created, the clone comes up with no
	# working tree at all ("remote HEAD refers to nonexistent ref"), and the
	# later cases push commits built on nothing.
	git init --quiet --bare --initial-branch=main "$origin"
	mkdir -p "$seed/projects/demo/chart" "$seed/projects/demo/deploy"
	git -C "$seed" init --quiet --initial-branch=main
	git -C "$seed" config user.email "t@e.com"
	git -C "$seed" config user.name "t"
	printf 'name: demo\nversion: %s\n' "$version" >"$seed/projects/demo/chart/Chart.yaml"
	# Multi-source, as every real deploy/application.yaml in this repo is: the
	# chart source pinned to a semver, and a $values source pinned to a GIT REF.
	# The git ref must survive the write-back untouched.
	cat >"$seed/projects/demo/deploy/application.yaml" <<-YAML
		spec:
		  sources:
		    - repoURL: oci://ghcr.io/jomcgi/homelab/charts
		      chart: demo
		      targetRevision: ${version}
		    - repoURL: https://github.com/jomcgi/homelab
		      targetRevision: HEAD
		      ref: values
	YAML
	git -C "$seed" add -A
	git -C "$seed" commit --quiet -m "chore: seed ${version}"
	git -C "$seed" push --quiet "$origin" main

	git clone --quiet "$origin" "$clone"
	git -C "$clone" config user.email "t@e.com"
	git -C "$clone" config user.name "t"
	echo "$clone"
}

record() {
	local dir="$1" chart="$2" chart_dir="$3" version="$4"
	mkdir -p "$dir"
	printf '%s %s\n' "$chart_dir" "$version" >"${dir}/${chart}"
}

# Read the version out of a file as it exists on origin/main. Uses the LAST
# field, because targetRevision is a list item ("- targetRevision: 0.1.3") and
# $2 there is the key, not the value.
version_on_main() {
	local clone="$1" path="$2"
	git -C "$clone" show "origin/main:${path}" 2>/dev/null |
		grep -E '^version:|targetRevision:' | head -1 | awk '{print $NF}'
}

# 1. No record directory at all: succeeds and does nothing. This is the "no
# chart changed in this merge" path, which is the common case.
clone=$(new_env norecords 0.1.0)
(cd "$clone" && bash "$SCRIPT" "$TMP/does-not-exist" >/dev/null 2>&1)
expect "missing record dir is a no-op" "0" "$?" "exit status"

# 2. A higher published version is written to BOTH files in one commit.
clone=$(new_env happy 0.1.0)
record "$TMP/rec-happy" demo projects/demo/chart 0.1.3
(cd "$clone" && bash "$SCRIPT" "$TMP/rec-happy" >/dev/null 2>&1)
git -C "$clone" fetch --quiet origin main
expect "Chart.yaml written" "0.1.3" \
	"$(version_on_main "$clone" projects/demo/chart/Chart.yaml)" "published version"
expect "targetRevision written" "0.1.3" \
	"$(version_on_main "$clone" projects/demo/deploy/application.yaml)" "kept in step"

# The $values source's git ref must be untouched. A loose targetRevision match
# would rewrite it to a chart version and break the values source, taking the
# Application down with it.
expect "git ref survives" "1" \
	"$(git -C "$clone" show origin/main:projects/demo/deploy/application.yaml |
		grep -c 'targetRevision: HEAD')" "\$values ref not rewritten"

# 3. MONOTONIC. A slow publish for an older commit must not walk main backwards.
# Without this, targetRevision regresses and ArgoCD silently rolls production
# back to the older chart.
clone=$(new_env monotonic 0.2.0)
record "$TMP/rec-mono" demo projects/demo/chart 0.1.5
(cd "$clone" && bash "$SCRIPT" "$TMP/rec-mono" >/dev/null 2>&1)
git -C "$clone" fetch --quiet origin main
expect "refuses to lower" "0.2.0" \
	"$(version_on_main "$clone" projects/demo/chart/Chart.yaml)" "0.1.5 is older"

# 4. Republishing the version main already carries makes no commit at all, so
# the write-back cannot loop against itself.
clone=$(new_env noop 0.4.0)
before=$(git -C "$clone" rev-parse origin/main)
record "$TMP/rec-noop" demo projects/demo/chart 0.4.0
(cd "$clone" && bash "$SCRIPT" "$TMP/rec-noop" >/dev/null 2>&1)
git -C "$clone" fetch --quiet origin main
expect "equal version is a no-op" "$before" \
	"$(git -C "$clone" rev-parse origin/main)" "main unmoved"

# 5. Several charts land as ONE commit, not one per chart. This is the whole
# reason the write-back is batched outside the `jobs = 0` multirun.
clone=$(new_env batch 0.1.0)
mkdir -p "$clone/projects/other/chart" "$clone/projects/other/deploy"
printf 'name: other\nversion: 0.1.0\n' >"$clone/projects/other/chart/Chart.yaml"
cat >"$clone/projects/other/deploy/application.yaml" <<-YAML
	spec:
	  sources:
	    - repoURL: oci://ghcr.io/jomcgi/homelab/charts
	      targetRevision: 0.1.0
	    - repoURL: https://github.com/jomcgi/homelab
	      targetRevision: HEAD
YAML
git -C "$clone" add -A
git -C "$clone" commit --quiet -m "chore: add second chart"
git -C "$clone" push --quiet origin HEAD:main
base=$(git -C "$clone" rev-parse origin/main)
record "$TMP/rec-batch" demo projects/demo/chart 0.1.1
record "$TMP/rec-batch" other projects/other/chart 0.1.2
(cd "$clone" && bash "$SCRIPT" "$TMP/rec-batch" >/dev/null 2>&1)
git -C "$clone" fetch --quiet origin main
expect "two charts, one commit" "1" \
	"$(git -C "$clone" rev-list --count "${base}..origin/main")" "single write-back commit"
expect "first chart written" "0.1.1" \
	"$(version_on_main "$clone" projects/demo/chart/Chart.yaml)" "batched"
expect "second chart written" "0.1.2" \
	"$(version_on_main "$clone" projects/other/chart/Chart.yaml)" "batched"

# 6. The write-back commit is authored by the bot push.sh.tpl's loop guard looks
# for. If this drifts, main republishes forever.
expect "authored by the bot" "chart-version-bot" \
	"$(git -C "$clone" log -1 --format='%an' origin/main)" "loop guard depends on it"

# 7. A REPOSITORY RULE REFUSAL FAILS FAST instead of being retried as a race.
#
# On 2026-08-10 main's ruleset refused the write-back because the pushing
# identity could not bypass the required `pr-checks`. The loop reported it as
# "another publish landed first" and spent all five attempts on a condition that
# is identical every time. A pre-receive hook standing in for the ruleset lets
# this assert the two things that were wrong: the exit is immediate, and the
# reported cause is the real one.
clone=$(new_env ruleviolation 0.1.0)
origin="$TMP/ruleviolation.git"
mkdir -p "$origin/hooks"
cat >"$origin/hooks/pre-receive" <<'HOOK'
#!/usr/bin/env bash
echo "$(($(cat "$GIT_DIR/push-count" 2>/dev/null || echo 0) + 1))" >"$GIT_DIR/push-count"
echo "error: GH013: Repository rule violations found for refs/heads/main." >&2
echo "- Required status check \"pr-checks\" is expected." >&2
exit 1
HOOK
chmod +x "$origin/hooks/pre-receive"

record "$TMP/rec-rule" demo projects/demo/chart 0.1.4
set +e
rule_out=$(cd "$clone" && WRITE_BACK_TRIES=5 bash "$SCRIPT" "$TMP/rec-rule" 2>&1)
rule_rc=$?
set -e

expect "rule violation exits non-zero" "1" "$rule_rc" "retrying cannot fix it"
expect "pushed exactly once" "1" \
	"$(cat "$origin/push-count" 2>/dev/null || echo 0)" "no wasted retries"
if grep -q "another publish landed first" <<<"$rule_out"; then
	echo "FAIL: reported a rule violation as a lost race" >&2
	FAILURES=$((FAILURES + 1))
else
	echo "ok: reports the real cause (not a lost race)"
fi
if grep -q "repository rule" <<<"$rule_out"; then
	echo "ok: names the repository rule in the error"
else
	echo "FAIL: error does not name the repository rule" >&2
	FAILURES=$((FAILURES + 1))
fi

# Add a dev-environment Application beside the production one, pinned BEHIND it,
# with or without the opt-in marker. Committed through the clone so origin/main
# carries it before the script runs.
add_dev_app() {
	local clone="$1" version="$2" marker="$3" path="$clone/projects/demo/dev/deploy"
	mkdir -p "$path"
	{
		echo "spec:"
		echo "  sources:"
		echo "    - repoURL: oci://ghcr.io/jomcgi/homelab/charts"
		echo "      chart: demo"
		if [[ "$marker" == "marked" ]]; then
			echo "      # chart-version-bot: manage-target-revision"
		fi
		echo "      targetRevision: ${version}"
		echo "    - repoURL: https://github.com/jomcgi/homelab"
		echo "      targetRevision: HEAD"
		echo "      ref: values"
	} >"$path/application.yaml"
	git -C "$clone" add -A
	git -C "$clone" commit --quiet -m "chore: seed dev application"
	git -C "$clone" push --quiet origin main
}

# 7. An opted-in dev Application advances with the chart, in one jump. Without
# this the dev pin freezes at whatever bootstrapped it and drifts further from
# production on every publish, which is what #4832 recorded.
clone=$(new_env devopt 0.1.0)
add_dev_app "$clone" 0.0.9 marked
record "$TMP/rec-devopt" demo projects/demo/chart 0.2.0
(cd "$clone" && bash "$SCRIPT" "$TMP/rec-devopt" >/dev/null 2>&1)
git -C "$clone" fetch --quiet origin main
expect "opted-in dev pin advances" "0.2.0" \
	"$(version_on_main "$clone" projects/demo/dev/deploy/application.yaml)" "marker present"
expect "dev \$values ref survives" "1" \
	"$(git -C "$clone" show origin/main:projects/demo/dev/deploy/application.yaml |
		grep -c 'targetRevision: HEAD')" "same anchored pattern as production"

# 8. THE ONE THAT MATTERS. A dev Application with no marker is not ours to
# write. This is monolith-dev: Kargo owns its targetRevision at runtime (#4744),
# and the committed value is a deliberately frozen bootstrap floor kept as the
# revert lever. A path-based rule matching */dev/deploy/ would rewrite it on
# every publish, fight the promotion pipeline, and destroy the lever silently.
clone=$(new_env devunmarked 0.1.0)
add_dev_app "$clone" 0.0.9 plain
record "$TMP/rec-devplain" demo projects/demo/chart 0.2.0
(cd "$clone" && bash "$SCRIPT" "$TMP/rec-devplain" >/dev/null 2>&1)
git -C "$clone" fetch --quiet origin main
expect "unmarked dev pin untouched" "0.0.9" \
	"$(version_on_main "$clone" projects/demo/dev/deploy/application.yaml)" "no marker, not ours to write"
expect "production advances regardless" "0.2.0" \
	"$(version_on_main "$clone" projects/demo/deploy/application.yaml)" "dev opt-in does not gate production"

if [[ "$FAILURES" -gt 0 ]]; then
	echo "${FAILURES} test(s) failed"
	exit 1
fi
echo "All write-back-versions tests passed"
