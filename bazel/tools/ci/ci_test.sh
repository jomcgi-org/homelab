#!/usr/bin/env bash
# Hermetic checks for bazel/tools/ci/ci (no git repo, no bb CLI required).
set -euo pipefail

CI_REL="bazel/tools/ci/ci"
CI=""
for candidate in \
	"${RUNFILES_DIR:-}/_main/${CI_REL}" \
	"${TEST_SRCDIR:-}/_main/${CI_REL}" \
	"${BASH_SOURCE[0]%/*}/ci"; do
	if [[ -f "$candidate" ]]; then
		CI="$(cd "${candidate%/*}" && pwd)/${candidate##*/}"
		break
	fi
done
if [[ -z "$CI" ]]; then
	echo "ERROR: cannot locate ci script" >&2
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

if grep -q 'local feedback loop' "$CI" &&
	grep -q 'SKIP_REMOTE=1' "$CI" &&
	grep -q 'include-secrets=true' "$CI"; then
	pass "header_docs"
else
	fail "header_docs" "missing usage/docs markers"
fi

if grep -q 'deleted_packages=bazel/tools/python' "$CI" &&
	grep -q 'test_tag_filters=-external,-future' "$CI" &&
	grep -qF '//...' "$CI"; then
	pass "ci_test_argv_locked"
else
	fail "ci_test_argv_locked" "missing locked Test flags"
fi

flag_ln=$(grep -n "unexpected flag" "$CI" | head -1 | cut -d: -f1)
bb_ln=$(grep -n "command -v bb" "$CI" | head -1 | cut -d: -f1)
if [[ -n "$flag_ln" && -n "$bb_ln" && "$flag_ln" -lt "$bb_ln" ]]; then
	pass "flags_before_bb"
else
	fail "flags_before_bb" "flag parse (line $flag_ln) must precede bb check (line $bb_ln)"
fi

if grep -qE '\*\.py\)' "$CI" && grep -qE '\*\.js' "$CI" && grep -qE '\*\.svelte' "$CI"; then
	pass "lint_extensions"
else
	fail "lint_extensions" "missing py/js/svelte cases"
fi

if grep -q 'need_generators' "$CI" && grep -q 'need_gazelle' "$CI"; then
	pass "regen_helpers"
else
	fail "regen_helpers" "missing need_* helpers"
fi

TMP="${TEST_TMPDIR:-$(mktemp -d)}"
FAKE_ROOT="$TMP/ci-fake-root"
STUB_BIN="$TMP/ci-stub-bin"
mkdir -p "$FAKE_ROOT" "$STUB_BIN"

cat >"$STUB_BIN/git" <<'EOF'
#!/usr/bin/env bash
if [[ "$1 ${2:-}" == "rev-parse --show-toplevel" ]]; then
	echo "$CI_FAKE_ROOT"
fi
exit 0
EOF
chmod +x "$STUB_BIN/git"

cat >"$STUB_BIN/bb" <<'EOF'
#!/usr/bin/env bash
cat "$BB_FIXTURE"
exit "${BB_STATUS:-0}"
EOF
chmod +x "$STUB_BIN/bb"

cat >"$TMP/green_run" <<'EOF'
INFO: Analyzed 361 targets
Executed 3 out of 361 tests: 361 tests pass.
EOF
cat >"$TMP/action_failed" <<'EOF'
Command failed: exit status 1

Action failed: failed to set up git repo: exit status 1
Remote run completed at 2026-07-28 05:20:10 UTC
EOF
cat >"$TMP/missing_summary" <<'EOF'
INFO: Analyzed 361 targets
INFO: Build completed successfully, 361 total actions
EOF
cat >"$TMP/bb_failure" <<'EOF'
Executed 3 out of 361 tests: 361 tests pass.
EOF

run_behavioral_case() {
	local name="$1"
	local fixture="$2"
	local bb_status="$3"
	local want_status="$4"
	local output="$TMP/$name.out"
	local got_status=0

	if (cd "$FAKE_ROOT" && PATH="$STUB_BIN:$PATH" CI_FAKE_ROOT="$FAKE_ROOT" \
		BB_FIXTURE="$fixture" BB_STATUS="$bb_status" "$CI" test >"$output" 2>&1); then
		got_status=0
	else
		got_status=$?
	fi
	if [[ "$got_status" -eq "$want_status" ]]; then
		pass "$name"
	else
		fail "$name" "expected exit $want_status, got $got_status: $(tr '\n' ' ' <"$output")"
	fi
}

run_behavioral_case "green_run_passes" "$TMP/green_run" 0 0
run_behavioral_case "action_failed_caught" "$TMP/action_failed" 0 1
run_behavioral_case "missing_summary_caught" "$TMP/missing_summary" 0 1
run_behavioral_case "bb_failure_propagates" "$TMP/bb_failure" 1 1

echo "--- $PASS passed, $FAIL failed ---"
[[ $FAIL -eq 0 ]]
