---
name: ship
description: Drive a feature end to end through the plan, ADR, failing BDD, implement and STPA lifecycle. Use when the user says `/ship "<feature>"`, asks to "ship a feature", or wants a feature taken from idea to merged with a safety assessment.
---

# /ship

Take a feature from idea to merged. `/ship "<feature>"`.

You are the orchestrator in the main conversation. You hold the phase sequence,
decide which phases this change actually needs, own the git operations and the
issue ledger, and dispatch every unit of work to an agent. Keep your own context
for sequencing and judgment: the agents carry the detail so this session does not
have to.

## Roles are agents

| Work | Agent | Runs on |
|------|-------|---------|
| Implementation of a specified task | `implementer` | Codex Sol, via dispatch |
| Review of the finished diff | `reviewer` | Opus |
| Writing the ADR | `adr-author` | Sonnet |
| Safety model refresh | `stpa-analyst` | Sonnet |
| A genuine wall Opus could not clear | `escalation` | Fable |

`implementer`, `reviewer` and `escalation` have no `Write` or `Edit` tool, so
implementation cannot leak into review and review cannot quietly become a fix.

Give each agent the full brief in one shot. An agent that has to come back for
clarification costs more than the spec would have. Fan out independent tasks in
the same turn rather than serializing them.

Codex `frontier` (Sol) stays available as a rare cross-vendor second opinion on
the hardest Phase 4 diffs, added alongside the `reviewer` pass and never
substituted for it.

Do **not** open bare `bazel` on the Mac, and do not treat PR CI as the first test
run: `ci` before push so Workflows mostly cache-hit. Routing detail lives in
`.claude/CLAUDE.md`; branch and merge mechanics in `pr-workflow`.

## Phase 0: clarify

If the feature, its scope, its success criteria, or the systems it touches are
unclear, ask 2 to 4 questions (`AskUserQuestion`). Restate the agreed direction
in one sentence, then proceed.

## Which phases this change needs

Phases 1 and 4 always run. The other three are conditional, and deciding
correctly is most of the value here: running all five on a small feature spends a
review cycle per unnecessary phase, while skipping a needed one loses the record
or the safety case.

**Phase 2, ADR.** Run when the change adds a service, dependency or external
integration; alters a data model or adds a non-additive migration; moves a
security, auth or public/private tier boundary; changes a cross-service contract;
or reverses an earlier ADR. Skip for bug fixes, copy, config values, dependency
bumps, docs, and refactors with no behaviour change.

**Phase 3, failing BDD.** Run when user-visible behaviour changes, or the change
adds an endpoint, page or scheduled job. Skip for internal refactors, docs, and
CI or infra-only changes.

**Phase 5, STPA.** Run when the change adds or alters a control action (a new
scheduled job, a new trigger, a new write path to the cluster or database); adds
or alters feedback the system acts on (a new data source, a changed freshness or
retention rule); or touches a safety constraint (new RBAC verbs, new deletion or
retention logic, a new autonomous action). Skip for additive UI, copy, docs, and
any small feature that touches no control loop. Skip outright if the touched
system has no `STPA.md`.

**Record every skip on the issue as `n/a: <reason>`.** A skipped phase with a
stated reason is a decision; a silently skipped phase is a gap.

## The ledger

Open or reuse a GitHub tracking issue (`enhancement`, plus `agent-ready` if
autonomously pickable). The issue body is the source of truth for what is left to
do. Multi-part work gets a parent with sub-issues. Never `docs/plans/`, which is
retired and hook-blocked. Always a worktree and a feature branch.

On resume: `gh issue view <n>`, find the first unchecked box, continue there.
Closing the issue is how "shipped" is recorded.

```markdown
## Summary

<!-- one sentence: what we are shipping and why -->

## Plan

<!-- tasks, acceptance, systems touched -->

## Lifecycle

- [ ] 1-plan done= notes=
- [ ] 2-adr pr= path=docs/decisions/... <!-- or n/a: reason -->
- [ ] 3-bdd pr= specs= <!-- or n/a: reason -->
- [ ] 4-implement pr= <!-- must include Closes #<this issue> -->
- [ ] 5-stpa pr= systems= <!-- or n/a: reason -->

## Phase 4 gates (all must be checked before "ready for review")

- [ ] `ci` green on the worktree
- [ ] PR body contains `Closes #<this issue>` (or the parent, if a sub-issue)
- [ ] No chart version in the diff (`Chart.yaml` `version:` / `targetRevision:` are written post-merge)
- [ ] Branch up to date with main (strict checks; rebase if BEHIND)
- [ ] `reviewer` agent run on the full diff; findings acted on or answered
- [ ] Human review requested

## Notes / blockers
```

Sub-issues inherit the Phase 4 gates. Lifecycle 1 to 5 stays on the parent unless
a sub-issue is independently shippable end to end.

## Phase 1: plan

Clarify, then write the plan into the tracking issue. Split it into tasks small
enough that each is a single one-shot brief for an `implementer`, naming the
files, the acceptance criteria, and the pattern to imitate.

## Phase 2: ADR

Dispatch `adr-author` with the decision, the rationale, and the options that were
rejected. Rebase-merge on green.

## Phase 3: failing BDD

Specs define done. `bdd_test(future = True, ...)` with `@covers_*` markers from
`shared/testing/markers.py`. The gating Test action excludes `-future`; the
informational "BDD future features" action runs them. Merging red specs is
intentional. This is implementation work, so it goes to `implementer`.

## Phase 4: implement

1. Dispatch `implementer` per task, in parallel where tasks are independent.
2. Keep on Opus only what a slow CI round-trip is the first detector for: deep
   Helm, Bazel/apko, RBAC verbs, migration ordering, cross-service wiring.
3. Run `ci` and commit with Conventional Commits. `implementer` agents never
   commit.
4. Dispatch `reviewer` **once**, on the full diff, at the end. Not per task.
5. Check every Phase 4 gate before calling the PR ready.
6. Human review only, no auto-merge. Report the PR URL and stop.

If an `implementer` reports `CODEX_QUOTA_EXHAUSTED`: send one `warn` Discord
notify, switch the remaining tasks to Sonnet implementers, and do not notify
again this session.

If Opus is genuinely stuck rather than merely slow, dispatch `escalation` with
what has already been tried. Do not open it for routine difficulty.

## Phase 5: STPA

For each touched system meeting the criteria above, dispatch `stpa-analyst` with
the system directory and what changed. Rebase-merge on green, record the systems
on the issue.

## Merge cadence

Phases 2, 3 and 5 can `gh pr merge --auto --rebase`, followed through to merged.
Phase 4 is human review only. The branch must stay up to date with main.

## Stop conditions

- Phase 0 clarification unanswered: ask and stop (Discord if Joe may be away).
- Phase 4 gates incomplete: finish them before requesting human review.
- Phase 4 PR awaiting human review with gates green: stop, report the URL.
- CI red on a real failure outside scope: stop and diagnose (`ci-triage`).
- All lifecycle boxes checked and the issue closed: report shipped, with links.
- User says stop: stop.
