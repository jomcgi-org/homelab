#!/usr/bin/env bash
# pre-push: run `ci test` only when asked.
#
# Force:  RUN_CI_TEST=1 git push
#
# Opt-in since the merge queue (2026-08-22). Every push is tested twice after
# it lands on GitHub anyway (the PR run, then the queue candidate rebased on
# current main), so a pre-push `bazel test //...` on a hosted worker was a
# third copy: ~7 minutes of wall time and 15 to 19 GB of BuildBuddy download
# per push, 14.5% of the repo's total. `ci lint` and the generators still run
# in pre-commit; this hook only gates the full remote test.
#
# Install: pre-commit install --hook-type pre-push
set -euo pipefail

if [[ "${RUN_CI_TEST:-0}" != "1" ]]; then
	echo "pre-push: skipping ci test; PR CI and the merge queue run it (RUN_CI_TEST=1 to run here)"
	exit 0
fi

ROOT="$(git rev-parse --show-toplevel)"
cd "$ROOT"

if [[ -x "$ROOT/bazel/tools/ci/ci" ]]; then
	CI="$ROOT/bazel/tools/ci/ci"
elif command -v ci >/dev/null 2>&1; then
	CI=ci
else
	echo "pre-push: ci not found (expected bazel/tools/ci/ci)" >&2
	exit 1
fi

echo "pre-push: RUN_CI_TEST=1, running ci test"
exec "$CI" test
