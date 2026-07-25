---
name: ship
description: Drive a feature end to end through a 5-phase lifecycle (plan, ADR, failing BDD, implement, STPA). Use when the user says `/ship "<feature>"`, asks to "ship a feature", or wants a feature taken from idea to merged with a safety assessment.
---

# /ship

Take a feature from idea to merged-with-a-safety-assessment. `/ship "<feature>"`.

This skill is the **orchestrator in the main conversation** (Opus 5). It clarifies
direction, holds the phase sequence and review gate, runs git, and owns the
ledger. It dispatches implementation bulk to Codex (Luna preferred) via
`codex-implement`, verification via `ci`, and STPA via the `stpa` skill.

## Model / multi-CLI flow (house style)

| Role | Who | Notes |
|------|-----|--------|
| Main loop, plan, ADR, review | **Opus 5** | Judgment and CI-only-verifiable work |
| Implementation bulk | **Codex Luna** (default), Terra only when Luna is too weak | `bazel/tools/codex/dispatch.sh` |
| Fallback implementers | **Sonnet** | When Codex quota exhausted (exit 42) |
| Cross-vendor second opinion | Codex `frontier` (Sol) | Rare; never default |
| Escalation of last resort | **Fable** (`/model fable`) | Not a normal-day tool |
| Local verification | **`ci`** | lint + regen + `bb remote` Test 1:1 with Workflows |

Do **not** open bare `bazel` on the Mac. Do **not** treat PR CI as the first
test run: run `ci` (or `ci test`) before push so Workflows mostly cache-hit.

Full routing detail: `.claude/CLAUDE.md` Model Routing. Dispatch detail:
`codex-implement` skill.

## Phase 0 — Clarify the direction (always first)

If feature, scope, success criteria, or systems touched are unclear, ask 2-4
questions (`AskUserQuestion`). Restate the agreed direction in one sentence,
then proceed.

## State ledger (GitHub Issues)

Open or reuse a tracking issue (`enhancement`, `agent-ready` if pickable). Body:

```
## Lifecycle state
- [ ] 1-plan      pr=
- [ ] 2-adr       pr=
- [ ] 3-bdd       pr=
- [ ] 4-implement pr=
- [ ] 5-stpa      pr=  systems=
```

Plan text lives in the issue (or an uncommitted working file), never
`docs/plans/` (retired). Multi-part work: parent + **sub-issues**. Close the
issue when all five boxes are checked. Always worktree + feature branch,
never main.

On every `/ship` resume: read the issue, first unchecked phase, continue there.

## The five phases

| # | Phase | How | Artifact | Merge |
|---|-------|-----|----------|-------|
| 1 | Plan | brainstorm + writing-plans in main loop (Opus) | Tracking issue holds plan + ledger | n/a |
| 2 | ADR | `adr` skill | `docs/decisions/<cat>/NNN-*.md` | auto rebase |
| 3 | Failing BDD | author `bdd_test(future = True, ...)` with `@covers_*` | Red future specs | auto rebase |
| 4 | Implement | **Codex Luna** (default) / Terra if needed / Sonnet fallback; Opus for CI-only-hard slices; **`ci` before push**; one human PR review | Feature code | **pause for human** |
| 5 | STPA | `stpa` skill per touched system | `<system>/STPA.md` | auto rebase |

### Phase 3 — Failing BDD

Specs define done. Gating Test excludes `-future`; informational "BDD future
features" runs them. Include `@covers_*` markers
(`shared/testing/markers.py`). Merging red specs is intentional.

### Phase 4 — Implement

1. Split the plan into tasks. Prefer **parallel Codex Luna** dispatches (one
   worktree per worker) for independent mechanical/standard work.
2. Keep on **Opus** anything only slow CI can verify (deep Helm, Bazel/apko,
   RBAC verbs, migration ordering, cross-service wiring).
3. After implementation chunks: dispatching agent reviews the diff (Opus
   eyes), runs **`ci`** (or at least `ci lint` + `ci test`), commits
   Conventional Commits, pushes, opens/updates the PR.
4. **One comprehensive human code review** per PR (not per sub-task). Do not
   auto-merge phase 4. Report PR URL and **stop** until merged.
5. Codex exit 42: one Discord notify (main loop only), fall back to Sonnet;
   do not re-notify in-session.

### Phase 5 — STPA

For each system subtree touched by phase 4, run `stpa` (`system =
projects/monolith`, etc.). Rebase-merge on green. Record systems in the ledger.

## Merge cadence

Phases 2, 3, 5: `gh pr merge --auto --rebase` and follow through. Phase 4:
human review only. Branch must stay up to date with main (strict checks).

## Stop conditions

- Phase 0 blocked on unanswered clarification: ask and stop.
- Phase 4 PR awaiting human review: stop, report URL.
- CI red on a real failure out of scope: stop, diagnose (prefer `ci test` /
  BuildBuddy MCP logs).
- All five boxes checked: report shipped with PR links.
- User says stop: stop.
