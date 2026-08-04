---
name: codex-implement
description: >
  Dispatch implementation work to the Codex CLI (OpenAI subscription) via
  bazel/tools/codex/dispatch.sh so bulk work bills OpenAI instead of the Claude
  weekly limit. Prefer Luna (most value per dollar). Use when executing
  locally-verifiable tasks, when implementers would otherwise be Sonnet, or when
  the user says "use codex", "dispatch to codex", or "/codex-implement".
---

# Codex Implement

Dispatch well-specified implementation tasks to Codex CLI workers. House style:
**Luna first**, Terra only when needed, Sol (`frontier`) almost never. Opus 5
reviews diffs. Verification is **`ci`** (bb remote Test 1:1 with Workflows), not
bare Mac `bazel`.

## When to Use

- Implementation bulk that `ci lint` / local renders catch cheaply
- Parallel fan-out of independent tasks (one worktree each)
- NOT for CI-only-verifiable work (deep Helm, Bazel/apko, RBAC, migration
  ordering): keep that on **Opus 5**
- NOT for review: reviewer stays Opus (never downgrade)

## How to Dispatch

Prefer the **`implementer`** agent (`.claude/agents/implementer.md`): it has no
`Write` or `Edit` tool, so spawning it cannot silently turn into Claude writing
the code. Hand it the full spec and a worktree path; it runs the wrapper below
and returns the diff. Drive the wrapper directly only when you need a tier or
invocation the agent does not cover.

Always through the wrapper:

```bash
bazel/tools/codex/dispatch.sh <tier> <workdir> "<full task spec>"
# or:
bazel/tools/codex/dispatch.sh luna /tmp/claude-worktrees/my-task - <<'SPEC'
...multi-line spec...
SPEC
```

| Tier       | Model         | Effort | Use for |
| ---------- | ------------- | ------ | ------- |
| **`luna`** | gpt-5.6-luna  | medium | **Default.** Mechanical + standard bulk; most of the value at far lower cost |
| `terra`    | gpt-5.6-terra | high   | Only when Luna failed or the spec is clearly above Luna |
| `frontier` | gpt-5.6-sol   | high   | **Rare.** Hardest cross-vendor second opinion only; never default |

Rules:

1. **Prefer Luna.** Do not default to Terra or Sol to "be safe."
2. **One worktree per worker.** The sandbox blocks writes outside the worktree.
   It does NOT block network (workers need to fetch deps and read upstream
   docs), so "no commit, no push" is enforced by the spec guardrails the
   wrapper appends, not by the sandbox.
3. **Full spec up front** (files, acceptance, patterns to imitate).
4. **Fan out in parallel** for independent tasks.
5. **Opus reviews** the diff, runs **`ci`** (or `ci lint` + `ci test`), then
   commits Conventional Commits. Do not skip `ci` and hope PR CI is the first
   signal.

## Quota Exhaustion (exit code 42)

1. One Discord `monolith-monolith-agent-notify` (level `warn`, main-loop only).
2. Fall back to Sonnet implementers for remaining tasks.
3. Do not retry codex in a loop; do not re-notify in the same session.

## Preflight (optional)

```bash
codex login status   # expect "Logged in using ChatGPT"
```

If not logged in, ask Joe to run `codex login` (not headless).
