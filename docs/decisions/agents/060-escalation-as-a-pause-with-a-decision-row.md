# ADR 060: Escalation as a Pause, Not a Return, With a Decision Row

**Author:** jomcgi
**Status:** Draft
**Created:** 2026-08-22
**Relates to:** [054 - The Run View: Pinned Plans, Epistemic Registers, and
Recorded-Not-Inferred Data](054-run-view-pinned-plans-epistemic-registers.md)
(Draft, defined `blocked_on {kind: dependency | human, refs, note}` as a
first-class node state and assigned it no writer, the gap this ADR closes);
[049 - Turn-Granular, Poll-Shaped Agent Session UI](049-turn-granular-poll-shaped-agent-ui.md)
(Accepted, the poll shape `poll_turn` / `_await_turn` this ADR reuses);
[058 - The Voice Companion](058-voice-driven-companion-screen.md) (Accepted,
the `voice_ui_ask` cards this ADR gives a second answer target); [053 - Swarm
Development, Bounded Conductor Orchestration](053-swarm-orchestration-bounded-conductor.md)
(Draft, the execution model this run belongs to); issue #4781 (the DAG as a
mutable artifact, the tracking issue this ADR's follow-on work files under);
issue #3842 (verdict is data, the scope guard decision 5 defers to)

---

## Problem

