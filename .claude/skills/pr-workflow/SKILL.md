---
name: pr-workflow
description: Branch, worktree, PR, and merge mechanics for this repo. Use when starting a change, opening or updating a PR, when a PR is BEHIND or will not merge, when deciding whether to auto-merge, or when the user says "ship this PR", "merge it", "why won't this merge", or asks about worktrees.
---

# PR workflow

**Never commit to main.** Every change goes through a worktree and a PR.

## Start

The main checkout at `~/repos/homelab` auto-fetches every 60s, so working in it
directly is unsafe. Always branch into a worktree:

```bash
git -C ~/repos/homelab worktree add -b feat/my-feature /tmp/claude-worktrees/my-feature origin/main
```

Then work in `/tmp/claude-worktrees/my-feature`, run `ci` until green, commit
with Conventional Commits, push, open the PR.

Run `ci` (or at least `ci test`) **before** pushing. PR Workflows should be
mostly cache-hit, not your first test run. A pre-push hook runs `ci test` when
installed (`pre-commit install --hook-type pre-push`); `SKIP_CI_TEST=1` skips it
for docs-only changes.

## Before you push again

Check `gh pr view --json state` first. Never push to a merged branch: start a new
worktree instead.

## Merging

This repo allows **rebase merges only**. Squash and merge commits are disabled,
so always `gh pr merge --rebase` (or `--auto --rebase`).

**Required checks are strict: the branch must be up to date with main.** Any
other PR merging puts every open PR into `BEHIND`, where auto-merge will not
fire. Fix it with `gh pr update-branch <number> --rebase` and let CI re-run.

This is deliberate, not friction to route around. The re-run makes the
missed-chart-bump guard re-check against post-merge main, and that is the only
point where a rebase-merge version collision is detectable: two PRs claiming the
same chart version means the loser's bump is silently dropped. If
`mergeStateStatus` is `BEHIND`, update the branch. Never try to bypass the strict
check.

## Auto-merge

Small focused fixes (a one-line config change, a typo) can go straight to
`gh pr merge --auto --rebase`. Having enabled it, follow through rather than
walking away:

1. Poll `gh pr view <number> --json state,mergeStateStatus` until it merges.
2. Poll the rollout to confirm the fix is actually live.

If auto-merge fails with "Pull request is in clean status", the PR is already
green: merge directly with `gh pr merge --rebase`.

Use background Bash or `Monitor` for CI waits. Sleep-chained polling is blocked.

## Chart bumps

Any PR whose code must deploy needs the chart bump in the **same** PR:

```bash
bazel/tools/git/bump-chart.sh projects/<service>
```

It moves `chart/Chart.yaml` `version` and `deploy/application.yaml`
`targetRevision` together, numbering from the origin/main tip so concurrent
sessions cannot pick the same version. Without it the merge fails the `Push
images` action with the exact fix command.

## Issues

Outstanding work lives in GitHub Issues, never in a committed plan file. Title
them `<area>: <summary>`, label `agent-ready` when autonomously pickable, and
append `, ADR <cat>/<NNN>` when the work came out of an ADR. Closing the issue is
how "shipped" is recorded.

Multi-part work gets a parent tracking issue with sub-issues:

```bash
gh api repos/jomcgi/homelab/issues/<parent>/sub_issues -F sub_issue_id=<childDatabaseId>
```

Note `-F` for that integer field. `-f` sends a string and returns 422.

For full feature lifecycle and review gates, use the `ship` skill.
