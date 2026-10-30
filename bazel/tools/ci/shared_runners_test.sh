#!/usr/bin/env bash
# Execute the workflow's actual dispatch blocks with argument-sensitive tools.
set -euo pipefail
tools="${BASH_SOURCE[0]%/*}"
tools="$(cd "$tools" && pwd)"
yaml="$tools/../../../buildbuddy.yaml"
tmp="$(mktemp -d "${TEST_TMPDIR:-/tmp}/shared-runners.XXXXXX")"
trap 'rm -rf "$tmp"' EXIT
mkdir -p "$tmp/work/bazel/tools/ci" "$tmp/bin"
cp "$tools/ratchets.sh" "$tools/local-affected-test.sh" "$tmp/work/bazel/tools/ci/"
awk '
    /base_ref="origin\/\$\{GIT_REPO_DEFAULT_BRANCH/ { capture=1 }
    /commit_format_drift\(\)/ { capture=0 }
    capture { sub(/^          /, ""); print }
' "$yaml" >"$tmp/guards"
awk '
    /# ---- 2\. tests / { capture=1; next }
    /# ---- 3\. deploy / { capture=0 }
    capture { sub(/^          /, ""); print }
' "$yaml" >"$tmp/tests"
[[ -s "$tmp/guards" && -s "$tmp/tests" ]]
cat >"$tmp/bin/git" <<'EOF'
#!/usr/bin/env bash
set -eu
printf 'git %s\n' "$*" >>"$LOG"
case "$*" in
    'fetch origin main --quiet') ;;
    'merge-base origin/main HEAD')
        [[ "${MISSING_BASE:-0}" == 0 ]] || exit 8
        echo frozen-base ;;
    'ls-files --others --exclude-standard') ;;
    *) echo "unexpected git: $*" >&2; exit 99 ;;
esac
EOF
cat >"$tmp/bin/python3" <<'EOF'
#!/usr/bin/env bash
set -eu
printf 'guard %s\n' "$*" >>"$LOG"
case "$1" in
    ./bazel/tools/ci/verification_genrule_guard.py) [[ "$*" == "$1 ." ]] ;;
    ./bazel/tools/ci/central_build_ratchet.py|./bazel/tools/ci/chart_version_guard.py|./bazel/tools/ci/source_ratchet.py)
        [[ "$*" == "$1 frozen-base HEAD" ]] ;;
    *) exit 99 ;;
esac
[[ "${FAIL_GUARD:-}" != "${1##*/}" ]] || exit 7
EOF
cat >"$tmp/work/bazel/tools/ci/affected-targets.sh" <<'EOF'
#!/usr/bin/env bash
set -eu
printf 'selector %s\n' "$*" >>"$LOG"
[[ "$*" == 'frozen-base HEAD -- --config=ci --deleted_packages=bazel/tools/python' ]]
[[ "${SELECTOR_STATUS:-0}" == 0 ]] || exit "$SELECTOR_STATUS"
[[ -z "${PATTERNS:-}" ]] || printf '%s\n' "$PATTERNS"
EOF
cat >"$tmp/bin/bazel" <<'EOF'
#!/usr/bin/env bash
set -eu
if [[ "${2:-}" == '//...' ]]; then
    [[ "$*" == 'test //... --config=ci --deleted_packages=bazel/tools/python' ]]
    printf 'full //...\n' >>"$LOG"
