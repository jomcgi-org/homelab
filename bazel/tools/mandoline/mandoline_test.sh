#!/usr/bin/env bash
# The binary from the private image runs and has the pinned version.
set -euo pipefail
out="$("$1" --version)"
echo "$out"
grep -q '^mandoline 0\.25\.0 ' <<<"$out"

# Keep the pinned command inventory in the private Bazel test outputs.
"$1" --help >"${TEST_UNDECLARED_OUTPUTS_DIR}/mandoline-help.txt"
test -s "${TEST_UNDECLARED_OUTPUTS_DIR}/mandoline-help.txt"
