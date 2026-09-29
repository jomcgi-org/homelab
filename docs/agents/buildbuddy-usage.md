# BuildBuddy usage

BuildBuddy hosts this repo's remote cache, remote execution and Workflows on
their free tier, and asked us to reduce what the Workflows runners download.
This doc covers measuring that traffic, aiming a change at the largest source,
and tracking the result. The measurement history is in the knowledge graph
(search "buildbuddy").

**Target: halve per-day download bytes** against the 2026-07-28 baseline in
`bazel/tools/buildbuddy/snapshots/`. Run `trend` for the current figure before
claiming progress. Upload is a rounding error: optimise downloads.

## Measure first

Never propose a fix from the shape of the config: the traffic is non-uniform
and intuition is usually wrong. Requires `BUILDBUDDY_API_KEY`.

```bash
python3 bazel/tools/buildbuddy/bb_usage.py trend --days 14     # per-day totals, fast
python3 bazel/tools/buildbuddy/bb_usage.py outliers --days 7   # the tail, with URLs
python3 bazel/tools/buildbuddy/bb_usage.py snapshot --days 7   # full measure, writes a snapshot
python3 bazel/tools/buildbuddy/bb_usage.py report              # newest vs baseline, offline
```

`snapshot` writes `bazel/tools/buildbuddy/snapshots/<date>.json`. Commit those
files: free-tier invocation retention is finite, so an uncommitted baseline is
lost once it ages out. The tool reads `SearchInvocation` (per-invocation
`cacheStats`) and `GetTrend`, which are not in the public API docs, and
discovers the group ID at runtime because the repo is public. `--repo` defaults
to the git origin; pass `--repo ""` for a window that spans an org move.

## What the numbers mean

- **Downloads are heavy-tailed.** The median invocation downloads almost
  nothing and a few invocations dominate, so start every cycle with `outliers`
  and find out what the worst invocation did. Shaving the median cannot reach
  the target.
- **`cacheStats.totalDownloadSizeBytes` counts remote executors fetching
  action inputs**, not the bazel client's downloads, so client download flags
  are the wrong layer.
- **`CI_RUNNER` and `CI` are not double counted.** `CI_RUNNER` bytes are the
  runner's snapshot restore; `CI` bytes are the bazel graph inside it.
- **Attribute lanes by joining `parentRunId` to `runId`, never by role or
  branch.** A `CI test //...` on branch `main` is usually somebody's local
  `ci test`, whose parent is a `HOSTED_BAZEL remote run`.
- **Test download = remotely executed test actions x each one's runfiles
  tree.** Correlate against `casCacheHits`, not action count or cache misses.
  Commit size predicts nothing: look at what a commit invalidates. A multi-day
  gap in `trend` (cache eviction) can re-execute every test on its own.
- **A cold runner restores its workspace snapshot**, so the restore size is
  workspace content. `$HOME` on a runner is inside the workspace, and `disk:`
  in `buildbuddy.yaml` is a reservation, not content. An early-exit step
  cannot save a cold start, which is paid at spin-up.
- **Full-suite fallbacks are logged.** Read the runner's `affected-targets:
  fallback to //... because` line for the reason. Exit 2 with no diff to
  explain it means a label the query lexer cannot parse (`+` in a SvelteKit
  route was one): quote the label in `bazel/tools/ci/affected-targets.sh`.

To measure an action's input tree: `GetExecution` for the invocation gives
each action's `actionDigest`; download the Action blob via
`/file/download?bytestream_url=bytestream://remote.buildbuddy.io/blobs/<hash>/<size>&invocation_id=...`
(API key header), read `input_root_digest` (field 2), then call
`rpc/BuildBuddyService/GetTreeDirectorySizes` with `root_digest` and walk the
Directory protos top-down, descending only into subtrees over a threshold.
Build tool logs (critical path, remote versus local process counts) come from
`get_invocation` with `includeBuildToolLogs: true`; `SearchInvocation` omits
them.

## Refuted, do not retry

- `--remote_local_fallback` is not the cause of the tail; leave it in.
- Client download flags (`--remote_download_minimal`,
  `--experimental_fetch_all_coverage_outputs`, `--remote_download_outputs=all`)
  act on the wrong layer.
- Excluding `ci-format-bot` commits: little CI-side traffic, and BuildBuddy has
  no author, message or path trigger filter.
- Auto-cancellation is already on (`allow_concurrent_runs` defaults to
  `false`), and a superseded run still pays its cold start.
- Pushing more often keeps a runner warmer, but nothing here controls push
  frequency. Attack the snapshot's size instead.

`common:ci --noreuse_sandbox_directories` plus the EXIT trap that removes only
`output-base/sandbox/sandbox_stash` are deliberate (#5402): the flag stops new
stash, the trap reclaims what an earlier snapshot banked. Never remove the
whole `sandbox` tree, which holds live per-action sandboxes.

## Candidate levers

Each is a hypothesis to confirm against a real invocation first.

1. **Shrink the runner snapshot**, the largest source. What is left is
   `output-base/external` and `output-base/execroot`, both real. Before
   deleting anything, ask whether it is rebuilt every run or reused: reused
   content dropped here is re-fetched next run, which is download bytes again.
   Read the `WORKSPACE ...:` lines in a recent pr-checks log first.
2. **Narrow the deleted-file fallback.** A deleted file can be mapped to a
   label from the base ref's tree, which still has the BUILD files. Deleting a
   BUILD file must still fall back, and that rule already fires first.
3. **Narrow the BUILD-shaped fallback**, the riskiest lever: rdeps of the
   edited package is the honest set, and too narrow means a green PR that was
   never tested. Get a second opinion before touching it.

Removing or renaming a CI action has a trap: required status checks match by
exact name, so a PR that deletes one blocks every PR, including itself. Flip
the ruleset while the PR is open and its new check is green.

## Normalise before claiming a win

A falling daily total may just be a quiet week. Normalise by pushes (the count
of `pr-checks` runner invocations) and by merged PRs
(`gh pr list --state merged --search "merged:<from>..<to>"`, watch the 200
cap). Never normalise by commits on main (write-back commits and PR commit
counts distort it) or by runner spin-ups.

## Running a cycle

1. `snapshot --days 7`, then `outliers --days 7`. If a change landed inside
   the window, it is half old behaviour: measure the post-change window with
   `snapshot --days <N> --no-write`, and commit a snapshot only after 7 clean
   days. `report` reads committed snapshots only.
2. Pick **one** lever. Open the invocations that motivate it and confirm the
   mechanism before writing code.
3. Land it as a normal PR.
4. Wait at least 7 days, then `snapshot --days 7` and commit the snapshot with
   any follow-up. `report` prints progress and the per-source movers.
5. If a lever moved nothing, say so in the PR and add it to "Refuted" above.

## Guardrails

- Correctness beats bytes. A change that makes CI flaky, non-hermetic or
  slower to diagnose is not worth any saving; `--remote_local_fallback` and the
  eviction retries exist because builds failed without them.
- Never disable BES (`--bes_backend`) or the invocation links CI posts: they
  are the only observability into CI and a trivial part of the traffic.
- Do not add `common:ci --stamp` back; see the standing comment in `.bazelrc`.
- If a snapshot hits `--max-invocations`, the numbers are a floor and the
  comparison against the baseline is not valid.
- Report the split by role, not just the total: local `ci test` traffic is
  real but not what BuildBuddy asked about.
