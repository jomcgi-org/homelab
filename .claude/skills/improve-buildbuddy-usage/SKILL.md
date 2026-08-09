---
name: improve-buildbuddy-usage
description: Measure and reduce the bytes this repo pushes through BuildBuddy's cache. Use when asked to cut BuildBuddy usage, when BuildBuddy flags upload/download volume, when checking progress against the reduction target, or when the user says "/improve-buildbuddy-usage".
---

# Improve BuildBuddy usage

BuildBuddy hosts this repo's remote cache, remote execution, and Workflows on
their free tier, and in July 2026 they asked us to reduce what the Workflows
runners upload and download. This skill measures that traffic, aims the next
change at the largest source, and tracks the result across cycles.

**Target: cut per-day download bytes by 50%.**

| | Value |
|---|---|
| Baseline (7 days to 2026-07-28) | 1.502 TB/day download |
| Target | 0.751 TB/day download |
| Upload | 7.1 GB/day, 0.5% of the problem |

Upload is a rounding error. **Optimise downloads.** Ignore any change that only
helps upload unless it is free.

## Measure first

Never propose a fix from the shape of the config. The traffic is wildly
non-uniform and intuition is usually wrong here. Requires `BUILDBUDDY_API_KEY`
in the environment.

```bash
python3 bazel/tools/buildbuddy/bb_usage.py trend --days 14     # per-day totals, fast
python3 bazel/tools/buildbuddy/bb_usage.py outliers --days 7   # the tail, with URLs
python3 bazel/tools/buildbuddy/bb_usage.py snapshot --days 7   # full measure, writes a snapshot
python3 bazel/tools/buildbuddy/bb_usage.py report              # newest vs baseline, offline
```

`snapshot` writes `bazel/tools/buildbuddy/snapshots/<date>.json` and prints the
comparison against the earliest snapshot. Those files are committed on purpose:
BuildBuddy's free-tier invocation retention is finite, so once the baseline
window ages out of their database an uncommitted baseline is gone for good.

The tool reads two BuildBuddy endpoints that are not in the public API docs:
`SearchInvocation` for per-invocation `cacheStats`, and `GetTrend` for
aggregates. The group ID is discovered at runtime from a recent commit's
invocations, never hardcoded, because this repo is public.

## Aim at the tail, not the average

This is the single most important fact about this workload, and it is why
flag-shaving across the board is the wrong instinct:

```
p50 2 B      p90 1.7 GB      p99 18.6 GB      max 392.6 GB
top  1% of invocations =  39% of all downloads
top  5% of invocations =  79%
top 10% of invocations =  92%
```

The median invocation downloads essentially nothing. A few hundred invocations
a week download hundreds of gigabytes each. **The 50% target is reachable by
fixing the tail alone, and is not reachable by shaving the median.**

So the first move in any cycle is `outliers`, then open the worst invocation in
the BuildBuddy UI and find out what that specific build did differently. A
change that cuts the average 10% is worth less than one that stops a single
392 GB invocation from recurring.

## Where the bytes are

**Stale as of 2026-08-09**: this is the 2026-07-28 baseline split, kept for
comparison. `Visual regression`, `Buck2 rules` and `BDD future features` no
longer run, and the `Push images` split has changed. Re-measure before using it.

From the baseline snapshot, by source (7 day totals):

