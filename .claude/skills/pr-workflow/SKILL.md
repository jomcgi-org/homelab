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

Run `ci` **before** pushing so the PR run is mostly cache-hit. The pre-push
hook no longer runs `ci test` by default: PR CI and the queue candidate both
test the change after push, so a third run was pure cost. `RUN_CI_TEST=1 git
push` runs it for a change you want proven before it is public.

## Before you push again

Check `gh pr view --json state` first. Never push to a merged branch: start a new
worktree instead.

If CI's format bot pushed a `style: auto-format` commit, your next push is
rejected as non-fast-forward, and wrappers have swallowed that rejection
before: fetch and rebase first. After every push, confirm the remote took it:

```bash
git rev-parse HEAD
gh pr view <number> --json headRefOid -q .headRefOid
```

If they differ, re-query once (the PR object can lag the push; an immediate
retry usually settles it). Still different with no push error: the push did
not land. Fetch, rebase, and push again.

## Merging

This repo allows **rebase merges only**. Squash and merge commits are disabled,
so always `gh pr merge --rebase` (or `--auto --rebase`).

**Merges go through the GitHub merge queue.** `gh pr merge --auto --rebase`
("merge when ready") enqueues the PR once it is green and reviewed; the queue
rebases it onto current main, runs `pr-checks` on the candidate (a push to a
`gh-readonly-queue/main/pr-<n>-<sha>` branch), and merges. **Never
`gh pr update-branch` or rebase a PR just because main moved**: the queue does
that, and a local rebase only changes the head and restarts CI.

`mergeStateStatus: BEHIND` is no longer a blocker; the required check is not
strict. The queue is what guarantees a PR was tested against **current** main,
so a semantic conflict between two individually green PRs cannot break main.

Each merge still moves main twice (the merge, then the chart write-back), and
the queue re-tests the candidates behind it on each move. That is the queue's
churn, not yours. A red queue run ejects the PR: re-enqueue with the same
`gh pr merge --auto --rebase` after checking whether the failure was the flaky
Elixir suite (#4828) or real.

This rationale used to be about chart versions: the re-run let the
missed-chart-bump guard catch two PRs claiming the same version. That reason is
gone, because PRs no longer carry a version at all (see below). The
tested-against-current-main reason is the live one.

## Auto-merge

Small focused fixes (a one-line config change, a typo) can go straight to
`gh pr merge --auto --rebase`. Having enabled it, follow through rather than
walking away:

1. Poll `gh pr view <number> --json state,mergeStateStatus` until it merges.
2. Poll the rollout to confirm the fix is actually live.

`gh pr merge --rebase` without `--auto` also enqueues rather than merging
directly; there is no bypass and none should be used.

Use background Bash or `Monitor` for CI waits. Sleep-chained polling is blocked.

## Chart bumps

**Do not bump a chart version in a PR.** Since ADR platform/009 decision 1 the
version is an output of merging, not an input on the branch: a PR that edits
`chart/Chart.yaml` `version:` or `deploy/application.yaml` `targetRevision:` is
fighting the publish job, and two PRs that both edit those lines conflict on
rebase for no reason.

After the merge, main's publish computes the next version from the merged
history, pushes the OCI chart, and commits both lines back as
`chart-version-bot`. So a deploying change lands in two commits: yours, then the
write-back. `bump-chart.sh` is retired.

The practical consequence for "is it live yet": the deploy starts when the
write-back commit lands, not when your PR merges. If nothing has rolled out,
check for that commit on main before assuming the deploy failed.

## Done means live

Merged is not done for a change that must deploy. Done means all four:

1. `gh pr view <number> --json state` says `MERGED`.
2. The ArgoCD app reports Synced and Healthy
   (`kubectl get application -n argocd <app>`, read-only). A moved
   `targetRevision` only proves desire, not deployment.
3. The workload actually rolled: the pod name changed and its image matches
   the bumped chart.
4. The service answers: curl its endpoint or check its probe.

If any step stalls, open `docs/runbooks/argocd-outofsync.md`. Never report
success on a subset.

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
