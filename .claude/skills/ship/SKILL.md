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

## Model / multi-CLI flow

Routing is owned by `.claude/CLAUDE.md` Model routing and is not restated here:
Opus orchestrates and reviews, Luna implements, Fable is a context-window
escalation of last resort. `/ship` adds one wrinkle, Codex `frontier` (Sol) as a
rare cross-vendor second opinion on the hardest Phase 4 diffs, added alongside
the Opus review and never substituted for it.

Do **not** open bare `bazel` on the Mac. Do **not** treat PR CI as the first
test run: run `ci` (or `ci test`) before push so Workflows mostly cache-hit.

Dispatch detail: `codex-implement` skill. Branch, PR, and merge mechanics:
`pr-workflow` skill.

## Phase 0 — Clarify the direction (always first)

If feature, scope, success criteria, or systems touched are unclear, ask 2-4
questions (`AskUserQuestion`). Restate the agreed direction in one sentence,
then proceed.

## State ledger (GitHub Issues — forced tracking)

Open or reuse a tracking issue (`enhancement`, `agent-ready` if pickable).
**The issue body is the source of truth for what is left to do.** Put the plan
and the checklists below into the issue (never `docs/plans/`, which is retired).
Multi-part work: parent + **sub-issues**. Always worktree + feature branch,
never main.

On every `/ship` resume: `gh issue view <n>`, find the first unchecked box,
continue there. Update the issue (`gh issue edit <n> --body-file …` or equivalent)
as each gate is met. Closing the issue is how "shipped" is recorded.

### Issue body template (paste at create time)

```markdown
## Summary
<!-- one sentence: what we are shipping and why -->

## Plan
<!-- full plan lives HERE, not in the repo. Tasks, acceptance, systems touched. -->

## Lifecycle
- [ ] 1-plan        done=  notes=
- [ ] 2-adr         pr=    path=docs/decisions/...
- [ ] 3-bdd         pr=    specs=
- [ ] 4-implement   pr=    <!-- must include Closes #<this issue> -->
- [ ] 5-stpa        pr=    systems=

## Phase 4 gates (must all be checked before "ready for review")
- [ ] `ci` green on the worktree (or at least `ci test` 1:1 with Workflows Test)
- [ ] Implement PR body contains `Closes #<this issue>` (or parent if sub-issue)
- [ ] Chart bump if the change must deploy (`bazel/tools/git/bump-chart.sh projects/<svc>`)
      N/A if docs/test-only: write `n/a — <reason>`
- [ ] Branch up to date with main (strict required checks; rebase if BEHIND)
- [ ] Opus review of the full PR diff done (agent); human review requested

## Notes / blockers
<!-- Discord notify only for blockers that need Joe -->
```

Sub-issues inherit the same **Phase 4 gates** section; lifecycle phase 1–5 stays on
the parent unless the sub-issue is independently shippable end-to-end.

## The five phases

| # | Phase | How | Artifact | Merge |
|---|-------|-----|----------|-------|
| 1 | Plan | Clarify + write plan **into the tracking issue** (Opus) | Issue body | n/a |
| 2 | ADR | `adr` skill | `docs/decisions/<cat>/NNN-*.md` | auto rebase |
| 3 | Failing BDD | `bdd_test(future = True, ...)` with `@covers_*` | Red future specs | auto rebase |
| 4 | Implement | Codex Luna (default) / Terra if needed / Sonnet fallback; Opus for CI-only-hard slices; **all Phase 4 gates** | Feature PR | **pause for human** |
| 5 | STPA | `stpa` skill per touched system | `<system>/STPA.md` | auto rebase |

### Phase 3 — Failing BDD

Specs define done. Gating Test excludes `-future`; informational "BDD future
features" runs them. Include `@covers_*` markers
(`shared/testing/markers.py`). Merging red specs is intentional.

### Phase 4 — Implement (gates are non-optional)

1. Split the plan into tasks. Prefer **parallel Codex Luna** dispatches (one
   worktree per worker) for independent mechanical/standard work.
2. Keep on **Opus** anything only slow CI can verify (deep Helm, Bazel/apko,
   RBAC verbs, migration ordering, cross-service wiring).
3. After implementation chunks: Opus reviews the diff, runs **`ci`** (full, or
   `ci lint` + `ci test`), commits Conventional Commits.
4. **Before calling the PR "ready for review"** every Phase 4 gate on the
   tracking issue must be checked. Especially:
   - Green `ci` (do not open "ready" on hope that Workflows will pass first)
   - PR description includes `Closes #<tracking issue>`
   - Chart version bumped in the **same** PR if code must deploy
5. One comprehensive **human** code review per PR. Do not auto-merge phase 4.
   Report PR URL and **stop** until merged. Then tick lifecycle `4-implement`.
6. Codex exit 42: one Discord notify (main loop only), fall back to Sonnet;
   do not re-notify in-session.

### Phase 5 — STPA

For each system subtree touched by phase 4, run `stpa` (`system =
projects/monolith`, etc.). Rebase-merge on green. Record systems on the issue.

## Merge cadence

Phases 2, 3, 5: `gh pr merge --auto --rebase` and follow through. Phase 4:
human review only. Branch must stay up to date with main (strict checks).

## Stop conditions

- Phase 0 blocked on unanswered clarification: ask and stop (Discord if Joe may be away).
- Phase 4 gates incomplete: do not request human review yet; finish gates.
- Phase 4 PR awaiting human review (gates green): stop, report URL.
- CI red on a real failure out of scope: stop, diagnose (`ci test` / BuildBuddy MCP).
- All lifecycle boxes checked and issue closed: report shipped with PR links.
- User says stop: stop.
