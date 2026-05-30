"""Gap-ready dispatcher: ``events.knowledge.gap`` ``created`` -> ``GapDrainWorkflow``.

ADR 016 §Architecture / ADR 017 §"Per-consumer interpretation": the gap-drain
dispatcher subscribes to ``events.knowledge.gap`` and filters ``event_type ==
"created"``. On a gap-created event it starts the per-gap drain workflow with the
deterministic ID ``gap-drain-{entity_id}`` on the ``gap-drain`` task queue,
swallowing :class:`temporalio.exceptions.WorkflowAlreadyStartedError` as an
idempotent no-op (ADR 015 §"Workflow identity = work identity": event / cron
sweep / manual triggers all converge on one execution; whichever fires first
wins, the rest are silent no-ops).

The workflow is referenced by **type-name string** (``"GapDrainWorkflow"``), never
imported — exactly as ADR 016 prescribes for dispatchers and mirroring the
``orchestrator.schedules.gap_drain_sweep`` schedule. This keeps the dispatcher a
~30-line adapter with no workflow/activity dependency edge.

SCOPE GUARD — shadow / parallel definition
-------------------------------------------
Like the W3 ``orchestrator.workflows.gap_drain`` shadow workflow this dispatcher
reacts to, this is a **parallel definition only**. Production gap dispatch still
flows through the existing live orchestrator
(``projects/agent_platform/orchestrator`` / ``knowledge/research_handler``).
Cutting the production gap pipeline over to this NATS->Temporal path is a
deliberate follow-up unit, **out of scope for this run** — nothing here is wired
to a Deployment by this unit (the dispatcher Deployment lives in the sibling
W4-CHART unit and the cutover is later still).
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from projects.lakehouse.dispatchers import Dispatcher
from projects.lakehouse.events.publish import SUBJECT_BY_ENTITY
from projects.lakehouse.orchestrator import TaskQueue

if TYPE_CHECKING:  # pragma: no cover - typing only
    import temporalio.client

    from projects.lakehouse.events.envelope import EventEnvelope

logger = logging.getLogger(__name__)

# Subject + durable consumer for the gap stream. Subject comes from the events
# package's canonical entity->subject map (single source of truth, ADR 016
# §"Per-subject topology") so the dispatcher and producers can never drift.
SUBJECT = SUBJECT_BY_ENTITY["gap"]  # "events.knowledge.gap"
DURABLE = "gap-drain-dispatcher"

# Only react to gap *creation* (ADR 017 §"Per-consumer interpretation": the
# gap-drain dispatcher filters event_type == "created"). updated/processed/
# tombstoned events on the same subject are ignored by this consumer.
TRIGGER_EVENT_TYPE = "created"

# Referenced by type-name string, never imported (ADR 016). Matches the W3
# `@workflow.defn class GapDrainWorkflow` registered in
# projects.lakehouse.orchestrator.workflows.gap_drain.
WORKFLOW_TYPE = "GapDrainWorkflow"

# Task queue the per-gap drain workflow runs on (ADR 015 worker pools).
TASK_QUEUE = TaskQueue.GAP_DRAIN.value  # "gap-drain"

# Deterministic per-gap workflow ID prefix (ADR 015 §"Workflow identity = work
# identity"). Mirrors orchestrator.workflows.gap_drain.WORKFLOW_ID_PREFIX, kept
# local so the dispatcher needs no import edge to the workflow module.
WORKFLOW_ID_PREFIX = "gap-drain-"


def workflow_id_for(entity_id: str) -> str:
    """Deterministic workflow ID for a gap's drain workflow: ``gap-drain-{id}``."""
    return f"{WORKFLOW_ID_PREFIX}{entity_id}"


async def handle_gap_created(
    envelope: EventEnvelope,
    temporal_client: temporalio.client.Client,
) -> None:
    """Start ``GapDrainWorkflow`` for a gap-created event (idempotent).

    The run loop has already applied the ``event_type == "created"`` filter, so
    every call here is a genuine gap-creation. Starts the workflow by type-name
    string with the deterministic ID ``gap-drain-{entity_id}`` on the
    ``gap-drain`` task queue. A re-delivered or duplicate event collapses onto
    the existing execution via :class:`WorkflowAlreadyStartedError`, which is
    swallowed (ADR 015 idempotent dispatch).

    The gap context (term/class/etc.) is **not** reconstructed here: the
    dispatcher's job per ADR 016 is the translation hop, not gap business logic.
    The shadow ``GapDrainWorkflow`` reads the gap row itself; the workflow input
    is the gap id wrapped in the workflow's expected payload shape, kept minimal
    and forwarded from the event payload when present.
    """
    # Imported lazily so importing this dispatcher module (and the hermetic
    # loader tests) never requires temporalio to be importable at definition
    # time — only invoking the handler does. The exception lives in
    # temporalio.exceptions (temporalio.client does NOT re-export it in 1.27.x);
    # this is the same symbol the W3 gap_drain workflow imports.
    from temporalio.exceptions import WorkflowAlreadyStartedError

    entity_id = envelope.entity_id
    wf_id = workflow_id_for(entity_id)
    try:
        await temporal_client.start_workflow(
            WORKFLOW_TYPE,
            envelope.payload,
            id=wf_id,
            task_queue=TASK_QUEUE,
        )
        logger.info(
            "dispatched %s id=%s task_queue=%s (gap-created event_id=%s)",
            WORKFLOW_TYPE,
            wf_id,
            TASK_QUEUE,
            getattr(envelope, "event_id", None),
        )
    except WorkflowAlreadyStartedError:
        # A drain for this gap is already running — idempotent no-op (ADR 015).
        logger.debug("%s id=%s already started; idempotent no-op", WORKFLOW_TYPE, wf_id)


DISPATCHERS = [
    Dispatcher(
        subject=SUBJECT,
        durable=DURABLE,
        handle=handle_gap_created,
        event_type=TRIGGER_EVENT_TYPE,
    ),
]
