---
name: ship
description: Drive a feature end to end through a 5-phase lifecycle (plan, ADR, failing BDD, implement, STPA), clarifying the direction first, then reusing the built-in /goal loop for cross-turn persistence and launching a workflow per fan-out phase. Use when the user says `/ship "<feature>"`, asks to "ship a feature", or wants a feature taken from idea to merged with a safety assessment. Composes the brainstorming, writing-plans, adr, subagent-driven-development, and stpa skills plus the future-feature BDD lane.
---

Take a feature from idea to merged-with-a-safety-assessment. `/ship "<feature>"`.

This skill is the orchestrator that runs in the main conversation. It does the
things a workflow cannot (clarify with you, pause for review, run git), and it
delegates the parallelizable phases to workflows.

## How the three layers fit

- **`/ship` (this skill, main conversation):** clarifies direction up front, holds
  the phase sequence and the review gate, runs git, owns the durable ledger.
- **built-in `/goal` (persistence):** `/ship` arms it with the completion
  condition so the session keeps working across turns and compactions until the
  feature is fully shipped. Do NOT shadow it with a same-named skill.
- **workflows (fan-out):** each parallelizable phase is launched as its own
  workflow (`ultracode: ...` or a saved sub-workflow). Workflows orchestrate up to
  16 concurrent subagents and keep their intermediate results out of context.

Workflow constraints that shape this design (per the docs): a workflow takes **no
mid-run user input**, has **no direct shell/git access** (its agents do the work),
and **resumes only within the same session**. So the interactive clarify step and
the human review gate live here in `/ship`, not inside a workflow, and each
fan-out phase is a separate workflow (a stage boundary is a sign-off boundary).

## Phase 0 — Clarify the direction (do this first, always)

Before arming anything, make the goal crisp. If the feature, scope, success
criteria, or which system(s) it touches are unclear, ask 2-4 questions
(`AskUserQuestion`) and iterate until there is no ambiguity. This is the only
place to resolve open questions: once workflows are running they cannot ask you
anything. Restate the agreed direction in one sentence, then proceed.

## Arm persistence

Set the built-in `/goal` completion condition to: "all five lifecycle artifacts
landed: plan + ADR + failing BDD specs merged; feature merged after human review;
STPA refreshed and merged; every box in the ledger checked." That keeps the
session driving to completion across turns without re-prompting you.

## State ledger & resumability

The phase-1 plan doc is the ledger, at
`docs/plans/<YYYY-MM-DD>-<slug>-ship.md`, ending with:

```
## Lifecycle state
- [ ] 1-plan      pr=
- [ ] 2-adr       pr=
- [ ] 3-bdd       pr=
- [ ] 4-implement pr=
- [ ] 5-stpa      pr=  systems=
```

Durable state lives in artifacts (plan, ADR, committed specs, merged PRs,
`<system>/STPA.md`), never only in context. Workflow resume is same-session only,
so this ledger is the cross-session source of truth: on every invocation, find
the first unchecked phase and resume there. Commit each ledger update with that
phase's PR. Always work on a worktree + feature branch (never main).

**GitHub tracking issue (repo source of truth for outstanding work).** At Phase 1,
open (or reuse) a GitHub issue for the feature (`gh issue create`, `enhancement`
label, `agent-ready` if autonomously pickable) and record its number in the plan
front matter and the ledger. It is the outward-facing home of "what's left"; the
plan ledger is the mechanical phase tracker. If the feature decomposes into several
independently-shippable pieces, open them as **sub-issues** of that tracking issue
(`gh api repos/jomcgi/homelab/issues/<parent>/sub_issues -f sub_issue_id=<child>`).
**Close the tracking issue when all five boxes are checked** (or let the phase-4 PR
close it via a `Closes #<n>` line); a closed issue is how the repo records the
feature shipped.

## The five phases

| # | Phase | How it runs | Artifact | Merge |
|---|-------|-------------|----------|-------|
| 1 | Plan | `brainstorming` + `writing-plans`; optional workflow to draft from several angles and weigh them | `docs/plans/<date>-<slug>-ship.md` (with ledger) | auto (rebase) |
| 2 | ADR | `adr` skill (inline) | `docs/decisions/<cat>/NNN-*.md` | auto (rebase) |
| 3 | Failing BDD | author `bdd_test(future = True, ...)` specs (inline or a small workflow) | red `future`-tagged specs carrying `@covers_*` markers | auto (rebase) |
| 4 | Implement | **workflow**: fan out the plan's tasks across parallel agents until every future spec passes; then drop `future = True` to promote | feature code | **pause for human review** |
| 5 | STPA | **workflow**: one agent per touched system, each running the `stpa` skill | `<system>/STPA.md` (logical + physical) | auto (rebase) |

### Phase 3 — Failing BDD
The specs that define "done." They go red (gating `Test` excludes `-future`; the
non-required `BDD future features` action runs them). Include `@covers_*` markers
(`shared/testing/markers.py`) so the completeness gate is pre-satisfied once the
real routes/pages/functions exist. Merging red specs here is the point.

### Phase 4 — Implement (workflow + review gate)
Launch a workflow that fans the plan's tasks across parallel agents, each driving
its slice until the relevant `future` specs pass; then promote (remove
`future = True`). The workflow **ends by opening the PR ready for review** — it
cannot pause for sign-off, and per the repo it gets ONE comprehensive human code
review. So `/ship` reports the PR URL and **stops**. Resume phase 5 only after a
human merges it.

### Phase 5 — STPA (workflow)
For each system whose subtree the phase-4 diff touched, launch a workflow with one
agent per system running the `stpa` skill (`system = projects/monolith`, etc.). It
refreshes `<system>/STPA.md` (logical + physical) and rebase-merges on green.
Record the systems in the ledger.

## Launching and saving fan-out workflows
To run a fan-out phase, request a workflow in-prompt (the `ultracode` keyword, or
plain "run this as a workflow"). After a good run you can save its script
(`/workflows`, select the run, press `s`) as a reusable command, e.g.
`/ship-implement` and `/ship-stpa`, so later features reuse the same orchestration.
Add the shell/git commands the agents need to your allowlist before a long run so
they do not prompt mid-run.

## Merge cadence
Phases 1, 2, 3, 5 are docs/test-only: enable auto-merge (`gh pr merge --auto
--rebase`) and follow through to merged before advancing. Phase 4 is the feature
diff: never auto-merge; it goes through human review.

## Stop conditions
- Phase 0 blocked on a clarification you have not answered: ask and stop.
- Phase 4 PR opened and awaiting human review: stop and report the URL.
- A phase's CI is red on a real failure out of scope: stop, report the diagnosis.
- All five boxes checked: report the feature is shipped with the five PR links.
- The user says stop: stop.
