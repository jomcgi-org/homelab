"""GapDrainWorkflow + GapDrainSweepWorkflow (ADR agents/015).

A **parallel / shadow** Temporal definition of the gap-research drain. It models
the ADR-015 "workflow identity = work identity" pattern: a gap is researched by a
workflow keyed to the gap itself (``gap-drain-{gap_id}``), and a cron sweep
discovers ready gaps and starts their (deterministically-IDed) child workflows,
swallowing ``WorkflowAlreadyStartedError`` so concurrent triggers are no-ops.

SCOPE GUARD — read before wiring anything to production
-------------------------------------------------------
This is a shadow definition only. It does **not** cut over production gap
dispatch from the existing ``projects/agent_platform/orchestrator`` /
``projects/monolith/knowledge/research_handler.py`` pipeline, and it must not
compete with it:

* :func:`find_ready_gaps` is strictly **read-only** — a ``SELECT`` against
  ``knowledge.gaps``. It does NOT claim, lock, or mutate rows. The live
  ``research_handler`` is the only writer that flips ``classified -> researching``;
  this sweep never races it for ownership because it never writes.
* :func:`run_research_session` is an **invoke-the-existing-harness skeleton**: it
  shells out to the existing inference path (Claude Code CLI subprocess per
  ``feedback_claude_cli_subprocess_for_tos``, or Goose) with the gap context, and
  publishes a gap event. It deliberately does **not** wire web tooling — the
  harness handles web access.

Actual production cutover (replacing the orchestrator's gap dispatch with these
workflows, and having the sweep claim/advance gap state) is a deliberate
follow-up unit, not this run. See ``docs/plans/event-sourced-impl-status/
WF-GAP-DRAIN.md``.
"""

from __future__ import annotations

import asyncio
import json
import os
from dataclasses import dataclass, field
from datetime import timedelta

from temporalio import activity, workflow
from temporalio.exceptions import WorkflowAlreadyStartedError

with workflow.unsafe.imports_passed_through():
    from projects.lakehouse.events.envelope import build_envelope
    from projects.lakehouse.events.publish import publish_event, subject_for
    from projects.lakehouse.nats_client.client import NatsClient

# --- constants ------------------------------------------------------------

ENTITY_TYPE = "gap"
PRODUCER = "lakehouse.gap_drain"

# Deterministic per-gap workflow ID prefix (ADR 015 §"Workflow identity = work
# identity"). The caller (sweep / event / manual) sets id=f"{WORKFLOW_ID_PREFIX}{gap_id}".
WORKFLOW_ID_PREFIX = "gap-drain-"

# Ceiling on gaps the sweep dispatches per tick. Mirrors the live research
# pipeline's batch ceiling (knowledge/research_handler RESEARCH_BATCH_SIZE) so
# the shadow definition has the same shape; read-only so it never competes.
DEFAULT_SWEEP_LIMIT = 10

# Env var naming the harness command (Claude CLI subprocess or Goose). Left
# unset in this skeleton run; the activity records a skipped result rather than
# failing so the workflow definitions are exercisable without the harness wired.
HARNESS_COMMAND_ENV = "GAP_RESEARCH_HARNESS_CMD"


def workflow_id_for(gap_id: str) -> str:
    """Deterministic workflow ID for a gap's drain workflow (ADR 015)."""
    return f"{WORKFLOW_ID_PREFIX}{gap_id}"


@dataclass
class GapContext:
    """Minimal gap context handed to the research harness.

    Carries only what the harness needs to research the gap; the full gap row
    stays in Postgres. ``gap_id`` is the stable per-gap identity (the gap row's
    primary key, as a string) used for the workflow ID and the event entity_id.
    """

    gap_id: str
    term: str
    context: str = ""
    gap_class: str | None = None


@dataclass
class GapDrainResult:
    """Outcome of a single gap-drain workflow run."""

    gap_id: str
    status: str  # "researched" | "skipped" | "failed"
    detail: str = ""


@dataclass
class SweepResult:
    """Outcome of a sweep tick: how many gaps were found / dispatched."""

    found: int = 0
    dispatched: int = 0
    already_running: int = 0
    gap_ids: list[str] = field(default_factory=list)


# --- activities -----------------------------------------------------------


def _resolve_database_url() -> str:
    """Read ``DATABASE_URL`` (monolith-pg, read-only) for the gap sweep query."""
    url = os.environ.get("DATABASE_URL")
    if not url or not url.strip():
        raise RuntimeError(
            "DATABASE_URL is required for find_ready_gaps "
            "(monolith-pg read-only credentials)"
        )
    return url.strip().replace("postgresql+psycopg://", "postgresql://", 1)


