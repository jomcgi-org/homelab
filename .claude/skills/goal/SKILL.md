---
name: goal
description: Drive a feature from design to safety assessment as a single resumable lifecycle — plan, ADR, failing BDD specs, implementation, STPA. Use when the user says `/goal "<feature>"`, asks to "run the goal lifecycle", or wants a feature taken end to end with progress that survives context compaction. Composes the brainstorming, writing-plans, adr, subagent-driven-development, and stpa skills, plus the future-feature BDD lane. Auto-merges the docs/test phases on green; pauses at the implementation PR for human review.
---

Drive a feature through five phases, end to end, making progress until done and
compacting as you go. Invoke with the feature description: `/goal "<feature>"`.

## The compaction principle (why this is resumable)

Durable state lives in **artifacts**, never in context: the plan doc, the ADR, the
committed BDD specs, the merged PRs, and `<system>/STPA.md`. Heavy work is
delegated to **subagents** (each gets a fresh context window and returns only a
summary). A context compaction, or even a brand-new session, loses nothing:
re-running `/goal` for the same feature reads the state ledger and resumes from
the first incomplete phase. Never hold phase state only in your head.

## State ledger & resumability

The phase-1 plan doc IS the ledger. It lives at
`docs/plans/<YYYY-MM-DD>-<slug>-goal.md` and ends with a machine-readable block:

```
## Lifecycle state
- [ ] 1-plan      pr=
- [ ] 2-adr       pr=
- [ ] 3-bdd       pr=
- [ ] 4-implement pr=
- [ ] 5-stpa      pr=  systems=
```

On every invocation:
1. Look for an in-progress goal doc matching the feature (glob
   `docs/plans/*-goal.md`, read the title). If found, read its ledger and resume
   from the first `[ ]` phase. If not, start at phase 1.
2. After a phase's PR merges, check its box and record the PR URL (and, for
   phase 5, the systems touched). Commit the ledger update with the phase's PR so
   it is never lost.

Always work on a worktree + feature branch (never main); the plan-file hook
enforces this for the plan doc.

## The five phases

Each phase: produce an artifact, land it as a PR, gate on CI green, then advance.
Use the merge policy in the table below.

| # | Phase | Skill / mechanism | Artifact | Merge |
|---|-------|-------------------|----------|-------|
| 1 | Plan | `brainstorming` then `writing-plans` | `docs/plans/<date>-<slug>-goal.md` (with ledger) | auto (rebase) |
| 2 | ADR | `adr` | `docs/decisions/<cat>/NNN-*.md` | auto (rebase) |
| 3 | Failing BDD | author specs via `bdd_test(future = True, ...)` | red `future`-tagged specs (carry `@covers_*` markers) | auto (rebase) |
| 4 | Implement | `subagent-driven-development` | feature code; specs go green; drop `future = True` | **pause for review** |
| 5 | STPA | `stpa` skill per touched system | `<system>/STPA.md` (logical + physical) | auto (rebase) |

### Phase 1 — Plan
Run `brainstorming` to converge on the approach (simplest-first, per repo
philosophy), then `writing-plans` to save the plan doc. Append the
`## Lifecycle state` block. This is the only phase that creates the ledger.

### Phase 2 — ADR
Run the `adr` skill to record the rationale (the decision and why), not the
implementation steps. Link the plan doc.

### Phase 3 — Failing BDD (the executable spec)
Author the BDD specs that define "done" for the feature, as
`bdd_test(future = True, srcs = [...])` targets. They go red, which is expected:
the gating `Test` action excludes `-future`, and the non-required
`BDD future features` action runs them. Include the `@covers_*` markers
(`shared/testing/markers.py`) so the completeness gate is pre-satisfied the
moment the real routes/pages/functions exist. Merging red specs here is the whole
point of step 3.

### Phase 4 — Implement
Run `subagent-driven-development` against the plan until every `future` spec
passes. Then **promote**: remove `future = True` from each `bdd_test` target so the
spec rejoins the gating suite (`BDD future features` going green is the signal it
is time). This is the one phase that touches feature code, so its PR **pauses for
the repo-mandated single comprehensive code review**: open it ready-for-review,
report the URL, and STOP. Resume phase 5 only after a human merges it.

### Phase 5 — STPA
For each system whose subtree the feature touched (derive from the phase-4 diff,
e.g. `projects/monolith`), run the `stpa` skill with that `system`. It refreshes
`<system>/STPA.md` (logical + physical) against the now-built feature and
rebase-merges on green. Record the systems in the ledger.

## Merge cadence

Phases 1, 2, 3, 5 are docs/test-only and low risk: enable auto-merge
(`gh pr merge --auto --rebase`) and follow through to merged before advancing.
Phase 4 is the feature diff: never auto-merge it; it goes through human review.
This honors "progress until the end" for everything mechanical while keeping the
implementation behind the review boundary.

## Delegation (how compaction actually happens)

Delegate the context-heavy work to subagents so your orchestrator context stays
thin:
- Phase 3: a subagent authors the BDD specs from the plan and returns the target
  list.
- Phase 4: `subagent-driven-development` already dispatches per-task implementers.
- Phase 5: a subagent (or the `stpa` skill's own run) does the code-grounding read
  and JSON extraction; you keep only the PR URL.
Keep only outcomes (artifact paths, PR URLs, the ledger) in your own context.

## Stop conditions
- Phase 4 PR opened and awaiting human review: STOP and report.
- A phase's CI is red on a real failure you cannot resolve in-scope: STOP, report
  the diagnosis and where you are stuck, leave the PR open.
- All five boxes checked: report the feature is complete with the five PR links.
- The user says stop: stop.
