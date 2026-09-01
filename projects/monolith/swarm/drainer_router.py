from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, HTTPException, Response

from agent.config import drainer_enabled, load_drainer_settings
from swarm import runtime
from swarm.drainer import drain_cycle
from swarm.queues import drainer_queue

router = APIRouter(prefix="/internal/agent", tags=["agent-internal"])
logger = logging.getLogger(__name__)

# DBOS workflow queue name for the drainer (must match swarm/queues.py).
_DRAINER_QUEUE_NAME = "drainer"

# How long a PENDING cycle may record NO step checkpoint before it is treated
# as wedged.
#
# This is a per-STEP bound, not a per-cycle one, and that distinction is the
# whole point. The first version of this reaper keyed on
# workflow_status.updated_at and so had to cover a cycle's entire runtime
# (turn_timeout + max_jobs_per_cycle * 60 + margin = 3300s). updated_at turned
# out not to advance on step checkpoints at all, so the signal moved to
# max(operation_outputs.completed_at_epoch_ms), but the old cycle-sized
# arithmetic came along unchanged. That left detection at 55 minutes, during
# which one dead workflow holds the concurrency-1 slot and the entire queue
# stalls behind it. Observed live: a wedge sat 39 minutes with 162 jobs waiting.
#
# The floor is the longest a HEALTHY cycle can go between checkpoints, which is
# its longest single step. _await_turn checkpoints poll_turn every 5 seconds,
# so the loop is not the constraint. start_agent_session is: it is one step,
# and inside it create_session can walk the capacity backoff ladder in
# agent_sessions/transport.py, which sums to roughly 19 minutes when the
# EmberVM workload cap is full. Reaping faster than that would cancel a cycle
# that is legitimately waiting for a slot, which is exactly the mistake this
# reaper exists to avoid making.
#
# 1800s is that ~19 minute ceiling plus headroom, and roughly halves detection
# time versus 3300s. It is a plain constant rather than a formula because the
# quantity it must exceed has nothing to do with turn_timeout or
# max_jobs_per_cycle; tying it to those was the original error.
_REAPER_STALENESS_SECONDS = 1800


def _reap_stale_drain_cycles(dbos) -> int:
    """Cancel stale PENDING drain cycles that no executor will advance.

    Uses the last recorded step activity as the staleness signal, not updated_at.
    updated_at only advances on status transitions (enqueue, dequeue, completion, cancel),
    not on step checkpoints. Step activity is read from DBOS.list_workflow_steps().

    Returns the count of workflows reaped.
    """
    staleness_threshold_seconds = _REAPER_STALENESS_SECONDS
    # updated_at in dbos.workflow_status is bigint epoch milliseconds.
    stale_before_ms = int((time.time() - staleness_threshold_seconds) * 1000)
    now_ms = int(time.time() * 1000)

    reaped = 0
    try:
        # Query DBOS workflow_status for PENDING drain cycles.
        workflows = dbos.list_workflows(
            name="drain_cycle",
            queue_name=_DRAINER_QUEUE_NAME,
            status="PENDING",
            load_input=False,
            load_output=False,
        )

        for workflow in workflows:
            workflow_uuid = workflow.workflow_id
            updated_at_ms = workflow.updated_at

            # Compute last activity as max of workflow updated_at and most recent step completion.
            # A freshly dequeued workflow has no steps yet, so updated_at is needed.
            last_activity_ms = updated_at_ms
            try:
                steps = dbos.list_workflow_steps(
                    workflow_uuid,
                    load_output=False,
                )
                if steps:
                    # Find the maximum completed_at_epoch_ms among all steps.
                    max_step_completed_ms = max(
                        (s.get("completed_at_epoch_ms") or 0) for s in steps
                    )
                    last_activity_ms = max(last_activity_ms, max_step_completed_ms)
            except Exception:  # noqa: BLE001
                # Skip this workflow rather than falling back to updated_at.
                # updated_at alone is the signal this whole function exists to
                # stop trusting: it is frozen at dequeue for a healthy running
                # cycle, so falling back to it here would reap exactly the
                # long healthy cycles the step signal was added to protect.
                # Leaving a genuinely wedged cycle for the next tick is cheap;
                # cancelling a live one destroys its guest and re-runs its job.
                logger.warning(
                    "Luna drainer could not read steps for %s, skipping reap",
                    workflow_uuid,
                    exc_info=True,
                )
                continue

            # Check if this workflow is stale based on last activity.
            if last_activity_ms < stale_before_ms:
                age_ms = now_ms - last_activity_ms
                age_seconds = age_ms / 1000

                try:
                    # Sync cancel_workflow is correct here: trigger_drain is a plain def,
                    # not an async handler, so no event loop is running. The async version
                    # would be wrong because DBOS's async call checks for an active loop
                    # and raises if one is found.
                    dbos.cancel_workflow(workflow_uuid, cancel_children=True)

                    # Reap guest sessions that were spawned by the workflow.
                    # reap_sessions_for_workflow is async, so we run it in a new event loop.
                    from agent_sessions.api import reap_sessions_for_workflow

                    asyncio.run(reap_sessions_for_workflow(workflow_uuid))

                    logger.warning(
                        "Luna drainer reaped stale workflow %s (%.0f seconds stale)",
                        workflow_uuid,
                        age_seconds,
                    )
                    reaped += 1
                except Exception:  # noqa: BLE001
                    logger.warning(
                        "Luna drainer failed to reap workflow %s",
                        workflow_uuid,
                        exc_info=True,
                    )
    except Exception:  # noqa: BLE001
        logger.warning(
            "Luna drainer workflow query failed",
            exc_info=True,
        )

    return reaped + _reap_version_stranded_cycles(dbos)