| Share | Source |
|---|---|
| 24.3% | `CI test //...` (the Test action's bazel run) |
| 22.7% | `Push images` runner |
| 19.7% | `Test` runner |
| 13.1% | `CI run //bazel/images:push_all` |
| 8.8% | `HOSTED_BAZEL remote test //...` (local `ci test`) |
| 4.2% | `Format check` runner |

Two roles matter. `CI` is the bazel client inside a workflow. `CI_RUNNER` is the
BuildBuddy runner wrapping it. They are **not** double counted: on a warm
recycled runner the `CI_RUNNER` invocation reports ~0 bytes, and its traffic
spikes only on some runs. Runner traffic is therefore about runner cold starts,
not about the bazel graph.

`ci test` runs `bazel test //... --config=ci` on a hosted worker, so it shares
every `--config=ci` flag with PR CI. Any flag fix lands on both lanes at once.

## What the bytes actually are (settled 2026-08-09, do not relitigate)

Two findings reframe everything above, and both are measured, not inferred.

**`cacheStats.totalDownloadSizeBytes` is not the bazel client's download.** The
client-side scorecard for a 299 GB invocation totals **1.8 MB** of CAS reads.
The figure aggregates *remote executors fetching action inputs*. So the metric
tracks `actions executed remotely x input tree size`, and every client-download
flag (`--remote_download_minimal`, `--experimental_fetch_all_coverage_outputs`,
`--remote_download_outputs=all`) is aimed at the wrong layer.

**`CI_RUNNER` is 47.7% of all traffic and 99% of it is cold starts.** A warm
runner reports under 100 MB; a cold one restores a VM snapshot from the cache
and averages 1.6 to 5.5 GB. Cold rate tracks *workspace size*, not run count:
`Format check`, `Test` and `Push images` all ran ~747 times with cold rates of
20%, 56%, 62%. A bigger `output_base` is likelier to be evicted locally *and*
costs more to restore. Each action has its own runner pool, so the repo keeps
several near-identical workspaces warm in parallel.

Corollary that keeps catching people: **an early-exit guard cannot save a cold
start.** The runner spins up, restores its snapshot, and only then runs step one.
`Format check`'s `ci-format-bot` author guard and `Buck2 rules`' `git diff` gate
both prove this: they exit in 1 to 5 seconds and still cost GBs.

## Refuted, do not retry

- **`--remote_local_fallback` is not the cause of the tail.** A 299 GB
  invocation ran **4** local actions out of 761 processes; a 360 GB one ran 57
  of 15,225. Mass local fallback would show thousands. Leave the flag in.
- **`--experimental_fetch_all_coverage_outputs` and Visual regression's
  `--remote_download_outputs=all`** were levers 4 and 5. Both are client-side,
  so both are rounding errors. Visual regression is gone anyway (#4588).
- **The `ci-format-bot` auto-commit is not worth excluding.** Its commits
  triggered 166 GB over 7 days, but 108.8 GB of that is `HOSTED_BAZEL` (local
  `ci test` on bot-authored commits), leaving ~0.5% on the CI side. BuildBuddy
  has no author, commit-message or path trigger filter, so there is no
  mechanism regardless.
- **Auto-cancellation is already on.** `allow_concurrent_runs` defaults to
  `false`. Superseded runs still cost their cold start, which is paid at
  spin-up before cancellation can land. Nothing to tune.

## Landed

| PR | change | measured |
|----|--------|----------|
| #4586 | PR branches build images instead of `bazel run push_all` | `CI run push_all` 1.8 GB -> 155 MB per run, 12x |
| #4587 | disabled `Buck2 rules` + `BDD future features` | 1,012 spin-ups, 558 GB/wk |
| #4588 | removed the visual regression suite | ~176 GB/wk |

`bazel run` stages every command's runfiles on the runner *before any command
executes*, which is why #4586 mattered: a 99% action-cache-hit push still
dragged all ~24 images out of CAS.

## Candidate levers

Ranked by expected bytes saved. Everything here is a **hypothesis that must be
confirmed against a real invocation** before you act on it.

1. **Collapse the remaining actions into one runner per push.** The largest
   lever left. `Test` (2.1 TB, 56% cold) and `Push images` (2.5 TB, 62% cold)
   keep separate, near-identical `output_base`s warm. One shared workspace is
   touched more often and has a smaller total warm footprint. Note the trap:
   required status checks are matched by exact name, so deleting an action
   without editing the ruleset blocks every PR, and the deleting PR cannot merge
   because it removes the checks required on itself. Flip the ruleset while the
   PR is open and its new check is green.
2. **Push only the images whose content changed, on main.** Worth ~2% now that
   PRs no longer push, and it is the only lever here that can break a deploy by
   skipping a push that was needed. Low priority.
3. **Fewer redundant pushes.** 1,396 runs in 7 days were superseded within 10
   minutes by another run of the same action on the same branch. The strict
   "up to date with main" rule forces `update-branch` on every open PR whenever
   anything merges. This is policy, not config.

## Running a cycle

1. `snapshot --days 7`, then `outliers --days 7`. Read the concentration block.
2. Pick **one** lever. Open the specific invocations that motivate it and
   confirm the mechanism before writing any code.
3. Land it as a normal PR (`pr-workflow`). Chart bumps still apply if a
   deployed service changes.
4. Wait at least 7 days so the window is not half old behaviour, then
   `snapshot --days 7` again and commit the new snapshot in the same PR as any
   follow-up. `report` prints progress toward the 50% and the per-source movers
   that explain it.
5. If a lever moved nothing, say so in the PR and record it here so the next
   cycle does not retry it.

## Guardrails

- **Correctness beats bytes.** A change that makes CI flaky, non-hermetic, or
  slower to diagnose is not worth any saving. `--remote_local_fallback` and the
  eviction retries exist because builds were failing without them.
- **Never disable BES.** `--bes_backend` is the only observability into CI, and
  it is a trivial part of the traffic. Same for the invocation links CI posts.
- **Do not add `common:ci --stamp` back** while chasing cache hits. See the
  standing comment in `.bazelrc`.
- **Watch for the truncation warning.** If a snapshot hits
  `--max-invocations`, the report says the numbers are a floor and the
  comparison against the baseline is not valid.
- Report the split by role, not just the total. Cutting local `ci test`
  traffic is real but it is not what BuildBuddy wrote to us about.
