# WF-GAP-DRAIN — GapDrainWorkflow + GapDrainSweepWorkflow

**Unit:** WF-GAP-DRAIN (part of Wavefront-3 WF-DOMAIN) · **Status:** complete
**Branch:** `feat/lakehouse-wf-domain` · **Author:** lakehouse WF-DOMAIN runner

A **parallel / shadow** Temporal definition of the gap-research drain, modeling
ADR-015 §"Workflow identity = work identity": a gap is researched by a workflow
keyed to the gap (`gap-drain-{gap_id}`), and an idempotent cron sweep discovers
ready gaps and starts their deterministically-IDed child workflows.

## Files (all new, conflict-free per W3-PREP)

- `projects/lakehouse/orchestrator/workflows/gap_drain.py`
- `projects/lakehouse/orchestrator/workflows/gap_drain_test.py`

No BUILD file touched (same glob/superset-deps mechanism as WF-BACKFILL).

## Design

- **`GapDrainWorkflow(gap_context)`** — `@workflow.run` calls the
  `run_research_session` activity. The deterministic workflow ID
  `gap-drain-{gap_id}` is set **by the caller**, not inside `run`
  (`workflow_id_for()` helper). Native Temporal dedup collapses concurrent
  triggers (event / cron sweep / manual) for the same gap onto one execution.
- **`run_research_session(gap_context)` activity** — invokes the **existing**
  research harness: shells out (via `asyncio.create_subprocess_exec`) to the
  inference path named by `$GAP_RESEARCH_HARNESS_CMD` — the Claude Code CLI
  subprocess (per `feedback_claude_cli_subprocess_for_tos`) or Goose — passing the
  gap context as JSON on argv. It **does NOT wire web tooling**; the harness owns
  web access. It heartbeats so a long session keeps its activity lease. After the
  session it publishes a `gap` `processed` event to `events.knowledge.gap`.
- **`GapDrainSweepWorkflow(limit)`** — cron sweep. Calls the read-only
  `find_ready_gaps` activity, then for each gap
  `workflow.start_child_workflow(GapDrainWorkflow.run, gap_context,
id="gap-drain-{gap_id}", parent_close_policy=ABANDON)`, swallowing
  `WorkflowAlreadyStartedError` so a gap already being researched is a silent
  no-op (ADR 015 idempotent cron sweep). `ABANDON` lets the short sweep return
  while children keep running.

## SCOPE GUARD (deliberate — production cutover is a FOLLOW-UP, not this run)

This is a **shadow definition only**. It does **not** cut over production gap
dispatch from the existing `projects/agent_platform/orchestrator` /
`projects/monolith/knowledge/research_handler.py` pipeline, and it must not
compete with it:

- **`find_ready_gaps` is strictly read-only.** It `SELECT`s `knowledge.gaps`
  mirroring the live `research_handler` selector (`gap_class='external' AND
state='classified' AND deleted_at IS NULL`, ordered by `id`, limited to the
  same batch ceiling of 10) but performs **no** `UPDATE/INSERT/DELETE`. It never
  flips `classified → researching`. The live `research_handler` is the only writer
  that claims gap ownership, so this sweep cannot race it. A test asserts the
  executed SQL contains no `UPDATE/INSERT/DELETE`.
- **No web tooling wired** — `run_research_session` is a harness-invoking skeleton;
  the harness handles web access (per the run scope).
- **Event versioning is genesis-only (`event_version=1`)** for this shadow
  producer. A production cutover would derive a monotonic per-gap version (ADR 017
  §versioning-per-entity).

**Follow-up (NOT this run):** actual production cutover — replacing the
orchestrator's gap dispatch with these workflows, having the sweep (or harness)
claim and advance gap state (`classified → researching → …`), deriving monotonic
per-gap event versions, and registering `GapDrainSweepWorkflow` as a Temporal cron
Schedule — is a deliberate separate unit. Until then the live
`research_handler`/orchestrator remains the sole gap dispatcher and the sole writer
of gap state.

## Tests (hermetic — no Temporal server, no DB, no network)

`asyncio.run`-driven. psycopg `connect` faked with canned `knowledge.gaps` rows
(the fake cursor asserts the query is a pure `SELECT`); the harness subprocess and
`NatsClient` are mocked. Coverage: deterministic `workflow_id_for`; `find_ready_gaps`
builds correct dicts (gap_id stringified) and requires `DATABASE_URL`;
`run_research_session` skips cleanly when no harness is configured (still publishes
a `gap-id-v1` outcome event), invokes the harness subprocess with the gap JSON on
argv (no web wiring) on success, and reports `failed` on non-zero exit; both
workflows are `@workflow.defn` + exported; activities are `@activity.defn` +
exported; sweep limit matches the live research batch ceiling.

## Deviations / notes

- Sweep dispatches children with `ParentClosePolicy.ABANDON` so the cron sweep is
  short-lived while the per-gap drains run to completion independently.