def _current_app_version() -> str:
    """The running DBOS app version, or "" when it cannot be resolved.

    Mirrors _server_app_version in swarm/router.py, including its discipline:
    an unresolvable version returns "" and callers must treat that as "cannot
    tell" rather than as evidence of stranding. That module documents a real
    incident where "unknown" was compared literally and marked every live run
    stranded.
    """
    try:
        from dbos._utils import GlobalParams

        return GlobalParams.app_version or ""
    except Exception:  # noqa: BLE001
        logger.warning("could not read the DBOS app version", exc_info=True)
        return ""


def _reap_version_stranded_cycles(dbos) -> int:
    """Cancel ENQUEUED cycles stamped with a version nothing will dequeue.

    A queued workflow carries the app_version of the process that enqueued it,
    and the dequeue query filters on application_version equalling the worker's
    own. So an ENQUEUED row left behind by a previous image is undequeuable:
    no PENDING row exists for the staleness reaper to find, yet
    _has_live_drain_cycle counts it regardless of version, so every tick
    returns already_queued forever. Silent permanent stall, and the only other
    exit is editing the row by hand, since resume re-enqueues without changing
    the version.

    Chaining widens the window that produces one of these. A cycle that
    finishes during the pod's termination grace period still enqueues its
    successor, while the queue poller threads are daemons and are already gone,
    so nothing dequeues it before the new image takes over.

    No staleness timer here on purpose: a version mismatch is not slowness, it
    is a row no worker will ever claim, so it is dead the moment it is
    observed.
    """
    current_version = _current_app_version()
    if not current_version:
        # Cannot tell, so do not guess. Cancelling on an unresolvable version
        # would reap healthy queued work.
        return 0

    reaped = 0
    try:
        workflows = dbos.list_workflows(
            name="drain_cycle",
            queue_name=_DRAINER_QUEUE_NAME,
            status="ENQUEUED",
            load_input=False,
            load_output=False,
        )
        for workflow in workflows:
            version = getattr(workflow, "app_version", None) or getattr(
                workflow, "application_version", None
            )
            if not version or version == current_version:
                continue
            try:
                dbos.cancel_workflow(workflow.workflow_id, cancel_children=True)
                logger.warning(
                    "Luna drainer cancelled version-stranded cycle %s "
                    "(enqueued at version %s, running %s)",
                    workflow.workflow_id,
                    version,
                    current_version,
                )
                reaped += 1
            except Exception:  # noqa: BLE001
                logger.warning(
                    "Luna drainer failed to cancel stranded cycle %s",
                    workflow.workflow_id,
                    exc_info=True,
                )
    except Exception:  # noqa: BLE001
        logger.warning("Luna drainer stranded-cycle query failed", exc_info=True)

    return reaped


def _has_live_drain_cycle() -> bool:
    """Check if a PENDING or ENQUEUED drain_cycle exists.

    Returns False on database errors to fail open: a spurious duplicate enqueue
    is cheap and self-corrects (concurrency is 1), while a false positive stalls
    the drainer permanently.
    """
    try:
        dbos = runtime.init_dbos()
        if dbos is None:
            return False
        workflows = dbos.list_workflows(
            name="drain_cycle",
            queue_name=_DRAINER_QUEUE_NAME,
            status=["PENDING", "ENQUEUED"],
            limit=1,
            load_input=False,
            load_output=False,
        )
        return len(workflows) > 0
    except Exception:  # noqa: BLE001
        logger.warning(
            "Luna drainer status check failed",
            exc_info=True,
        )
        # On error, assume there is NOT a live cycle to be conservative and allow
        # the drainer to at least attempt to enqueue. A false negative (enqueueing
        # a duplicate) is self-correcting; a false positive (permanent stall) is not.
        return False


@router.post("/drain", status_code=202)
def trigger_drain(response: Response) -> dict:
    if not drainer_enabled():
        response.status_code = 200
        return {"status": "disabled"}
    if not runtime.is_launched():
        raise HTTPException(
            status_code=503, detail="DBOS is not launched on this replica"
        )
    dbos = runtime.init_dbos()
    if dbos is None:
        raise HTTPException(status_code=503, detail="DBOS is not configured")

    # Resolve the queue BEFORE any early return. drainer_queue() is what
    # constructs the DBOS Queue object and so registers it in the DBOS
    # registry; the queue thread only polls queues that are registered on this
    # process. If the "already_queued" return below skipped this call, a pod
    # that rolled with a backlog would never register the queue, never dequeue
    # the backlog, and therefore keep seeing a live cycle forever. That is a
    # self-reinforcing stall, and it is the same failure this endpoint exists
    # to prevent.
    queue = drainer_queue()

    # Reap stale PENDING cycles that will never advance.
    _reap_stale_drain_cycles(dbos)

    # Make enqueue idempotent: do not stack another cycle if one is already live.
    # The CronWorkflow fires every 15 minutes regardless of whether the previous
    # cycle finished, so without this guard every tick during a slow-but-healthy
    # cycle would stack another workflow that contends for the single concurrency
    # slot, creating the 52-deep pileup that blocked draining for hours.
    if _has_live_drain_cycle():
        response.status_code = 200
        return {"status": "already_queued"}

    queue.enqueue(drain_cycle)
    return {"status": "started"}
