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
# The template is rendered rather than mocked: the six {{...}} substitutions
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
	local version_status="${6:-0}" escalated="${7-0.4.0}" escalation_status="${8:-0}" escalated_published="${9:-no}"
	local ws="$TMP/$name" stubs="$TMP/$name-stubs"
	mkdir -p "$ws/projects/demo/chart" "$stubs/bazel_tools/tools/bash/runfiles"

	# The template sources this and calls rlocation on each substitution. The
	# identity function is correct here precisely because we substitute absolute
	# paths below.
	cat >"$stubs/bazel_tools/tools/bash/runfiles/runfiles.bash" <<-'RUNFILES'
		rlocation() { echo "$1"; }
	RUNFILES

	printf 'apiVersion: v2\nname: demo\nversion: %s\n' "$current" >"$ws/projects/demo/chart/Chart.yaml"
	git -C "$ws" init --quiet --initial-branch=main
	git -C "$ws" config user.email "t@e.com"
	git -C "$ws" config user.name "t"
	git -C "$ws" add -A
	git -C "$ws" commit --quiet -m "chore: seed"

	# Keep source values distinct from the build-time digest-pinned package.
	mkdir -p "$stubs/archive/demo"
	cp "$ws/projects/demo/chart/Chart.yaml" "$stubs/archive/demo/Chart.yaml"
	cat >"$stubs/archive/demo/values.yaml" <<-'VALUES'
		replicas: 2
		image:
		  repository: ghcr.io/example/demo
		  digest: sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
		sidecar:
		  image: ghcr.io/example/helper@sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb
	VALUES
	tar -czf "$stubs/chart.tgz" -C "$stubs/archive" demo
	# Write fixture configuration as shell-quoted assignments, including empty
	# escalation output. Each stub loads this independently of the caller env.
	printf '%s=%q\n' current "$current" computed "$computed" published "$published" \
		digests_match "$digests_match" version_status "$version_status" \
		escalated "$escalated" escalation_status "$escalation_status" \
		escalated_published "$escalated_published" >"$stubs/config"
	cat >"$stubs/common.sh" <<-'COMMON'
		set -euo pipefail
		stubs=$(cd "$(dirname "$0")" && pwd)
		source "$stubs/config"
		repository=oci://example.test/charts
		unexpected() {
			echo "unexpected stub operation: $*" >>"$stubs/unexpected"
			echo "unexpected stub operation: $*" >&2
			exit 97
		}
	COMMON
	cat >"$stubs/chart-version.sh" <<-'VERSION'
		#!/usr/bin/env bash
		source "$(dirname "$0")/common.sh"
		[[ $# == 2 && $1 == projects/demo/chart && $2 == //projects/demo/chart:chart.package ]] || unexpected "version args: $*"
		[[ $PWD == "$BUILD_WORKSPACE_DIRECTORY" ]] || unexpected "version cwd: $PWD"
		printf '%s %s %s\n' "${CHART_VERSION_ALL_PATHS-unset}" "$1" "$2" >>"$stubs/version.calls"
		calls=$(wc -l <"$stubs/version.calls")
		if [[ $calls == 1 && ${CHART_VERSION_ALL_PATHS-unset} == unset ]]; then
			printf '%s\n' "$computed"
			exit "$version_status"
		elif [[ $calls == 2 && ${CHART_VERSION_ALL_PATHS-unset} == 1 ]]; then
			printf '%s\n' "$escalated"
			exit "$escalation_status"
		fi
		unexpected "version call $calls, all paths: ${CHART_VERSION_ALL_PATHS-unset}"
	VERSION
	cat >"$stubs/helm" <<-'HELM'
		#!/usr/bin/env bash
		source "$(dirname "$0")/common.sh"
		printf '%s\n' "$*" >>"$stubs/helm.calls"
		case "${1-}" in
			show)
				[[ $# == 5 && $2 == chart && $3 == "$repository/demo" && $4 == --version ]] || unexpected "show: $*"
				printf '%s\n' "$5" >>"$stubs/show.calls"
				if [[ $5 == "$computed" ]]; then
					exists=$published
				elif [[ $5 == "$escalated" ]]; then
					exists=$escalated_published
				else
					unexpected "show version: $5"
				fi
				if [[ $exists == yes ]]; then
					printf 'name: demo\nversion: %s\n' "$5"
				else
					echo 'manifest unknown' >&2
					exit 1
				fi
				;;
			package)
				[[ $# == 4 && $3 == --destination && $2 == "$4/demo" && -d $2 ]] || unexpected "package: $*"
				version=$(awk '/^version:/ {print $2}' "$2/Chart.yaml")
				[[ $version == "$escalated" ]] || unexpected "package version: $version"
				cmp "$stubs/archive/demo/values.yaml" "$2/values.yaml" || unexpected 'package values changed'
				tar -czf "$4/demo-$version.tgz" -C "$4" demo
				printf '%s\n' "$4/demo-$version.tgz" >>"$stubs/package.calls"
				;;
			push)
				[[ $# == 3 && $3 == "$repository" && -f $2 && $2 == */demo-"$escalated".tgz ]] || unexpected "push: $*"
				[[ $(cat "$stubs/package.calls") == "$2" ]] || unexpected 'push did not use packaged artifact'
				mkdir -p "$stubs/pushed"
				tar -xzf "$2" -C "$stubs/pushed"
				cmp "$stubs/archive/demo/values.yaml" "$stubs/pushed/demo/values.yaml" || unexpected 'pushed values changed'
				[[ $(awk '/^version:/ {print $2}' "$stubs/pushed/demo/Chart.yaml") == "$escalated" ]] || unexpected 'pushed Chart.yaml version'
				printf '%s\n' "$2" >>"$stubs/push.calls"
				;;
			*) unexpected "helm: $*" ;;
		esac
	HELM
	cat >"$stubs/check-missed-bump.sh" <<-'GUARD'
		#!/usr/bin/env bash
		source "$(dirname "$0")/common.sh"
		[[ $# == 3 && $1 == demo && $2 == "$computed" && $3 == "$stubs/chart.tgz" ]] || unexpected "guard: $*"
		[[ $HELM == "$stubs/helm" && $REPOSITORY == "$repository" ]] || unexpected 'guard environment'
		mkdir -p "$stubs/guarded"
		tar -xzf "$3" -C "$stubs/guarded"
		cmp "$stubs/archive/demo/Chart.yaml" "$stubs/guarded/demo/Chart.yaml" || unexpected 'guard did not get fresh chart'
		cmp "$stubs/archive/demo/values.yaml" "$stubs/guarded/demo/values.yaml" || unexpected 'guard values changed'
		printf '%s %s %s\n' "$1" "$2" "$3" >>"$stubs/guard.calls"
		[[ $digests_match == yes ]]
	GUARD
	chmod +x "$stubs"/*.sh "$stubs/helm"

	sed \
		-e "s|{{HELM}}|$stubs/helm|" \
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
	)
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

# 2. An aligned version still belongs in the complete publication receipt.
# Recording it does not republish the chart or artificially bump its version.
ws=$(setup aligned 0.3.3 0.3.3 yes yes)
out=$(run_push "$ws" aligned)
expect "aligned version recorded" "0.3.3" "$(record_version "$ws")" \
	"receipt includes content-verified reused charts"
if grep -q "nothing to publish" <<<"$out"; then
	echo "ok: reports nothing to publish"
else
	echo "FAIL: expected the quiet 'nothing to publish' branch" >&2
	FAILURES=$((FAILURES + 1))
fi

# 3. A failed version computation must stop publication. Falling back to the
# Chart.yaml version can either mutate an existing artifact or silently skip a
# real content change, so the caller propagates the failure instead.
ws=$(setup versionfail 0.3.3 ignored no yes 9)
set +e
out=$(run_push "$ws" versionfail)
push_rc=$?
set -e
expect "version failure propagates" "1" "$([[ $push_rc -ne 0 ]] && echo 1 || echo 0)" \
	"chart-version.sh exited non-zero"
expect "version failure records nothing" "(no record)" "$(record_version "$ws")" \
	"publication stopped before write-back"
if grep -q "refusing to reuse Chart.yaml's 0.3.3" <<<"$out"; then
	echo "ok: reports the fail-safe refusal"
else
	echo "FAIL: missing fail-safe version error: ${out}" >&2
	FAILURES=$((FAILURES + 1))
fi

# 4. A packaged chart missing from the runfiles must stop before the registry.
# Under --remote_download_minimal a remote-only .package output leaves rlocation
# printing "", and the factory chart's first publish then died in tar AFTER
# announcing the release. The guard has to fire first and name the cause.
ws=$(setup missingtgz 0.1.0 0.1.4 no yes)
rm "$TMP/missingtgz-stubs/chart.tgz"
set +e
out=$(run_push "$ws" missingtgz)
push_rc=$?
set -e
expect "missing chart tgz fails" "1" "$([[ $push_rc -ne 0 ]] && echo 1 || echo 0)" \
	"CHART_TGZ does not exist on disk"
expect "missing chart tgz records nothing" "(no record)" "$(record_version "$ws")" \
	"publication stopped before write-back"
if grep -q "is not in the runfiles on disk" <<<"$out" && ! grep -q "Publishing" <<<"$out"; then
	echo "ok: reports the missing package before publishing"
else
	echo "FAIL: missing package not reported before publishing: ${out}" >&2
	FAILURES=$((FAILURES + 1))
fi

# A missing flag or wrong helper argument must never look like a deliberate
# refusal. Check fixture errors separately from the production diagnostics.
no_push() {
	local name="$1"
	expect "$name pushes nothing" no "$([[ -e "$TMP/$name-stubs/push.calls" ]] && echo yes || echo no)" "registry writes remain local stubs"
}

collision_calls() {
	local name="$1" shows="$2" stubs="$TMP/$1-stubs"
	expect "$name checks exact versions in order" "$shows" "$(cat "$stubs/show.calls")" "registry version lookups"
	expect "$name computes repo-wide with chart arguments" \
		$'unset projects/demo/chart //projects/demo/chart:chart.package\n1 projects/demo/chart //projects/demo/chart:chart.package' \
		"$(cat "$stubs/version.calls")" "initial computation then escalation"
	expect "$name guards fresh archive at initial version" \
		"demo 0.3.3 $stubs/chart.tgz" "$(cat "$stubs/guard.calls")" "digest mismatch selects escalation"
}

# 5. Changed digests under a published version recover at an absent version.
ws=$(setup recovery 0.2.0 0.3.3 yes no)
set +e
out=$(run_push "$ws" recovery)
push_rc=$?
set -e
expect "recovery succeeds" 0 "$push_rc" "$out"
collision_calls recovery $'0.3.3\n0.4.0'
expect "recovery records escalated version" 0.4.0 "$(record_version "$ws")" "write-back receipt"
stubs="$TMP/recovery-stubs"
expect "recovery packages exactly once" 1 "$(wc -l <"$stubs/package.calls" | tr -d ' ')" "real local archive"
expect "recovery pushes exactly once" 1 "$(wc -l <"$stubs/push.calls" | tr -d ' ')" "push inspects package values and Chart.yaml"
expect "recovery pushes escalated package" "$(cat "$stubs/package.calls")" "$(cat "$stubs/push.calls")" "same artifact"
expect "recovery preserves values and pinned digests" yes \
	"$(cmp -s "$stubs/archive/demo/values.yaml" "$stubs/pushed/demo/values.yaml" && echo yes || echo no)" "all original values survive"
expect "recovery updates packaged Chart.yaml version" 0.4.0 \
	"$(awk '/^version:/ {print $2}' "$stubs/pushed/demo/Chart.yaml")" "published chart metadata"

# 6-9. Each refusal must reach its own diagnostic, never an incidental tar or
# fixture error. Empty and unchanged output share a production diagnostic, but
# use different helper outputs and still require a successful helper call.
for name in escalationfail empty unchanged collision; do
	escalated=0.4.0 escalation_status=0 escalated_published=no
	shows=0.3.3
	case "$name" in
	escalationfail)
		escalation_status=9
		diagnostic='ERROR: repo-wide version escalation failed for demo.'
		;;
	empty | unchanged)
		if [[ $name == empty ]]; then escalated=""; else escalated=0.3.3; fi
		diagnostic='ERROR: demo 0.3.3 is published with DIFFERENT image digests, and no higher version could be computed.'
		;;
	collision)
		escalated_published=yes
		shows=$'0.3.3\n0.4.0'
		diagnostic='ERROR: escalated version demo 0.4.0 is ALSO already published.'
		;;
	esac
	ws=$(setup "$name" 0.2.0 0.3.3 yes no 0 "$escalated" "$escalation_status" "$escalated_published")
	set +e
	out=$(run_push "$ws" "$name")
	push_rc=$?
	set -e
	expect "$name refuses publication" 1 "$push_rc" "$out"
	expect "$name reaches specific diagnostic" yes \
		"$(grep -Fq "$diagnostic" <<<"$out" && echo yes || echo no)" "$diagnostic"
	collision_calls "$name" "$shows"
	no_push "$name"
	expect "$name records nothing" '(no record)' "$(record_version "$ws")" "refusal before receipt"
	expect "$name does not repackage" no "$([[ -e "$TMP/$name-stubs/package.calls" ]] && echo yes || echo no)" "refusal before artifact changes"
done

for name in orphan aligned versionfail missingtgz; do no_push "$name"; done
expect "initial helper failure stops before registry" no \
	"$([[ -e "$TMP/versionfail-stubs/helm.calls" ]] && echo yes || echo no)" "no registry access"
expect "missing package stops before registry" no \
	"$([[ -e "$TMP/missingtgz-stubs/helm.calls" ]] && echo yes || echo no)" "runfile guard"
for name in orphan aligned; do
	expect "$name reaches digest guard" 1 "$(wc -l <"$TMP/$name-stubs/guard.calls" | tr -d ' ')" "content-verified reuse"
done
for name in orphan aligned versionfail missingtgz recovery escalationfail empty unchanged collision; do
	expect "$name uses only expected stub operations" no \
		"$([[ -e "$TMP/$name-stubs/unexpected" ]] && echo yes || echo no)" "fixture contract"
done

if [[ "$FAILURES" -gt 0 ]]; then
	echo "${FAILURES} test(s) failed"
	exit 1
fi
echo "All push tests passed"
