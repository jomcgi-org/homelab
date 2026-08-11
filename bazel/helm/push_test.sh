#!/usr/bin/env bash
# Unit tests for push.sh.tpl's "already published" branch.
#
# push.sh.tpl decides, per chart, whether to publish and whether to record a
# version for the batched write-back. That record is the ONLY input to
# write-back-versions.sh, so a path that exits without writing one is a path on
# which main silently never learns the published version. This file drives the
# rendered template directly, because every failure of this feature so far has
# been in a branch that no test executed.
#
# The template is rendered rather than mocked: the seven {{...}} substitutions
# are plain string replacements, so the code under test is byte for byte what
# `bazel run` executes. `rlocation` is stubbed to the identity function and the
# substitutions are absolute paths, which is what makes that possible without a
# bazel runfiles tree.
set -o errexit -o nounset -o pipefail

TPL_REL="bazel/helm/push.sh.tpl"
TPL=""
for candidate in \
	"${RUNFILES_DIR:-}/_main/${TPL_REL}" \
	"${TEST_SRCDIR:-}/_main/${TPL_REL}" \
	"${BASH_SOURCE[0]%/*}/push.sh.tpl"; do
	if [[ -f "$candidate" ]]; then
		TPL="$candidate"
		break
	fi
done
if [[ -z "$TPL" ]]; then
	echo "ERROR: cannot locate push.sh.tpl in runfiles" >&2
	exit 1
fi
TPL="$(cd "$(dirname "$TPL")" && pwd)/$(basename "$TPL")"

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

# Build a workspace plus the stubs the template resolves, and render it.
# `published` decides whether `helm show chart` succeeds, and `digests_match`
# whether the missed-bump guard passes, which together select the branch.
setup() {
	local name="$1" current="$2" computed="$3" published="$4" digests_match="$5"
	local ws="$TMP/$name" stubs="$TMP/$name-stubs"
	mkdir -p "$ws/projects/demo/chart" "$stubs/bazel_tools/tools/bash/runfiles"

	# The template sources this and calls rlocation on each substitution. The
	# identity function is correct here precisely because we substitute absolute
	# paths below.
	cat >"$stubs/bazel_tools/tools/bash/runfiles/runfiles.bash" <<-'RUNFILES'
		rlocation() { echo "$1"; }
	RUNFILES

	printf 'name: demo\nversion: %s\n' "$current" >"$ws/projects/demo/chart/Chart.yaml"
	git -C "$ws" init --quiet --initial-branch=main
	git -C "$ws" config user.email "t@e.com"
	git -C "$ws" config user.name "t"
	git -C "$ws" add -A
	git -C "$ws" commit --quiet -m "chore: seed"

	printf '#!/usr/bin/env bash\necho "%s"\n' "$computed" >"$stubs/chart-version.sh"
	# `helm show chart` exiting 0 is how the template reads "already published".
	printf '#!/usr/bin/env bash\nexit %s\n' "$([[ "$published" == "yes" ]] && echo 0 || echo 1)" \
		>"$stubs/helm"
	printf '#!/usr/bin/env bash\nexit 0\n' >"$stubs/crane"
	printf '#!/usr/bin/env bash\nexit %s\n' "$([[ "$digests_match" == "yes" ]] && echo 0 || echo 1)" \
		>"$stubs/check-missed-bump.sh"
	: >"$stubs/chart.tgz"
	chmod +x "$stubs"/*.sh "$stubs/helm" "$stubs/crane"

	sed \
		-e "s|{{HELM}}|$stubs/helm|" \
		-e "s|{{CRANE}}|$stubs/crane|" \
		-e "s|{{CHART_TGZ}}|$stubs/chart.tgz|" \
		-e "s|{{REPOSITORY}}|oci://example.test/charts|" \
		-e "s|{{CHART_VERSION_SH}}|$stubs/chart-version.sh|" \
		-e "s|{{CHART_DIR}}|projects/demo/chart|" \
		-e "s|{{CHECK_MISSED_BUMP}}|$stubs/check-missed-bump.sh|" \
		"$TPL" >"$stubs/push.sh"
	chmod +x "$stubs/push.sh"
	echo "$ws"
}

run_push() {
	local ws="$1" name="$2"
	(
		cd "$ws" &&
			RUNFILES_DIR="$TMP/$name-stubs" \
				BUILD_WORKSPACE_DIRECTORY="$ws" \
				bash "$TMP/$name-stubs/push.sh" 2>&1
	) || true
}

record_version() {
	local ws="$1"
	if [[ -f "$ws/.chart-version-records/demo" ]]; then
		awk '{print $2}' "$ws/.chart-version-records/demo"
	else
		echo "(no record)"
	fi
}

# 1. THE ORPHAN. The computed version is already published and its digests
# match, but main's Chart.yaml still names an older one. That is the state left
# behind when a publish succeeds and its write-back is then refused, which is
# exactly what happened on 2026-08-10. Recording is the only way main ever
# catches up: with no record the write-back has nothing to do, every later run
# recomputes this same version and lands here again, and a chart whose content
# never changes stays stranded forever.
ws=$(setup orphan 0.2.0 0.3.3 yes yes)
out=$(run_push "$ws" orphan)
expect "records the published version" "0.3.3" "$(record_version "$ws")" \
	"main was behind at 0.2.0"
if grep -q "main still says 0.2.0" <<<"$out"; then
	echo "ok: says why it recorded"
else
	echo "FAIL: did not report that main was behind" >&2
	FAILURES=$((FAILURES + 1))
fi

# 2. CONVERGENCE. Once the write-back has landed, main names the published
# version and this must go quiet. Without this the previous case would rewrite
# the same record on every run forever, and the write-back would commit to main
# on every merge with nothing to change.
ws=$(setup aligned 0.3.3 0.3.3 yes yes)
out=$(run_push "$ws" aligned)
expect "aligned records nothing" "(no record)" "$(record_version "$ws")" \
	"main already names it"
if grep -q "nothing to publish" <<<"$out"; then
	echo "ok: reports nothing to publish"
else
	echo "FAIL: expected the quiet 'nothing to publish' branch" >&2
	FAILURES=$((FAILURES + 1))
fi

if [[ "$FAILURES" -gt 0 ]]; then
	echo "${FAILURES} test(s) failed"
	exit 1
fi
echo "All push tests passed"
