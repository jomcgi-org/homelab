# ADR 054: The Run View: Pinned Plans, Epistemic Registers, and Recorded-Not-Inferred Data

**Author:** jomcgi
**Status:** Draft
**Created:** 2026-08-10
**Extends:** 049 - Turn-Granular, Poll-Shaped Agent Session UI
(Accepted, the session surface this composes with);
[053 - Swarm Development, Bounded Conductor Orchestration](053-swarm-orchestration-bounded-conductor.md)
(Draft, the execution model whose state this renders)
**Relates to:** issue #4625 (tracking and work links), #4600 (the
verbatim-rationale precedent), #4614 (walkthrough composition), #4618
(the pin bug), #4624 (rationale capture)

---

## Problem

A swarm run has no legible surface. The console lists sessions; a run's
plan, progress, deviations, and future are invisible, and the sidebar
rollup actively misreports engine truth (a CANCELLED run renders as "1
warn" because session status is all the client has). The design work
recorded in #4625 produced a validated product reference (a payload-driven
mockup with a live audit proving every rendered string traces to a
contract field). This ADR records the decisions that reference rests on,
because they constrain the engine and the data model, not just the page.

## Decision

### 1. Three epistemic registers, rendered as three forms

Every element the run surface shows is one of:

- **Engine fact**: recorded state (DBOS workflow status, step outputs,
  session rows, parsed verdicts). Renders with glyphs, counts, fills,
  and strong state color.
- **System belief**: the system's own inference that could be wrong
  (a denominator read from live env for a run that predates the pin, a
  plan skeleton for a run from an older build). Renders as quiet muted
  prose; never a glyph, count, or fill. Under a stale engine connection,
  beliefs leave the view entirely rather than compounding hedges.
- **Agent testimony**: recorded utterances (rationale, self-reported
  deviations). It is fact that the agent said it, not fact that it is
  true (ADR 053 decision 1 already forbids treating claims as evidence).
  Renders quoted and attributed to node and attempt, and the client
  never restates, summarizes, or extracts testimony into fact-shaped
  chrome. Juxtaposition beside mechanical evidence is allowed;
  laundering into badges is not.

The server owns epistemics: every composed sentence is server-side and
carries its register in the payload; the client maps register to style
and holds no prose of its own.

### 2. The plan is pinned before the first node runs

A run's resolved plan (retry bound, models, turn timeout, optional cost
budget, plan version) is durably recorded before its first node
executes, and the run endpoint serves the recorded plan or no plan.
Today `config.max_attempts()` is read in the workflow body, so recovery
re-reads the environment and can change a live run's control flow
(#4618), which violates DBOS's own determinism rule and makes "attempt 2
of 2" a belief. The pin turns the denominator, the skeleton, budget
deviations, and the conductor's future amendment protocol into facts
measured against a recorded reference. Under ADR 053 the same rule
becomes: a ledger plan version row exists before dispatch.

### 3. The contract carries width, blocking, and evidence from day one

- Nodes carry explicit `deps` (edges); array order is display order
  only. Today's chain is the degenerate case. Shipping implicit sequence
  would make width a breaking change later.
- `blocked` is a first-class node state with `blocked_on {kind:
  dependency | human, refs, note}`, distinct from `queued` (a worker
  slot position): different questions, different remedies. Its color is
  two-tone: attention only when a human is the blocker.
- Gates carry an `evidence` field. Today it holds the recorded
  branch-head observation; ADR 053's rung-2 Agent CI evidence
  (invocation, `Executed N of M` verdict) lands in the same field.
- A verdict is a property of the review node, not a node of its own.
- Conductor plans of unknown width render as one placeholder expansion
  node (`expected_count: null`, decider named), never invented lanes.

### 4. Record, never infer: new recordings this surface depends on

Recording new data at the source is the sanctioned path; presenting
inference as fact is not (#4600's rule, generalized). Adopted
recordings: turn-time implementer rationale via a prompt trailer stored
verbatim in `result_text` (#4624, capture only, never routing input);
node identity as explicit columns (`node_key`, `node_attempt`) minted by
the workflow, ending client-side parsing of `local_session_id`; the
cancel actor recorded by our cancel endpoint as workflow attributes
(DBOS records no actor); the pinned budget. Deviations served by the
endpoint are mechanical-only by type; testimony never appears in the
deviations array.

### 5. Surfaces

The run is the third selectable unit in the existing `/agents` console,
rendered in the transcript pane (no separate route; it would fork
polling, staleness, theme, and the mobile drill). A master triage view
(attention band, queue pressure, in-flight list with elapsed against
enforced bounds, aggregate spend) replaces the pane's blank state and is
pinned atop the sidebar; its primary question is "is anything waiting on
me, and is everything else actually moving". Degradation is three-tier:
engine live, engine stale (aged facts, beliefs dropped, actions
disabled), engine absent (the #4615 session rollup as the permanent
floor). The DAG renders as ranked stages with dependency chips, never a
pan/zoom canvas: this repo's daily Argo Workflows experience is the
counterexample the choice is made against.

## Alternatives considered

- **Wait for the ADR 053 ledger before any endpoint.** Rejected: the
  payload contract is designed to survive the source swap, and waiting
  leaves the engine-truth misreports standing.
- **A single honesty indicator or per-element warning badges** instead
  of registers-as-form. Rejected: a global flag is too coarse for a view
  that always mixes registers; badge forests train blindness.
- **Retries as graph nodes** (the Argo shape). Rejected: the graph
  explodes exactly when calm is needed; retries are capacity pips inside
  a node, matching `policy.next_action` semantics.
- **Model-written per-file rationale after the fact.** Rejected in
  #4600 and re-rejected here; it reads as sourced when it is inferred.

## Security

Private tier only: the swarm module is registered solely in the private
app registry, the new frontend proxies live under `routes/private/`, and
a CI test asserts the public registry carries no swarm module and no
`/api/swarm` route. The cancel actor is recorded from the CF Access
identity header. No new credentials; the conductor-era boundaries are
ADR 053's and unchanged here.

## References

| Resource | Relevance |
| -------- | --------- |
| #4625 | Tracking issue: decisions list, product reference artifact, work links. |
| #4600 | The verbatim-rationale precedent decision 4 generalizes. |
| #4618, #4624 | The pin bug and the rationale capture this ADR depends on. |
| ADR 049, ADR 053 | The session surface and execution model this composes with. |
