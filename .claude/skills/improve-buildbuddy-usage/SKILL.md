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
| Latest (1.5 days to 2026-08-10, post-rewrite) | 1.67 TB/day download |
| Upload | 7.1 GB/day, 0.5% of the problem |

The latest figure is not a failure of the landed work, and it is not comparable
to the baseline either: the four reduction commits landed on 2026-08-09 and the
window still contains the actions they removed. See "Where the bytes are".

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

**Both committed snapshots predate the 2026-08-09 rewrite.** Every reduction
commit (`d2bd5b032`, `524430f02`, `b671019ed`, `d6b4041c6`) landed that day, so
`report` compares two pre-change windows and prints 0% progress no matter what
happened. Do not read that as "nothing worked". A snapshot is only clean from
**2026-08-16** onward; before then measure with `snapshot --days N --no-write`
over a window that starts after the change.

Current split, measured 2026-08-10 over the 1.5 days since the rewrite
(1.67 TB/day, still 2.2x the target):

| Share | GB/day | Source |
|---|---|---|
| 35.8% | 589 | `CI test //...` |
| 29.6% | 488 | `CI_RUNNER deploy` |
| 14.5% | 240 | `HOSTED_BAZEL remote test //...` (local `ci test`) |
| 7.9% | 130 | `CI_RUNNER pr-checks` |
| ~11% | ~180 | the four removed actions, still draining out of the window |

The 2026-07-28 baseline split is in `snapshots/2026-07-28.json` if you need it.

Two roles matter. `CI` is the bazel client inside a workflow. `CI_RUNNER` is the
BuildBuddy runner wrapping it. They are **not** double counted, and this was
re-confirmed under the new layout: invocation `6273779d` reports 61.4 GB while
its six child bazel invocations sum to 8.0 GB. Runner traffic is its own thing,
not a rollup of the bazel graph.

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

**`CI_RUNNER` is roughly half of all traffic and it is bimodal.** A warm runner
reports under 200 MB; a cold one restores a VM snapshot from the cache. Each
action has its own runner pool, so the repo keeps several near-identical
workspaces warm in parallel.

**Cold-start unit cost is additive in workspace content, and that is the number
that matters.** Same-window measurement on 2026-08-10, cold = >1 GB:

| action | runs | cold% | cold unit | cost per push |
|---|---|---|---|---|
| old `Test` | 69 | 30% | 8.2 GB | 2.5 GB |
| old `Push images` | 69 | 43% | 10.2 GB | 4.4 GB |
| old `Format check` | 69 | 16% | 5.8 GB | 0.9 GB |
| **old three combined** | | | | **7.8 GB** |
| new `pr-checks` | 125 | 7% | 20.9 GB | **1.5 GB** |
| new `deploy` | 57 | 39% | 33.1 GB | **12.9 GB** |

8.2 + 10.2 + 5.8 = 24.2 GB against `pr-checks`' measured 20.9 GB: one collapsed
workspace costs about what its parts cost separately. **So collapsing wins by
cutting the NUMBER of restores per push, not by keeping anything warmer.** The
1.6 to 5.5 GB figure quoted before the rewrite is dead; units are 21 to 33 GB
now.

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
- **Running an action more often does not keep it warm.** Cold rate is flat
  against the gap since that action's previous run: on 2026-08-10 `deploy` was
  29 to 43% cold in every gap bucket from under 5 minutes to over an hour, and
  `pr-checks` was 0 to 10% cold in all of them, including >60m. So "merge the
  pools so the workspace gets touched more" is not a mechanism. Attack the
  snapshot's SIZE, or the number of restores per push, not its frequency.

## Landed

| PR | change | measured |
|----|--------|----------|
| #4586 | PR branches build images instead of `bazel run push_all` | `CI run push_all` 1.8 GB -> 155 MB per run, 12x |
| #4587 | disabled `Buck2 rules` + `BDD future features` | 1,012 spin-ups, 558 GB/wk |
| #4588 | removed the visual regression suite | ~176 GB/wk |
| `d6b4041c6` | collapsed four actions into `pr-checks` + `deploy` | **split result.** `pr-checks` 7.8 -> 1.5 GB per push, 5x. `deploy` 7.8 -> 12.9 GB per push, a regression: it is a fourth workspace, the largest, and main-only |
| this PR | main pushes only images whose digest is not already published | pending; re-measure after 2026-08-16 |

`bazel run` stages every command's runfiles on the runner *before any command
executes*, which is why #4586 mattered: a 99% action-cache-hit push still
dragged all ~24 images out of CAS.

## Candidate levers

Ranked by expected bytes saved. Everything here is a **hypothesis that must be
confirmed against a real invocation** before you act on it.

1. **`CI test //...` at 589 GB/day is now the largest source.** Untouched so far
   and the only remaining item bigger than `deploy`. It is remote executors
   fetching action inputs, so the lever is the build graph (input tree size,
   how many actions miss the cache), not any client flag. Start by finding what
   the 70+ GB `test //...` invocations do that the median one does not.
2. **Shrink `deploy`'s snapshot further.** This PR removes the image runfiles
   staging. If `deploy`'s cold unit does not fall from 33.1 GB toward
   `pr-checks`' 20.9 GB, the extra 12 GB is something else and worth finding.
3. **`HOSTED_BAZEL` at 240 GB/day is local `ci test`.** Real bytes, but not what
   BuildBuddy wrote to us about. Report it separately, never fold it into the
   CI number.
4. **Fewer redundant pushes.** 1,396 runs in 7 days were superseded within 10
   minutes by another run of the same action on the same branch. The strict
   "up to date with main" rule forces `update-branch` on every open PR whenever
   anything merges. This is policy, not config. Note that a superseded run still
   pays its cold start, so cancellation does not help.

Collapsing actions is **done** (`d6b4041c6`), with the split result recorded
above. If you revisit it, the trap is unchanged: required status checks are
matched by exact name, so deleting an action without editing the ruleset blocks
every PR, and the deleting PR cannot merge because it removes the checks
required on itself. Flip the ruleset while the PR is open and its new check is
green.

## Running a cycle

1. `snapshot --days 7`, then `outliers --days 7`. Read the concentration block.
   If a change landed inside that 7 days, the window is half old behaviour and
   both the split and the comparison lie. Measure the post-change window with
   `snapshot --days <N> --no-write` instead, and do not commit a snapshot until
   7 clean days have passed. `report` reads committed snapshots only, so it will
   keep printing the stale comparison until then; that is expected, not a bug.
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
