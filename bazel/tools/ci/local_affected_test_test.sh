#!/usr/bin/env bash
set -euo pipefail

RUNNER=""
for candidate in \
	"${RUNFILES_DIR:-}/_main/bazel/tools/ci/local-affected-test.sh" \
	"${TEST_SRCDIR:-}/_main/bazel/tools/ci/local-affected-test.sh" \
	"${BASH_SOURCE[0]%/*}/local-affected-test.sh"; do
	if [[ -f "$candidate" ]]; then
		RUNNER="$(cd "${candidate%/*}" && pwd)/${candidate##*/}"
		break
	fi
done
if [[ -z "$RUNNER" ]]; then
	echo "ERROR: cannot locate local-affected-test.sh" >&2
	exit 1
fi

PASS=0
FAIL=0
pass() {
	echo "PASS [$1]"
	PASS=$((PASS + 1))
}
fail() {
	echo "FAIL [$1]: $2"
	FAIL=$((FAIL + 1))
}

TMP="${TEST_TMPDIR:-$(mktemp -d)}"
ROOT="$TMP/root"
STUB_BIN="$TMP/bin"
mkdir -p "$ROOT/bazel/tools/ci" "$STUB_BIN"

cat >"$ROOT/bazel/tools/ci/affected-targets.sh" <<'EOF'
#!/usr/bin/env bash
printf '%s\n' "$@" >"$AFFECTED_LOG"
[[ -n "${AFFECTED_OUTPUT:-}" ]] && printf '%s\n' "$AFFECTED_OUTPUT"
exit 0
EOF
chmod +x "$ROOT/bazel/tools/ci/affected-targets.sh"

cat >"$STUB_BIN/git" <<'EOF'
#!/usr/bin/env bash
printf '%s\n' "$@" >>"$GIT_LOG"
if [[ "$1 ${2:-}" == "ls-files --others" ]]; then
	[[ -n "${GIT_UNTRACKED_OUTPUT:-}" ]] && printf '%s\n' "$GIT_UNTRACKED_OUTPUT"
fi
exit 0
EOF
chmod +x "$STUB_BIN/git"

cat >"$STUB_BIN/bazel" <<'EOF'
#!/usr/bin/env bash
printf '%s\n' "$@" >"$BAZEL_LOG"
for argument in "$@"; do
	case "$argument" in
	--target_pattern_file=*) cat "${argument#*=}" >"$TARGETS_LOG" ;;
	esac
done
[[ -n "${BAZEL_OUTPUT:-}" ]] && printf '%s\n' "$BAZEL_OUTPUT"
exit "${BAZEL_STATUS:-0}"
EOF
chmod +x "$STUB_BIN/bazel"

run_runner() {
	BUILD_WORKSPACE_DIRECTORY="$ROOT" CI_BASE_REF=origin/main PATH="$STUB_BIN:$PATH" \
		AFFECTED_LOG="$TMP/affected.log" GIT_LOG="$TMP/git.log" \
		GIT_UNTRACKED_OUTPUT="${GIT_UNTRACKED_OUTPUT:-}" \
		BAZEL_LOG="$TMP/bazel.log" TARGETS_LOG="$TMP/targets.log" \
		"$RUNNER"
}

rm -f "$TMP"/*.log
buildbuddy_patch="0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
printf 'diff --git a/file b/file\n' >"$ROOT/$buildbuddy_patch"
if AFFECTED_OUTPUT="" GIT_UNTRACKED_OUTPUT="$buildbuddy_patch" run_runner >"$TMP/buildbuddy-patch.out" 2>&1 &&
	[[ ! -e "$ROOT/$buildbuddy_patch" ]]; then
	pass "buildbuddy_patch_removed"
else
	fail "buildbuddy_patch_removed" "$(tr '\n' ' ' <"$TMP/buildbuddy-patch.out")"
fi

rm -f "$TMP"/*.log
if AFFECTED_OUTPUT="//p:test" BAZEL_OUTPUT="Executed 1 out of 1 test: 1 test passes." run_runner >"$TMP/subset.out" 2>&1 &&
	grep -qF "//p:test" "$TMP/targets.log" &&
	grep -qF -- "--test_tag_filters=-external,-future" "$TMP/bazel.log" &&
	! grep -qF -- "--test_tag_filters=-external,-future" "$TMP/affected.log"; then
	pass "subset_tested"
else
	fail "subset_tested" "$(tr '\n' ' ' <"$TMP/subset.out")"
fi

rm -f "$TMP"/*.log
if AFFECTED_OUTPUT="" run_runner >"$TMP/empty.out" 2>&1 &&
	grep -qF "no Bazel targets affected" "$TMP/empty.out" && [[ ! -e "$TMP/bazel.log" ]]; then
	pass "empty_subset_skipped"
else
	fail "empty_subset_skipped" "$(tr '\n' ' ' <"$TMP/empty.out")"
fi

rm -f "$TMP"/*.log
if AFFECTED_OUTPUT="//p:lib" BAZEL_STATUS=4 run_runner >"$TMP/no-tests.out" 2>&1 &&
	grep -qF "affected targets contain no tests" "$TMP/no-tests.out"; then
	pass "no_test_targets_pass"
else
	fail "no_test_targets_pass" "$(tr '\n' ' ' <"$TMP/no-tests.out")"
fi

rm -f "$TMP"/*.log
status=0
AFFECTED_OUTPUT="//p:test" BAZEL_STATUS=3 run_runner >"$TMP/failure.out" 2>&1 || status=$?
if [[ "$status" -eq 3 ]]; then
	pass "test_failure_propagated"
else
	fail "test_failure_propagated" "expected 3, got $status"
fi

echo "--- $PASS passed, $FAIL failed ---"
[[ "$FAIL" -eq 0 ]]