@activity.defn
async def find_ready_gaps(limit: int = DEFAULT_SWEEP_LIMIT) -> list[dict]:
    """Read gaps ready for research — **read-only**, no claim, no mutation.

    Mirrors the live ``research_handler`` selector (``gap_class='external' AND
    state='classified' AND deleted_at IS NULL``, ordered by id) but performs only
    a ``SELECT``: it never flips state to ``researching`` and so never competes
    with the live orchestrator for gap ownership (see module SCOPE GUARD).

    Returns plain dicts (Temporal-serializable). psycopg is imported inside the
    activity (outside the workflow sandbox).
    """
    import psycopg

    dsn = _resolve_database_url()
    rows: list[dict] = []
    with psycopg.connect(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, term, context, gap_class
                FROM knowledge.gaps
                WHERE deleted_at IS NULL
                  AND gap_class = 'external'
                  AND state = 'classified'
                ORDER BY id
                LIMIT %(limit)s
                """,
                {"limit": limit},
            )
            for gap_id, term, context, gap_class in cur.fetchall():
                rows.append(
                    {
                        "gap_id": str(gap_id),
                        "term": term,
                        "context": context or "",
                        "gap_class": gap_class,
                    }
                )
    return rows


@activity.defn
async def run_research_session(gap_context: GapContext) -> GapDrainResult:
    """Invoke the EXISTING research harness for a gap, then publish a gap event.

    Skeleton: shells out to the existing inference path (Claude Code CLI
    subprocess per ``feedback_claude_cli_subprocess_for_tos``, or Goose) named by
    ``$GAP_RESEARCH_HARNESS_CMD``, passing the gap context as JSON on argv. It
    does **not** wire web tooling — the harness owns web access. When the harness
    command is unset (this skeleton run), the session is recorded as ``skipped``
    rather than failing, so the workflow definitions are exercisable end-to-end
    without a live harness.

    After the session, publishes a ``gap`` ``processed`` domain event to
    ``events.knowledge.gap`` (``Nats-Msg-Id`` dedup via the publish helper).
    """
    harness_cmd = os.environ.get(HARNESS_COMMAND_ENV, "").strip()
    if not harness_cmd:
        status, detail = "skipped", "no harness command configured (skeleton run)"
    else:
        gap_json = json.dumps(
            {
                "gap_id": gap_context.gap_id,
                "term": gap_context.term,
                "context": gap_context.context,
                "gap_class": gap_context.gap_class,
            }
        )
        # Heartbeat so a long research session keeps its activity lease alive and
        # a dead worker's in-flight work is detected/retried (ADR 015).
        activity.heartbeat("research session starting")
        proc = await asyncio.create_subprocess_exec(
            harness_cmd,
            gap_json,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        if proc.returncode == 0:
            status = "researched"
            detail = stdout.decode("utf-8", "replace").strip()[:2000]
        else:
            status = "failed"
            detail = stderr.decode("utf-8", "replace").strip()[:2000]

    # Publish a gap event recording the research outcome. Event version 1 is the
    # genesis for this shadow producer; a production cutover would derive a
    # monotonic per-gap version (ADR 017 §versioning-per-entity).
    nats_client = NatsClient()
    await nats_client.connect()
    try:
        envelope = build_envelope(
            entity_type=ENTITY_TYPE,
            entity_id=gap_context.gap_id,
            event_type="processed",
            event_version=1,
            producer=PRODUCER,
            payload={
                "term": gap_context.term,
                "gap_class": gap_context.gap_class,
                "state": "researching",
                "status": status,
            },
        )
        await publish_event(nats_client, envelope, subject=subject_for(envelope))
    finally:
        await nats_client.close()

    return GapDrainResult(gap_id=gap_context.gap_id, status=status, detail=detail)


# --- workflows ------------------------------------------------------------


@workflow.defn
class GapDrainWorkflow:
    """Research a single gap (workflow identity = the gap, ADR 015).

    The caller sets the workflow ID to ``gap-drain-{gap_id}`` (see
    :func:`workflow_id_for`); this run body does not set its own ID. Native
    Temporal dedup makes concurrent triggers (event / cron sweep / manual) for
    the same gap collapse onto one execution.
    """

    @workflow.run
    async def run(self, gap_context: GapContext) -> GapDrainResult:
        return await workflow.execute_activity(
            run_research_session,
            gap_context,
            start_to_close_timeout=timedelta(hours=1),
            heartbeat_timeout=timedelta(minutes=10),
            retry_policy=workflow.RetryPolicy(
                initial_interval=timedelta(seconds=5),
                backoff_coefficient=2.0,
                # Cap backoff at 1h+ for external-dependency outages
                # (ADR 015 risk: activity retry storms).
                maximum_interval=timedelta(hours=1),
                maximum_attempts=3,
            ),
        )


@workflow.defn
class GapDrainSweepWorkflow:
    """Cron sweep: find ready gaps and start their per-gap drain workflows.

    For each ready gap, starts a :class:`GapDrainWorkflow` child with the
    deterministic ID ``gap-drain-{gap_id}``, swallowing
    ``WorkflowAlreadyStartedError`` so a gap already being researched is a silent
    no-op (ADR 015 idempotent cron sweep). ``ParentClosePolicy.ABANDON`` lets the
    short-lived sweep return immediately while the children keep running.
    """

    @workflow.run
    async def run(self, limit: int = DEFAULT_SWEEP_LIMIT) -> SweepResult:
        ready = await workflow.execute_activity(
            find_ready_gaps,
            limit,
            start_to_close_timeout=timedelta(minutes=2),
            retry_policy=workflow.RetryPolicy(
                initial_interval=timedelta(seconds=1),
                backoff_coefficient=2.0,
                maximum_interval=timedelta(minutes=1),
                maximum_attempts=5,
            ),
        )

        result = SweepResult(found=len(ready))
        for gap in ready:
            gap_context = GapContext(
                gap_id=gap["gap_id"],
                term=gap["term"],
                context=gap.get("context", ""),
                gap_class=gap.get("gap_class"),
            )
            try:
                await workflow.start_child_workflow(
                    GapDrainWorkflow.run,
                    gap_context,
                    id=workflow_id_for(gap["gap_id"]),
                    parent_close_policy=workflow.ParentClosePolicy.ABANDON,
                )
                result.dispatched += 1
                result.gap_ids.append(gap["gap_id"])
            except WorkflowAlreadyStartedError:
                # A drain for this gap is already running — idempotent no-op.
                result.already_running += 1

        return result


# Auto-discovery exports (read by the workflows package loader).
WORKFLOWS = [GapDrainWorkflow, GapDrainSweepWorkflow]
ACTIVITIES = [find_ready_gaps, run_research_session]