else
    [[ $# == 4 && "$1" == test && "$2" == --config=ci && "$3" == --deleted_packages=bazel/tools/python && "$4" == --target_pattern_file=* ]]
    [[ "$(cat "${4#*=}")" == "$PATTERNS" ]]
    printf 'subset %s\n' "$PATTERNS" >>"$LOG"
fi
echo 'Executed 1 out of 1 test: 1 test passes.'
exit "${BAZEL_STATUS:-0}"
EOF
chmod +x "$tmp/bin/"* "$tmp/work/bazel/tools/ci/"*.sh
export PATH="$tmp/bin:$PATH" BUILD_WORKSPACE_DIRECTORY="$tmp/work" LOG="$tmp/log"
cd "$tmp/work"
run() {
	local expected="$1" actual=0
	: >"$LOG"
	bash -euc 'source "$1"; source "$2"; echo reached >>"$LOG"' bash "$tmp/guards" "$tmp/tests" >"$tmp/out" 2>&1 || actual=$?
	[[ "$actual" == "$expected" ]] || {
		cat "$tmp/out"
		echo "expected $expected got $actual"
		exit 1
	}
}
export GIT_PR_NUMBER=7455 GIT_BRANCH=factory/test PATTERNS=//p:unit_test WRITE_BACK_ONLY=false
run 0
expected=$'git fetch origin main --quiet\ngit merge-base origin/main HEAD\nguard ./bazel/tools/ci/verification_genrule_guard.py .\nguard ./bazel/tools/ci/central_build_ratchet.py frozen-base HEAD\nguard ./bazel/tools/ci/chart_version_guard.py frozen-base HEAD\nguard ./bazel/tools/ci/source_ratchet.py frozen-base HEAD\ngit ls-files --others --exclude-standard\nselector frozen-base HEAD -- --config=ci --deleted_packages=bazel/tools/python\nsubset //p:unit_test\nreached'
[[ "$(cat "$LOG")" == "$expected" ]]
echo 'PASS workflow PR frozen base, ordered guards and selective target-pattern file'
for guard in verification_genrule_guard.py central_build_ratchet.py chart_version_guard.py source_ratchet.py; do
	FAIL_GUARD="$guard" run 7
	[[ "$(tail -n 1 "$LOG")" == guard*"$guard"* ]]
	! grep -qE 'selector|subset|reached' "$LOG"
done
echo 'PASS workflow each guard failure stops all subsequent work'
MISSING_BASE=1 run 8
! grep -qE 'guard |selector|reached' "$LOG"
echo 'PASS workflow missing merge base stops guards and tests'
PATTERNS='' run 0
! grep -qE '^subset|^full' "$LOG"
grep -qF 'no Bazel targets affected' "$tmp/out"
BAZEL_STATUS=4 run 0
grep -qF 'affected targets contain no tests' "$tmp/out"
BAZEL_STATUS=3 run 3
! grep -qF reached "$LOG"
SELECTOR_STATUS=9 run 9
! grep -qE '^subset|^full|reached' "$LOG"
echo 'PASS workflow empty selection, exit 4, real Bazel failure and selector failure'
for route in queue main push fork-pr; do
	export GIT_PR_NUMBER=0 GIT_BRANCH=main
	case "$route" in
	queue) GIT_BRANCH=gh-readonly-queue/main/pr-7455 ;;
	push) GIT_BRANCH=feature ;;
	fork-pr) GIT_PR_NUMBER=7455 ;;
	esac
	run 0
	if [[ "$route" == fork-pr ]]; then
		grep -qxF 'subset //p:unit_test' "$LOG"
	else
		grep -qxF 'full //...' "$LOG"
		! grep -qF selector "$LOG"
		BAZEL_STATUS=4 run 4
	fi
done
echo 'PASS queue/main/push full suite remains strict; fork main PR is selective'
# ratchets.sh itself rejects an absent/empty interface before starting a guard.
for args in absent empty; do
	: >"$LOG"
	status=0
	if [[ "$args" == absent ]]; then
		./bazel/tools/ci/ratchets.sh >"$tmp/out" 2>&1 || status=$?
	else
		./bazel/tools/ci/ratchets.sh '' HEAD >"$tmp/out" 2>&1 || status=$?
	fi
	[[ "$status" == 2 && ! -s "$LOG" ]]
done
echo 'PASS shared guard runner missing-base interface fails closed'
