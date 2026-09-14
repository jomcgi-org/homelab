# ADR 064: Ship and STPA lifecycle

**Status:** Superseded in part by [#4667](https://github.com/jomcgi-org/homelab/issues/4667)
**Adopted:** 2026-06-16
**Recorded:** 2026-09-14

## Context

Feature delivery needs durable evidence of intent, behavior, implementation,
review, and safety analysis. Those artifacts must survive context compaction and
must not let an intentionally failing specification weaken the required test
gate.

The adopted lifecycle originated in a plan that was removed when GitHub Issues
became the source of truth for outstanding work. That plan is historical
provenance, not a file to recreate or a current work ledger.

## Adopted decision

Feature delivery follows five ordered phases:

1. **Plan.** Clarify scope, success criteria, affected systems, and work items.
   Persist the active ledger in the tracking issue.
2. **Architecture rationale.** Record what was decided and why before behavior
   is specified. At adoption this was an ADR phase using the repository `adr`
   skill. The repository retired that skill and the ADR workflow under #4667;
   current `/ship` runs this phase in the affected domain's `ARCHITECTURE.md`.
3. **Future BDD.** Add executable specifications with
   `bdd_test(future = True, ...)` before implementation. The macro adds the
   `future` tag. The required Test path excludes `future` tests because an
   unbuilt feature is expected to make these specifications red, and that
   expected red state must not block unrelated merges.
4. **Implementation and human review.** Implement the behavior, make the future
   specifications pass, and require human review of the implementation. Once a
   future specification passes, remove `future = True` so it is promoted into
   the required Test suite.
5. **STPA refresh.** After implementation, refresh the safety model for each
   affected system when the change alters a control action, feedback signal, or
   safety constraint. The repository `stpa` skill extracts grounded findings and
   renders the colocated safety document deterministically.

`/ship` and `stpa` are repository skills under `.claude/skills/`. `/ship`
orchestrates the lifecycle and records skipped conditional phases with a reason;
`stpa` owns the safety-analysis method and rendered artifact.

The designed `BDD future features` BuildBuddy action is advisory. Red means a
feature is still being built, while green means all future specifications pass
and should be promoted. As of this record, the whole action is commented out in
`buildbuddy.yaml` to avoid runner cost. It does not currently run and provides no
automatic promotion signal. Until it is re-enabled, promotion is a manual step.
The required Test path continues to exclude the `future` tag.

## Rationale

The sequence turns intent into progressively stronger evidence. Planning fixes
scope, architecture rationale preserves why, BDD defines externally observable
completion before code is written, human review checks the completed
implementation, and STPA reassesses the control structure that actually shipped.

Separating future specifications from required Test preserves both signals. The
required gate remains actionable, while an advisory future lane can report
expected failures without normalizing failures in the gating suite. Promotion,
not merely a passing advisory run, is what converts the specification into a
regression guard.

## Consequences

- Every phase has a durable artifact or an explicit not-applicable reason in the
  issue ledger.
- Intentionally red future specifications do not block the required Test path.
- A passing future specification is not complete until `future = True` is
  removed and the required suite owns it.
- While the advisory action remains commented out, no CI signal identifies
  passing future specifications, so promotion depends on implementation review.
- Human review remains the completion gate for implementation changes.
- STPA follows implementation so its evidence can cite the realized control
  structure.
- This historical record does not restore the retired `adr` skill or change the
  current #4667 policy. Current decisions continue to live in domain
  `ARCHITECTURE.md` files.

## References

- [`projects/monolith/bdd_test.bzl`](../../../projects/monolith/bdd_test.bzl):
  future tagging and promotion semantics.
- [`buildbuddy.yaml`](../../../buildbuddy.yaml): required Test exclusion and the
  currently commented advisory action.
- [`.claude/skills/ship/SKILL.md`](../../../.claude/skills/ship/SKILL.md): current
  lifecycle orchestration and the human-review gate.
- [`.claude/skills/stpa/SKILL.md`](../../../.claude/skills/stpa/SKILL.md): STPA
  analysis and deterministic rendering workflow.
- [Issue #3967](https://github.com/jomcgi-org/homelab/issues/3967): request for
  this lifecycle rationale.
- [Issue #4667](https://github.com/jomcgi-org/homelab/issues/4667): decision to
  retire the ADR workflow and use domain architecture documents.
- [Historical source plan](https://github.com/jomcgi-org/homelab/blob/db59710e349439abf7dd7c740342b6123d897473/docs/plans/2026-06-16-stpa-bdd-goal-lifecycle-plan.md):
  provenance removed by commit `8117e4d02e1be9b2a7c3ba748ddfa784744e297d`.