The agents console redesign (#5041, all six slices shipped 2026-08-22) is
inbox-first: rows state the ask ("Approve push") and the run view draws a
push gate with Approve / Deny. The engine underneath never waits on a human.
`escalated` is a terminal workflow output. `_escalated` (`swarm/workflows.py:138`)
is returned at three call sites in `implement_then_review` (lines 250, 275,
360: no branch movement with no attempts left, a defensive fallthrough with
no commit to review, and a review cycle exhausted with no branch movement on
the send-back retry), and DBOS records the workflow's status as SUCCESS the
moment any of them return. `push_gate` is a synthetic view node built in
`compose_run` (`swarm/view.py:456`) whose `decision` field is a rendered
policy explanation, not a decision anyone made. `blocked_on` is hardcoded
`None` on every node the server emits (`view.py:453,468,493`), so `needs` on
a run can never derive from a human gate: ADR 054 decision 3 named
`blocked_on {kind: dependency | human, refs, note}` as a first-class node
state and left it unwritten.

The only thing the workflow ever waits on is a session turn row, polled every
five seconds: `_await_turn` (`workflows.py:105`) calls `poll_turn` and sleeps
on `DBOS.sleep(POLL_INTERVAL_SECONDS)` (`workflows.py:30,112`). `router.py`
has no decision route. `cancel_run` (`router.py:480`) is the only mutating
precedent the engine has: it reads the actor from the
`Cf-Access-Authenticated-User-Email` header and records it via
`update_workflow_attributes_async`, best-effort, never failing the operation
it is attached to.

The console therefore ships the gate buttons `aria-disabled` with "Decisions
are answered in the session for now", and a follow-up PR
(fix/agents-honest-gate) replaces them with "Open session". ADR 058's voice
`ask` cards answer through `agent_session_send` today for the same reason:
there is nothing else to answer into. Issue #4781 tracks the DAG as a mutable
artifact generally; this ADR is the decision for the one mutation the console
already promises.

---

## Decision

| Aspect | Today | Decided |
| ------ | ----- | ------- |
| Escalation | terminal workflow return, DBOS status SUCCESS | a pause: a decision row is written and the workflow waits on it |
| Human answer path | none; console buttons are disabled | one endpoint, actor recorded the way `cancel_run` records one |
| `blocked_on` | hardcoded `None` on every node | populated from the open decision row when one exists |
| Timeout | none needed, nothing waits | undecided row past `decision_timeout_seconds` escalates today's way |
| Requeue after a decision | n/a | out of scope; a "send back" ends the run, as `review_cycles_exhausted` does today |

### 1. Escalation becomes a pause, recorded as a row and waited on by polling it

At each of its escalate points, `implement_then_review` records a decision
request and waits on it rather than returning `_escalated`. The wait is a
`swarm.swarm_decision` row (`workflow_id`, `node_key`, `kind` (`push_gate` |
`review_escalation` | ...), `options` JSONB, `requested_at`, `decided_at`,
`decision`, `note`, `actor_subject`, `actor_authority`), following the naming
and shape of the existing `swarm.swarm_task` / `swarm.swarm_plan_version` /
`swarm.swarm_conductor_call` tables in
`chart/migrations/20260813000000_swarm_recording_schema.sql`. The workflow
polls it on the same `DBOS.sleep(POLL_INTERVAL_SECONDS)` cadence `_await_turn`
already uses.

The rejected alternative is a DBOS `recv` keyed by workflow id, with the row
as the durable record behind it. No `DBOS.recv` or `DBOS.send` call exists
anywhere in the monolith today; this ADR would be the first. `recv` is a
second wait primitive next to the poll loop `_await_turn` already runs, for a
codebase whose one existing rule about waiting (ADR 049) is poll-shaped
end to end. A row read on a timer is checkable in SQL by anyone, live, with
no replay concerns; a `recv` requires either trusting DBOS's own recovery
semantics to redeliver a message sent while the workflow was down, or writing
the row anyway to have something to check against, at which point the `recv`
call is decoration on the same row and buys nothing the poll does not
already have. An event is not an audit row: `send`/`recv` is a signal, and
the decided-by, decided-at, and actor fields this feature exists to produce
have to live somewhere durable regardless. Symmetry with the existing wait,
and one fewer mechanism a reader has to hold in their head, decide it.

### 2. One endpoint, mirroring the `cancel_run` precedent

`POST /api/swarm/runs/{workflow_id}/nodes/{node_key}/decision` with
`{decision, note}`. The decision is validated against the open row's
`options`. The actor is recorded from `Cf-Access-Authenticated-User-Email`,
the same header and the same best-effort-recording discipline `cancel_run`
already established (`router.py:497`: an actor-recording failure must not
fail the mutating call it describes). A call with no open decision row for
that node returns 409. A repeat of the same decision on an already-decided
row is idempotent, matching a human who double-clicks Approve rather than
punishing them for it.

The MCP tool `agent_run_decide` (or an extension of the existing run tool
family in `agent_sessions/mcp.py`) calls the same function, so ADR 058's
`voice_ui_ask` cards can target it directly instead of going through
free-text session parsing. `agent_session_send` stays exactly as it is for
free-text answers; nothing about this decision narrows what a session
message can do.

### 3. `compose_run` stops hardcoding `blocked_on`

For a node with an open decision row, `compose_run` populates
`blocked_on {kind: "human", note, since, decision_id}` and `needs` on the run
derives from it, closing the gap ADR 054 decision 3 left open. For a run from
before this shipped, or a run whose escalation predates the row (nothing
back-fills history), the push gate stays exactly as synthetic as it is
today: a rendered policy explanation, not a decision anyone can act on. The
two states are visually distinguishable by construction, because one carries
a `decision_id` and the other does not; the console does not need a second
flag to tell them apart.

### 4. An undecided row times out the same way an unwatched gate does today

A decision row past `plan.decision_timeout_seconds` (default 24h, following
`pin_plan`'s naming convention alongside `turn_timeout_seconds` in
`swarm/steps.py:25`) resolves to `decision: "expired"` and the workflow takes
today's escalate path: terminal, DBOS status SUCCESS. This is not a new
failure mode; it is the same one a forgotten gate produces today, given a
name and a recorded cause instead of silence. Without it, a pause turns an
abandoned run into a workflow that never completes, which is a worse
property than the terminal escalation it replaces.

### 5. Requeue is explicitly out of scope

A "send back" decision ends the run, with the note recorded on the row, the
same way `review_cycles_exhausted` ends a run today when `max_review_cycles`
is spent on a `request_changes` verdict (`workflows.py:371`): terminal, not a
resume. This ADR gives escalation a wait it did not have; it does not give
the workflow a way to re-enter its own loop from a decision, which is the
attempt-requeue direction #3842 already scoped and this ADR is not
duplicating.

---

## Consequences

The workflow now runs two poll loops instead of one: `_await_turn` for
session turns and the new one for decisions. Both share the same primitive
and the same `POLL_INTERVAL_SECONDS`, so this is a second call site, not a
second mechanism.

The decision row is the first human-written fact the run payload carries.
Every other epistemic register ADR 054 defined is engine fact derived from
mechanical state or agent testimony quoted verbatim; a decision row is a
human's own claim, recorded the way `cancel_run`'s actor already is, and
rendered as fact because the actor and the decision are exactly what
happened, not an inference about it.

The console's `aria-disabled` gate buttons (shipped disabled in #5041, with
fix/agents-honest-gate swapping them for "Open session" in the interim) come
back as real buttons wired to the new endpoint. That is a console-side
change, sequenced after the engine and API slices below, not part of this
ADR's decision.

