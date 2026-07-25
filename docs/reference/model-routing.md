# Model routing and wall-time

How work is routed across models and CLIs in this repo. Read this when deciding
who should do a piece of work: the main loop, a subagent, or a Codex worker.

**Objective:** lowest wall-time-to-outcome at maximum quality within the weekly
subscription budget. The budget is to be spent, not minimized. Route work to the
cheapest model whose mistakes are caught cheaply, and reserve the quality tiers
for mistakes only slow CI can catch.

## Who does what

| Role | Who | Why |
|------|-----|-----|
| Main loop, planning, code review | **Opus 5** | Judgment work, and the safety net for everyone else |
| Subtle or architectural implementation | **Opus 5** | Deep Helm, Bazel/apko, RBAC verbs, migration ordering, cross-service wiring: things only a CI round-trip would catch |
| Implementation bulk | **Codex Luna** | Most of the value at far lower cost, for mechanical *and* standard well-specified work |
| Bulk work Luna is too weak for | **Codex Terra** | Only when Luna has actually failed or is clearly outmatched |
| Cross-vendor second opinion | **Codex Frontier (Sol)** | Rare, on the hardest diffs, never a default |
| Fallback implementer | **Sonnet** | Codex unavailable or out of quota, or the task needs Claude-side skills, MCP, or session context |
| Read-only lookup | **Explore (Haiku)** | Search is cheap |
| Escalation of last resort | **Fable** (`/model fable`) | Genuine hard design or debug wall after a real Opus attempt, not a normal-day upgrade path |

`general-purpose` subagents inherit Opus, so keep them for judgment rather than
for search.

## Codex dispatch

Dispatch **only** via `bazel/tools/codex/dispatch.sh` (see the `codex-implement`
skill). One worktree per worker. Workers cannot commit, push, or reach the
network: the dispatching Opus agent reviews the diff, runs `ci`, and commits.

**Quota exhaustion (exit 42):** send one Discord notify via
`monolith-monolith-agent-notify` at level `warn` (main loop only), then fall back
to Sonnet implementers. No retry loop, and no second notify in the same session.

## Standing preferences

- **Opus reviews stay Opus.** Codex and Sonnet hands, Opus eyes. Sol may be
  *added* as a second opinion on the hardest diffs, never substituted for the
  Opus review.
- **Reject downgrades taken purely to save Claude quota** when they buy more CI
  round-trips. Preferring Luna over Sol saves OpenAI cost with no quality loss on
  bulk work, which is the trade worth making.
- **Fan out.** Independent work should go to multiple Codex Luna workers or
  subagents in the same turn.
- **Write the full task spec up front** so one-shot implementers succeed.
- **Effort defaults to `high`** (global settings). Bump to `xhigh` for the
  hardest long-horizon runs; never drop below `high` to save budget.
- **Fast mode (`/fast`) is off by default.** It is a cash-for-speed bypass for
  being blocked at the weekly wall.
