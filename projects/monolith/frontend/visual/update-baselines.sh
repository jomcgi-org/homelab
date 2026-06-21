#!/usr/bin/env bash
# update-baselines.sh: copy the current captures over the committed baselines
# and push the result back to the PR branch as a bot. Triggered by the CI action
# only when the head commit message contains [update-baselines].
#
# Push ref + auth mirror ci-format-bot (buildbuddy.yaml) and the chart-version
# push (bazel/helm/push.sh.tpl): the repo has NO BUILDBUDDY_GIT_BRANCH env var
# (it appears nowhere in the tree), so the established convention is to derive
# the branch from `git rev-parse --abbrev-ref HEAD` and authenticate the push by
# rewriting the origin remote with the GHCR_TOKEN x-access-token credential.
set -euo pipefail

REPO_ROOT="$(git rev-parse --show-toplevel)"
VDIR="$REPO_ROOT/projects/monolith/frontend/visual"

cp "$VDIR"/out/*.png "$VDIR/baseline/"
cd "$REPO_ROOT"

# Consume the one-shot sentinel so the reseed does not repeat on later runs
# (and so this commit always has a staged change to make even when the pixels
# are unchanged).
git rm -q --ignore-unmatch "$VDIR/.reseed-baselines"

# Stage FIRST, then check the index. The seeded baselines are new untracked
# files, and `git diff --quiet` (working tree vs index) ignores untracked files,
# so it would falsely report "already current" on the initial seed. `git diff
# --cached --quiet` checks staged changes, which includes additions.
git add "$VDIR/baseline"
if git diff --cached --quiet; then
	echo "Baselines already current."
	exit 0
fi

git config user.name "visual-baseline-bot"
git config user.email "visual-baseline-bot@users.noreply.github.com"

# Authenticate the push the same way ci-format-bot does.
if [ -n "${GHCR_TOKEN:-}" ]; then
	git remote set-url origin "https://x-access-token:${GHCR_TOKEN}@github.com/jomcgi/homelab.git"
fi

CURRENT_BRANCH="$(git rev-parse --abbrev-ref HEAD 2>/dev/null || echo "unknown")"

git commit -m "chore(visual): update visual baselines [skip ci]"
git push origin "HEAD:${CURRENT_BRANCH}"
echo "Baselines updated and pushed to ${CURRENT_BRANCH}"
