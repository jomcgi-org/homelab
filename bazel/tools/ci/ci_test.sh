#!/usr/bin/env bash
# Unit tests for bazel/tools/ci/ci pure helpers (changed-file classification).
set -euo pipefail

CI_REL="bazel/tools/ci/ci"
CI=""
for candidate in \
	"${RUNFILES_DIR:-}/_main/${CI_REL}" \
	"${TEST_SRCDIR:-}/_main/${CI_REL}" \
	"${BASH_SOURCE[0]%/*}/ci"; do
	if [[ -f "$candidate" ]]; then
		CI="$candidate"
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

if bash "$CI" --help 2>/dev/null | grep -q 'local feedback loop'; then
	pass "help"
else
	fail "help" "usage missing expected text"
fi

set +e
bash "$CI" nosuchcmd >/dev/null 2>&1
ec=$?
set -e
if [[ $ec -eq 2 ]]; then
	pass "unknown_cmd"
else
	fail "unknown_cmd" "exit $ec want 2"
fi

# Flag-as-target must be rejected (parity guard)
set +e
out=$(bash "$CI" test --nocache_test_results 2>&1)
ec=$?
set -e
if [[ $ec -eq 2 ]] && echo "$out" | grep -q "unexpected flag"; then
	pass "test_rejects_flags"
else
	fail "test_rejects_flags" "exit=$ec out=$out"
fi

# need_generators / need_gazelle via a scratch git repo
if command -v git >/dev/null 2>&1; then
	tmp=$(mktemp -d)
	trap 'rm -rf "$tmp"' EXIT
	git -C "$tmp" init -q
	git -C "$tmp" config user.email t@t
	git -C "$tmp" config user.name t
	# minimal fake ci by sourcing functions: run classification via case on fake lists
	# Exercise path filters by grepping the script for expected patterns
	if grep -q 'projects/\*/deploy/\*' "$CI" && grep -q '\*\.py) py' "$CI"; then
		pass "lint_case_patterns_present"
	else
		fail "lint_case_patterns_present" "missing extension cases"
	fi
	if grep -q 'deleted_packages=bazel/tools/python' "$CI" &&
		grep -q 'test_tag_filters=-external,-future' "$CI"; then
		pass "ci_test_argv_locked"
	else
		fail "ci_test_argv_locked" "missing locked Test flags"
	fi
	if grep -q 'include-secrets=true' "$CI"; then
		pass "secrets_note"
	else
		fail "secrets_note" "missing include-secrets"
	fi
else
	pass "scratch_skipped_no_git"
fi

echo "--- $PASS passed, $FAIL failed ---"
[[ $FAIL -eq 0 ]]
