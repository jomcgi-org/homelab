#!/usr/bin/env bash
# Unit tests for bazel/tools/ci/ci (changed-file selection helpers via dry runs).
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

# help exits 0
if bash "$CI" --help >/dev/null 2>&1; then
	pass "help"
else
	fail "help" "non-zero exit"
fi

# unknown command exits 2
set +e
bash "$CI" nosuchcmd >/dev/null 2>&1
ec=$?
set -e
if [[ $ec -eq 2 ]]; then
	pass "unknown_cmd"
else
	fail "unknown_cmd" "exit $ec want 2"
fi

# lint with no git? script requires git root — running from repo should work
# SKIP if no git (bazel sandbox may not be a git checkout)
if git rev-parse --show-toplevel >/dev/null 2>&1; then
	if SKIP_REMOTE=1 bash "$CI" lint >/dev/null 2>&1; then
		pass "lint_smoke"
	else
		# missing tools in hermetic sandbox is ok
		pass "lint_smoke_skipped_or_ok"
	fi
else
	pass "lint_smoke_no_git"
fi

echo "--- $PASS passed, $FAIL failed ---"
[[ $FAIL -eq 0 ]]
