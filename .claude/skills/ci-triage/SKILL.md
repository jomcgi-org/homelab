---
name: ci-triage
description: Diagnose a red CI run in this repo. Use when a BuildBuddy Workflows check fails, a PR is red, `ci test` fails, or the user asks why the build broke, what the test failure means, or whether a failure is flaky.
---

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

When CI is red, the first action is to fetch the actual log:

1. `mcp__buildbuddy__get_invocation` with the `commitSha` selector, which skips
   the invocation-ID lookup.
2. `get_target` to find the failing targets.
3. `get_log` for the trace.

**Quote the real assertion error or exception verbatim before proposing a
cause.** Do not raise infrastructure (BuildBuddy outages, flaky runners, RBE
hiccups) until a real test failure has been ruled out: an unverified "it's just
flaky" costs several wasted iterations.

## Green that proves nothing

`ci test` has exited 0 without testing anything (#4118): the remote runner can
fail setup, and a fully cached run re-executes nothing. Judge a run by its
log, not its exit code: find the `Executed N out of M tests` summary and grep
the full output for `FAILED`. Never pipe `ci` output straight into `tail`,
`head`, or `grep`; a hook blocks it. `| tee` the run to a file first, then
grep the saved log.

## Retrigger discipline

Never retrigger a red run before reading the failing log and naming the
failure. One known shape: a red `pr-checks` run whose bazel summary looks green is
the Elixir mix test genrule failing inside the build (ordering flake, #4391).
Do not blind-retrigger it; if the same failure reappears, treat it as new
evidence, not the same flake.

## Reproduce locally

`ci test` runs the affected subset on one hosted Linux runner using the same
test flags as PR CI. Use the explicit target escape hatch to reproduce the full
merge-queue test:

```bash
ci test -- //...
```

The default affected run and explicit full run both use the hosted Linux runner
and PR Test flags, so their test actions share the remote cache. Bare `bazel` or
`bazelisk` on the Mac will not reproduce anything useful: there are no darwin
workflow executors and the platforms are wrong.

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