ADR 058's `voice_ui_ask` gains a second target: a card can now resolve either
through `agent_session_send` (existing) or through the decision endpoint
(new). Both remain peers under ADR 058 decision 4; neither is privileged.

Sequencing: an engine slice (the row, the wait, the timeout), then an API and
MCP slice (the endpoint, `agent_run_decide`), then a console slice (buttons
wired to the endpoint). Each becomes an issue under #4781.

---

## Risks

| Risk | Likelihood | Impact | Mitigation |
| ---- | ---------- | ------ | ---------- |
| A workflow pauses on a row nobody looks at | Medium | Medium | `blocked_on` plus the inbox surfaces it; decision 4's timeout is the backstop that still ends the run |
| DBOS recovery replays the wait after a restart | Low | Medium | The row is the idempotency key: replay re-reads the same open row rather than minting a new decision request |
| RBAC gap on the new endpoint | Low | High | Same gate as `cancel_run`; no new authorization mechanism is introduced |
| A node's `options` drift from what the console renders | Low | Medium | The endpoint validates the submitted decision against the row's own `options`, not against console state |
| Two poll loops double the workflow's `DBOS.sleep` wake-ups under load | Low | Low | Same interval, same primitive; only load-bearing at very high concurrent-run counts, not observed today |

---

## What this does not decide

This ADR decides that escalation pauses and how the pause is recorded and
answered. It does not decide:

- **Requeue.** Whether or how a "send back" decision could re-enter the
  workflow loop instead of ending the run is #3842's scope, not this one.
- **Console visual design** for the now-live gate buttons; #5041 already
  shipped the layout, this ADR only makes the buttons real.
- **Which decision kinds beyond `push_gate` and `review_escalation` exist.**
  The `kind` column is open; new kinds are additive and do not require
  revisiting this ADR.
- **Retention or expiry of decided rows.** `decision_timeout_seconds`
  governs undecided rows only; how long an answered row is kept is
  unaddressed.
- **Whether the decision endpoint is reachable from a channel other than the
  console and voice**, such as Discord. Nothing here forecloses it, and
  nothing here builds it.

---

## References

| Resource | Relevance |
| -------- | --------- |
| ADR 054 decision 3 | Defined `blocked_on` and left it unwritten; this ADR is the writer |
| ADR 049 | The poll shape (`_await_turn`, `POLL_INTERVAL_SECONDS`) this ADR's wait reuses |
| ADR 058 decision 4 | The two peer answer channels (spoken, clicked) the decision endpoint becomes a second target for |
| Issue #4781 | The DAG as a mutable artifact; the engine, API, and console slices file under it |
| Issue #3842 | Verdict is data, the scope guard requeue is deferred to |
| `swarm/workflows.py:138,250,275,360` | `_escalated` and its three call sites in `implement_then_review` |
| `swarm/workflows.py:105,30,112` | `_await_turn`, `POLL_INTERVAL_SECONDS`, `DBOS.sleep`, the wait shape reused |
| `swarm/workflows.py:371` | `review_cycles_exhausted`, the existing terminal-not-resumed precedent for a "send back" |
| `swarm/view.py:343,453,456,468,493` | `compose_run`, the synthetic `push_gate` node, and the hardcoded `blocked_on: None` sites |
| `swarm/router.py:480-517` | `cancel_run`: the actor-header and best-effort-recording precedent the decision endpoint mirrors |
| `swarm/steps.py:10-27` | `pin_plan`, the naming convention `decision_timeout_seconds` follows |
| `chart/migrations/20260813000000_swarm_recording_schema.sql` | The `swarm.swarm_task` / `swarm.swarm_plan_version` / `swarm.swarm_conductor_call` naming the new `swarm.swarm_decision` table follows |
