#!/usr/bin/env bash
# Executes the real guard against failing and fixed BUILD fixtures.

set -euo pipefail

RUNFILES_ROOT="${RUNFILES_DIR:-.}"
MAIN_ROOT="$RUNFILES_ROOT/_main"
if [[ ! -d "$MAIN_ROOT" ]]; then
	MAIN_ROOT="$RUNFILES_ROOT"
fi

GUARD="$MAIN_ROOT/bazel/semgrep/rules/verification_genrule_guard"
FAILING_FIXTURE="$MAIN_ROOT/bazel/semgrep/rules/bazel/verification-genrule-missing-tag-failing.build"
PASSING_FIXTURE="$MAIN_ROOT/bazel/semgrep/rules/bazel/verification-genrule-missing-tag-passing.build"
IGNORED_FIXTURE="$MAIN_ROOT/bazel/semgrep/rules/bazel/verification-genrule-missing-tag-ignored.bzl"

for required in "$GUARD" "$FAILING_FIXTURE" "$PASSING_FIXTURE" "$IGNORED_FIXTURE"; do
	if [[ ! -e "$required" ]]; then
		echo "ERROR: required runfile not found: $required" >&2
		exit 1
	fi
done

BAD_ROOT="$TEST_TMPDIR/failing"
GOOD_ROOT="$TEST_TMPDIR/passing"
mkdir -p "$BAD_ROOT/new-package" "$GOOD_ROOT/new-package"

cp "$FAILING_FIXTURE" "$BAD_ROOT/BUILD"
cp "$FAILING_FIXTURE" "$BAD_ROOT/new-package/BUILD.bazel"
cp "$IGNORED_FIXTURE" "$BAD_ROOT/ignored.bzl"
cp "$PASSING_FIXTURE" "$GOOD_ROOT/BUILD"
cp "$PASSING_FIXTURE" "$GOOD_ROOT/new-package/BUILD.bazel"
cp "$IGNORED_FIXTURE" "$GOOD_ROOT/ignored.bzl"

BAD_OUTPUT="$TEST_TMPDIR/failing.out"
if "$GUARD" "$BAD_ROOT" >"$BAD_OUTPUT" 2>&1; then
	echo "ERROR: guard accepted untagged suite-shaped genrules" >&2
	cat "$BAD_OUTPUT" >&2
	exit 1
fi

# Each failing BUILD form contains four violations. The ignored .bzl contains
# another violation and proves the source filter is limited to BUILD files.
finding_count=$(grep -c 'verification-genrule-missing-tag' "$BAD_OUTPUT" || true)
if [[ "$finding_count" -ne 8 ]]; then
	echo "ERROR: expected 8 findings across BUILD and BUILD.bazel, got $finding_count" >&2
	cat "$BAD_OUTPUT" >&2
	exit 1
fi

if ! "$GUARD" "$GOOD_ROOT"; then
	echo "ERROR: guard rejected fixtures after adding verification" >&2
	exit 1
fi

echo "PASSED: verification genrule guard detects missing tags and accepts fixed BUILD forms"
