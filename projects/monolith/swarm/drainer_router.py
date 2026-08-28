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

# Staleness margin for the reaper: turn_timeout_seconds + (max_jobs_per_cycle * 60) + 600 seconds.
_REAPER_STALENESS_MARGIN_SECONDS = 600


def _reap_stale_drain_cycles(dbos) -> int:
    """Cancel stale PENDING drain cycles that no executor will advance.

    Uses the last recorded step activity as the staleness signal, not updated_at.
    updated_at only advances on status transitions (enqueue, dequeue, completion, cancel),
    not on step checkpoints. Step activity is read from DBOS.list_workflow_steps().

    Returns the count of workflows reaped.
    """
    settings = load_drainer_settings()
    staleness_threshold_seconds = (
        settings.turn_timeout_seconds
        + (settings.max_jobs_per_cycle * 60)
        + _REAPER_STALENESS_MARGIN_SECONDS
    )
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
                # If we fail to fetch steps, fall back to updated_at alone.
                pass

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
                        "qwen drainer reaped stale workflow %s (%.0f seconds stale)",
                        workflow_uuid,
                        age_seconds,
                    )
                    reaped += 1
                except Exception:  # noqa: BLE001
                    logger.warning(
                        "qwen drainer failed to reap workflow %s",
                        workflow_uuid,
                        exc_info=True,
                    )
    except Exception:  # noqa: BLE001
        logger.warning(
            "qwen drainer workflow query failed",
            exc_info=True,
        )

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
            "qwen drainer status check failed",
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

    drainer_queue().enqueue(drain_cycle)
    return {"status": "started"}
