#!/usr/bin/env bash
# pre-push: run `ci test` (bb remote, 1:1 with Workflows Test) so PR checks
# mostly cache-hit. Skip with SKIP_CI_TEST=1 (docs-only / emergency).
#
# Install: pre-commit install --hook-type pre-push
# Or rely on .pre-commit-config.yaml stage: pre-push after
#   pre-commit install --hook-type pre-push
set -euo pipefail

if [[ "${SKIP_CI_TEST:-0}" == "1" ]]; then
	echo "pre-push: SKIP_CI_TEST=1 — skipping ci test"
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

echo "pre-push: running ci test (set SKIP_CI_TEST=1 to skip)…"
exec "$CI" test
