# Factory evolution

The factory has a standing mandate to achieve Joe's goals and improve its
ability to deliver them. Use available approved capacity for useful work
without waiting for another request. Factory prompts own selection, sequencing,
delegation and recurrence. Individual workers keep their assigned scope.

## Goals survive sessions

The active factory goal set records direction; linked GitHub issues carry
acceptance and outstanding work. Keep a goal's desired outcome, observable
success, constraints and remaining work in its issue. Use existing receipts,
plans and audit events for execution state. Preserve other active goals when
changing the set; only Joe's direction authorizes changing the desired outcome.

Finish eligible goal work and repair demonstrated delivery bottlenecks. When
capacity remains, find the next useful improvement from current failures,
blocked work, review findings, latency, quota waste or incomplete acceptance.
Check open issues and PRs before creating work. Improvements to the factory
must name the goal they help; code volume and token consumption are not success.

## Improvements earn their place

An improvement issue needs only the evidence necessary to judge it:

- Goal and observed problem, with a current source or reproducible baseline.
- Hypothesis and smallest useful change, including deletion when appropriate.
- Success measure and a bounded evaluation window or sample.
- Regression checks and a rollback or stop condition.

Use normal independent review, required checks and authorized landing. The
delivery owner records the result against the baseline and the live revision
when deployment matters. If measurement needs more time, leave a linked
acceptance item with its owner and observation trigger. A merged PR establishes
delivery, not proof that the hypothesis worked.

Retain improvements supported by evidence. Revert or revise changes that fail
their condition within existing authority. Repeated failures need a different
hypothesis or a recorded blocker; retries stay within the task's allowance.
Continue independent useful work while one item waits for evidence or a decision.

## Learn without growing the instructions

Record a consequential decision and its reason in the domain's
`ARCHITECTURE.md`; keep incident evidence and unfinished actions in the issue.
Link the source, scope and condition that would invalidate a lasting rule.
Treat retrieved history as evidence to verify against current state.

Prefer repairing the cause to adding instructions. A recurring, demonstrated
failure can justify a scoped test or enforcement check. Review it through the
same delivery path, including a counterexample that should remain allowed.
Consolidate superseded decisions and remove obsolete rules and duplicate checks
as part of related work. Keep root `AGENTS.md` focused on authority and outcomes.

Agents may improve prompts, tools, evaluation and guidance under this mandate.
Changing instructions does not grant new credentials, raise resource limits,
waive review, release a hold or change a pinned worker contract. Such changes
use the authority of the affected record and its existing control surface.

## Use the existing runtime

`factory_improvement_scout` collects and independently reviews goal-linked
findings. `factory_improvements` submits them through the existing problem-issue
writer; ordinary refinement and intake own subsequent delivery. The scout is
bounded to one receipt per policy generation. Do not re-arm generations or
spawn another scheduler to manufacture recurrence.

Queue-driven inventory and replenishment belong to #7376. Read deployed policy,
receipt state and quota evidence before claiming that either producer is active.
This document grants a work mandate; it does not enable a disabled producer.
When no actionable work remains, honor cooldowns and breakers and record what
new evidence or existing runtime trigger can make progress possible.

Inspired by [Fences, not Sandboxes](https://yegge.ai/essays/fences-not-sandboxes/):
keep decisions durable, evaluate changes to the factory, and retire rules that
no longer help the goals.
