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
pass() { echo "PASS [$1]"; PASS=$((PASS + 1)); }
fail() { echo "FAIL [$1]: $2"; FAIL=$((FAIL + 1)); }

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

if grep -qE '\*\.py\)' "$CI" && grep -qE '\*\.js' "$CI"; then
	pass "lint_extensions"
else
	fail "lint_extensions" "missing py/js cases"
fi

if grep -q 'need_generators' "$CI" && grep -q 'need_gazelle' "$CI"; then
	pass "regen_helpers"
else
	fail "regen_helpers" "missing need_* helpers"
fi

echo "--- $PASS passed, $FAIL failed ---"
[[ $FAIL -eq 0 ]]
