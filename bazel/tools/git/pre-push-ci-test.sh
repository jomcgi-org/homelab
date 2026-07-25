#!/usr/bin/env bash
# pre-push: run `ci test` when the push includes non-doc code changes.
#
# Skip entirely:  SKIP_CI_TEST=1 git push
# Force always:   RUN_CI_TEST=1 git push
# Docs-only pushes (only docs/, .claude/, *.md vs origin/main) skip by default.
#
# Install: pre-commit install --hook-type pre-push
set -euo pipefail

if [[ "${SKIP_CI_TEST:-0}" == "1" ]]; then
	echo "pre-push: SKIP_CI_TEST=1, skipping ci test"
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

# shellcheck source=/dev/null
# Re-use code_changed by invoking a tiny check via bash -c sourcing is heavy;
# duplicate the path filter here for a standalone hook.
need_test=0
if [[ "${RUN_CI_TEST:-0}" == "1" ]]; then
	need_test=1
else
	base="$(git merge-base HEAD origin/main 2>/dev/null || echo origin/main)"
	while IFS= read -r f; do
		[[ -z "$f" ]] && continue
		case "$f" in
		docs/* | .claude/* | *.md | */*.md) continue ;;
		*)
			need_test=1
			break
			;;
		esac
	done < <({
		git diff --name-only --diff-filter=ACMR "$base" 2>/dev/null || true
		git diff --name-only --diff-filter=ACMR 2>/dev/null || true
		git diff --cached --name-only --diff-filter=ACMR 2>/dev/null || true
	} | sort -u)
fi

if [[ $need_test -eq 0 ]]; then
	echo "pre-push: docs/agent-prose only, skipping ci test (RUN_CI_TEST=1 to force)"
	exit 0
fi

echo "pre-push: running ci test (SKIP_CI_TEST=1 to skip)…"
exec "$CI" test
