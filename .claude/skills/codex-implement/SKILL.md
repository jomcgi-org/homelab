---
name: codex-implement
description: >
  Dispatch implementation work to the Codex CLI (OpenAI subscription) via the
  deterministic wrapper bazel/tools/codex/dispatch.sh, so the implementation
  bulk drains the OpenAI quota instead of the Claude weekly limit. Use when
  executing implementation tasks that are locally verifiable (boilerplate,
  config plumbing, mechanical edits, clearly-spec'd functions), when a plan's
  implementer subagents would otherwise go to Sonnet, or when the user says
  "use codex", "dispatch to codex", or "/codex-implement". Also defines the
  mandatory quota-exhaustion handling: Discord-notify Joe once and fall back
  to Sonnet.
---

# Codex Implement

Dispatch well-specified implementation tasks to Codex CLI workers instead of
Sonnet subagents. Same routing philosophy as CLAUDE.md Model Routing: fast
hands on cheaply-verified work, Opus eyes on the diff. Codex workers bill the
OpenAI subscription, preserving the Claude weekly budget for judgment work.

## When to Use

- Implementation-bulk tasks that local hooks and renders can verify
  (`fast-format.sh`, `helm template`, type checks): the Sonnet slot
- Parallel fan-out of independent implementation tasks in a plan
- NOT for CI-only-verifiable work (Helm value plumbing several levels deep,
  Bazel/apko, RBAC verbs, migration ordering): keep that on Opus/Fable
- NOT for review: the safety-net reviewer stays Opus (never downgrade it)

## How to Dispatch

Always through the wrapper, never raw `codex exec` (the wrapper pins the
sandbox, appends repo guardrails, and classifies quota exhaustion):

```bash
bazel/tools/codex/dispatch.sh <tier> <workdir> "<full task spec>"
# or pipe a long spec:
bazel/tools/codex/dispatch.sh terra /tmp/claude-worktrees/my-task - <<'SPEC'
...multi-line spec...
SPEC
```

Tiers:

| Tier       | Model         | Effort | Use for                                   |
| ---------- | ------------- | ------ | ----------------------------------------- |
| `luna`     | gpt-5.6-luna  | medium | Mechanical edits, boilerplate, renames    |
| `terra`    | gpt-5.6-terra | high   | Standard implementation bulk (default)    |
| `frontier` | gpt-5.5       | high   | Hardest specs; cross-vendor second opinion |

Rules:

1. **One worktree per worker.** Workers write files only; they cannot commit,
   push, or reach the network (workspace-write sandbox). Parallel workers
   must not share a worktree.
2. **Write the full spec up front**: files to touch, acceptance criteria,
   patterns to follow (point at an existing file to imitate). Codex has no
   conversation memory across dispatches.
3. **Fan out in parallel** for independent tasks: run several dispatches with
   `run_in_background: true` Bash calls in one turn.
4. **Review before committing.** The dispatching (Opus) agent reads the diff,
   runs `bazel/tools/format/fast-format.sh`, and commits with Conventional
   Commits. Test execution stays deferred to CI on the pushed branch.

## Quota Exhaustion (exit code 42)

Exit 42 plus a `CODEX_QUOTA_EXHAUSTED` line on stderr means the OpenAI
subscription is out of quota or rate-limited. The dispatching agent MUST:

1. Send exactly ONE Discord notification via
   `monolith-monolith-agent-notify` (level `warn`), e.g.
   "Codex quota exhausted mid-plan (task N of M); falling back to Sonnet
   implementers." Main-loop agent only, per the single-voice rule; a
   subagent that sees exit 42 reports it to the dispatcher instead.
2. Fall back to Sonnet subagents (Agent tool, `model: sonnet`) for the
   remaining implementation tasks. Do not retry codex in a loop.
3. Not re-notify for subsequent 42s in the same session.

Any other non-zero exit is an ordinary worker failure: read the transcript
the wrapper printed, fix the spec or fall back to Sonnet for that one task.
No Discord notification.

## Preflight (once per session, optional)

If codex has not been used yet this session and the plan leans on it:

```bash
codex login status   # expect "Logged in using ChatGPT"
```

If not logged in, tell Joe to run `! codex login` rather than attempting it
headlessly.
