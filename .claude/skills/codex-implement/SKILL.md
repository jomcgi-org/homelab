---
name: codex-implement
description: >
  Dispatch implementation work to the Codex CLI (OpenAI subscription) via
  bazel/tools/codex/dispatch.sh so bulk work bills OpenAI instead of the Claude
  weekly limit. Default to Sol (`frontier` tier, on trial). Use when executing
  locally-verifiable tasks, when implementers would otherwise be Sonnet, or when
  the user says "use codex", "dispatch to codex", or "/codex-implement".
---

# Codex Implement

Dispatch well-specified implementation tasks to Codex CLI workers. House style:
**Sol (`frontier`) first**, on trial until 2026-08-28; Luna for trivially
mechanical bulk or quota pressure, Terra as the middle rung. Opus 5 reviews
diffs. Verification is **`ci`** (bb remote Test 1:1 with Workflows), not bare
Mac `bazel`.

## When to Use

- Implementation bulk that `ci lint` / local renders catch cheaply
- Parallel fan-out of independent tasks (one worktree each)
- NOT for CI-only-verifiable work (deep Helm, Bazel/apko, RBAC, migration
  ordering): keep that on **Opus 5**
- NOT for review: reviewer stays Opus (never downgrade)
- NOT for typing out a fix that review already wrote. If the exact diff is in
  hand and under ~20 lines, apply it directly in the main loop: the thinking
  is spent, and a dispatch round costs more than the typing.

## How to Dispatch

Prefer the **`implementer`** agent (`.claude/agents/implementer.md`): it has no
`Write` or `Edit` tool, so spawning it cannot silently turn into Claude writing
the code. Hand it the full spec and a worktree path; it runs the wrapper and
returns the diff. Drive the wrapper directly only when you need a tier or
invocation the agent does not cover.

Always through the wrapper, always in the background, always by the worktree's
own copy (relative paths have exit-127'd when cwd drifted):

```
Bash tool:
  command: "$WORKDIR"/bazel/tools/codex/dispatch.sh frontier "$WORKDIR" - <<'SPEC'
  ...full spec...
  SPEC
  run_in_background: true
```

Workers run minutes to over an hour (median ~2.5 min, p90 ~8 min, tail 80+
min). A foreground call dies at the shell timeout, kills the worker mid-run,
and leaves a partial diff that reads as a wrong implementation at review. The
full transcript lands in `<worktree>/.codex-dispatch/<stamp>.log`; stdout is
just the worker's final message.

| Tier           | Model         | Effort | Use for |
| -------------- | ------------- | ------ | ------- |
| **`frontier`** | gpt-5.6-sol   | high   | **Default, on trial until 2026-08-28.** Judged on correction rounds per PR, OpenAI quota burn, and exit-42 events |
| `luna`         | gpt-5.6-luna  | medium | Trivially mechanical bulk, or the fallback default if the trial ends badly |
| `terra`        | gpt-5.6-terra | high   | Middle rung; largely idle during the trial |

Rules:

1. **Default to Sol (`frontier`).** Do not drop to Luna or Terra to save quota
   without cause: the trial (#4913) is measuring Sol's one-shot rate, and mixed
   tiers muddy the numbers. There is no rung above `frontier`: if Sol fails the
   same spec after one batched respec, the task is under-specified or
   CI-only-verifiable, so fix the spec or keep that task on Opus as the
   exception.
2. **One worktree per worker, one worker per worktree.** The wrapper enforces
   the second half (exit 65) because concurrent workers interleave edits.
3. **Full spec up front** (see the template below).
4. **Fan out in parallel** for independent tasks.
5. **Opus reviews** the diff, runs **`ci`** (or `ci lint` + `ci test`), then
   commits Conventional Commits. Do not skip `ci` and hope PR CI is the first
   signal.

## Spec Template

Repo conventions ship with the worktree in `AGENTS.md` (codex reads it
automatically), so do not restate them. What the spec must carry:

```markdown
# Task: <one line>

## Goal
<what changes and why, including the invariant the change must preserve;
for EmberVM control-plane work, state what would falsify the design>

## Files
<files to touch, and the files whose patterns to imitate>

## Acceptance
<grep-able assertions the implementer verifies mechanically:
"def handle_agent_thread_reply exists in chat/bot.py",
"py_test target bot_agent_thread_test in projects/monolith/BUILD">

## Out of scope
<what NOT to touch>
```

The Acceptance section is load-bearing: the implementer greps each line before
reporting success, which catches an incomplete diff at Haiku prices instead of
at Opus review.

## Correction Rounds

After a review or a red `ci`, **batch every finding into one respec and one
dispatch**. Single-finding dispatches have taken four rounds to fix one test;
the batched respec fixes the lot in one. Same rule for test failures: all of
them, one spec.

- Never re-dispatch a trimmed spec after a failure or kill; a shorter respec
  produces a worse diff.
- Before any re-dispatch, confirm the previous worker is dead and inspect
  `git status` for a partial diff to build on or reset.
- Exact fixes under ~20 lines that review already wrote: main loop, not a
  dispatch (see When to Use).

## Quota Exhaustion (exit code 42)

1. One Discord `monolith-monolith-agent-notify` (level `warn`, main-loop only).
2. Fall back to Sonnet implementers for remaining tasks.
3. Do not retry codex in a loop; do not re-notify in the same session.

## Preflight (optional)

```bash
codex login status   # expect "Logged in using ChatGPT"
```

If not logged in, ask Joe to run `codex login` (not headless).
