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

## Candidate levers

Ranked by expected bytes saved. Everything below the first line is a
**hypothesis that must be confirmed against a real invocation** before you act
on it. Confirm by opening the outlier invocation and reading its timing and
cache scorecard, exactly like `ci-triage` insists on quoting a real failure
before naming a cause.

1. **Whatever causes the bimodal tail.** p50 of 2 bytes next to a max of
   392 GB is a switch flipping, not a gradual cost. Two config-level suspects,
   both currently enabled for `--config=ci`:
   - `common:ci --remote_local_fallback` (`bazel/tools/preset.bazelrc`). When a
     remote call fails, Bazel re-runs actions locally, and local execution has
     to materialize every input that `--remote_download_minimal` was avoiding.
     This fits the signature precisely.
   - `build:ci --experimental_remote_cache_eviction_retries=5`
     (`bazel/remote.bazelrc`). Retrying an evicted action refetches inputs.

   Neither is confirmed. Check a 200 GB+ invocation for local-fallback spawns
   or eviction retries before touching either, and note that
   `--remote_local_fallback` exists so a cache blip does not fail the build, so
   removing it trades bytes for red builds.

2. **`bazel run //bazel/images:push_all --config=ci --stamp`** (13.1% directly,
   plus most of the `Push images` runner's 22.7%). Two compounding problems:
   `bazel run` must materialize outputs on the runner, so image layers round
   trip CAS to runner to GHCR and `--remote_download_minimal` cannot help; and
   `--stamp` makes stamp-aware actions non-cacheable remotely, which the long
   comment in `.bazelrc` documents from PR#4038. Ask whether the image tag can
   come from a build setting instead of a stamp, and whether PRs need to push
   images at all or only need the missed-chart-bump guard.

3. **Five workflow actions fire on every PR push**, roughly 84 runs each per
   day. `Buck2 rules` (2.9%) and `BDD future features` (2.1%) mostly no-op but
   still pay runner cold-start traffic. An early exit on a `git diff` check
   saves the bazel traffic, though not the runner spin.

4. **`common --experimental_fetch_all_coverage_outputs`**
   (`bazel/tools/preset.bazelrc`). Its own comment says it downloads coverage
   files even when `--remote_download_minimal` would skip them on test cache
   hits. It is set globally rather than scoped away from `:ci`. Cheap to scope,
   but measure the delta rather than assuming it.

5. **`Visual regression` uses `--remote_download_outputs=all` twice**
   (1.0% plus 0.5%). Narrowing to `--remote_download_regex` for the PNGs only
   is a contained change.

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
