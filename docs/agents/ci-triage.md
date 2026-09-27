# CI triage

CI is **BuildBuddy Workflows** (`buildbuddy.yaml`), not GitHub Actions, and every
build runs remotely on RBE. One action, `pr-checks`, runs on each push, PR and
merge-queue candidate, in stages:

- **Format**: standalone formatters plus gazelle, auto-committing fixes to PR
  branches as `ci-format-bot`.
- **Test**: affected targets on PR branches, the full `//...` on merge-queue
  candidates.
- **Publish** (main only): image pushes, chart publish, and the
  `chart-version-bot` write-back.

## Quote before hypothesizing

When CI is red, the first action is to fetch the actual log. With the
BuildBuddy MCP tools:

1. `get_invocation` with the `commitSha` selector, which skips the
   invocation-ID lookup.
2. `get_target` to find the failing targets.
3. `get_log` for the trace.

Without the BuildBuddy MCP (a cloud session, say), the workflow log for a
`pr-checks` run is readable without auth. Take the invocation ID from the
status `target_url` (`GET /repos/{owner}/{repo}/commits/{sha}/status`), then:

```bash
curl -sS -X POST -H "Content-Type: application/json" \
  https://jomcgi.buildbuddy.io/rpc/BuildBuddyService/GetEventLogChunk \
  -d '{"invocationId":"<id>","chunkId":"","minLines":500}' | jq -r .buffer | base64 -d
```

An empty `chunkId` returns the tail; walk back with `previousChunkId` (chunks
are `0000`, `0001`, ...) until the failure is on screen.

**Quote the real assertion error or exception verbatim before proposing a
cause.** Do not raise infrastructure (BuildBuddy outages, flaky runners, RBE
hiccups) until a real test failure has been ruled out.

A green exit proves nothing on its own: judge a run by its `Executed N out of
M tests` line (`AGENTS.md`, Commands).

## Retrigger discipline

Never retrigger a red run before reading the failing log and naming the
failure. A red `pr-checks` run whose bazel summary looks green is usually the
Elixir mix test genrule failing inside the build. A known flake that recurs is
still new evidence, not the same flake.

## Reproduce locally

`ci test` runs the affected subset on one hosted Linux runner using the same
test flags as PR CI. Use the explicit target escape hatch to reproduce the full
merge-queue test:

```bash
ci test -- //...
```

Both runs use the hosted Linux runner and PR test flags, so their test actions
share the remote cache.

## Failures with a known shape

- **A test asserting on a number you changed.** Bumping a TTL, timeout,
  `max_tokens`, or retry count breaks assertions that hardcode the old value.
  Grep the test tree for the old value and fix the assertions in the same commit,
  or the failure looks like flakiness and takes a second push.
- **The main-only publish stage of `pr-checks` failing after a merge.** PRs do
  not carry chart versions, so this is never a missed bump. Read the failing
  stage.
  - **write-back (`write-back-versions.sh`)**: the charts published but main
    does not reference them yet, so nothing deploys until it is resolved.
    Re-running the action is safe and idempotent.
  - **"published but the image digests DIFFER ... no higher version could be
    computed"**: the content changed under a published version and the
    escalation could not produce a new one. Do not re-run and hope; this means
    `chart-version.sh` returned the same version twice, so read it against that
    chart's directory.
  - anything else: a normal build failure.
- **Merged but never deployed, everything green.** Check whether the chart
  version actually moved: `git log --author=chart-version-bot -3 origin/main`.
  A merge whose images changed must produce a write-back. If it did not, compare
  the digests pinned in the published chart against what the run built, because
  "nothing to publish" and "failed to notice a change" print almost the same
  thing.
- **Generated files drifting.** `ci regen` runs the committed generators (home
  cluster kustomization, posts manifest, routes, orchestrator bundle). The
  repo-docs and public docs manifests are genrule outputs (#6446); a
  `repo-docs coverage: FAIL` from `validate-generate-scripts.sh` means a tracked
  doc sits in a package with no `repo_docs` filegroup, and its message says
  what to add. If CI auto-commits a regen you did not run, that is the format
  bot, not a failure.
