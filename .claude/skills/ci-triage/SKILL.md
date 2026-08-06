---
name: ci-triage
description: Diagnose a red CI run in this repo. Use when a BuildBuddy Workflows check fails, a PR is red, `ci test` fails, or the user asks why the build broke, what the test failure means, or whether a failure is flaky.
---

# CI triage

CI is **BuildBuddy Workflows** (`buildbuddy.yaml`), not GitHub Actions, and every
build runs remotely on RBE. Two actions run on each push and PR:

- **Format check**: standalone formatters plus gazelle, auto-committing fixes to
  PR branches as `ci-format-bot`.
- **Test and push**: `bazel test //...`, plus image pushes on main.

## Quote before hypothesizing

When CI is red, the first action is to fetch the actual log:

1. `mcp__buildbuddy__get_invocation` with the `commitSha` selector, which skips
   the invocation-ID lookup.
2. `get_target` to find the failing targets.
3. `get_log` for the trace.

**Quote the real assertion error or exception verbatim before proposing a
cause.** Do not raise infrastructure (BuildBuddy outages, flaky runners, RBE
hiccups) until a real test failure has been ruled out. Claude has hallucinated
infra failures in this repo before, and one wrong "it's just flaky" costs several
wasted iterations.

## Green that proves nothing

`ci test` has exited 0 without testing anything (#4118): the remote runner can
fail setup, and a fully cached run re-executes nothing. Judge a run by its
log, not its exit code: find the `Executed N out of M tests` summary and grep
the full output for `FAILED`. Never pipe `ci` output through `tail` or
`head`; a hook blocks it because truncated reads are how false greens happen.

## Retrigger discipline

Never retrigger a red run before reading the failing log and naming the
failure. One known shape: a red Test check whose bazel summary looks green is
the Elixir mix test genrule failing inside the build (ordering flake, #4391).
Do not blind-retrigger it; if the same failure reappears, treat it as new
evidence, not the same flake.

## Reproduce locally

`ci test` is 1:1 with the Workflows Test action:

```bash
bb remote --os=linux --arch=amd64 \
  test //... --config=ci --deleted_packages=bazel/tools/python --test_tag_filters=-external,-future
```

Same argv means a shared action cache, so a green `ci test` should make the PR
Test check mostly cache-hit. Bare `bazel` / `bazelisk` on the Mac will not
reproduce anything useful: there are no darwin workflow executors and the
platforms are wrong.

## Failures with a known shape

- **A test asserting on a number you changed.** Bumping a TTL, timeout,
  `max_tokens`, or retry count breaks assertions that hardcode the old value.
  Grep the test tree for the old value and fix the assertions in the same commit,
  or the failure looks like flakiness and takes a second push.
- **Semgrep rules.** `no-sync-session-in-async-def`, `no-session-in-to-thread`,
  `session-add-in-loop`, `no-hardcoded-k8s-service-url`, and
  `no-hardcoded-image-digest` each encode a real production incident. See
  `projects/monolith/CLAUDE.md` for the first three. Fix the code, do not silence
  the rule.
- **`Push images` failing on merge.** The change needed a chart bump that the PR
  did not carry. The action prints the exact `bazel/tools/git/bump-chart.sh`
  command.
- **Generated files drifting.** `ci regen` runs the committed generators (home
  cluster kustomization, doc manifests, routes, orchestrator bundle). If CI
  auto-commits a regen you did not run, that is the format bot, not a failure.
