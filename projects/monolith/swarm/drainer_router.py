from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, HTTPException, Response
from sqlalchemy import text
from sqlmodel import Session

from agent.config import drainer_enabled, load_drainer_settings
from core.db import get_engine
from swarm import runtime
from swarm.drainer import drain_cycle
from swarm.queues import drainer_queue

router = APIRouter(prefix="/internal/agent", tags=["agent-internal"])
logger = logging.getLogger(__name__)

# Query workflow_status keyed on updated_at (last checkpointed step), not
# created_at, because a long healthy drain_cycle has a very old created_at.
# A drain_cycle updates updated_at on every step, so the staleness bound
# must be generous: turn_timeout_seconds + (max_jobs_per_cycle * 60) + 600 seconds.
_REAPER_STALENESS_MARGIN_SECONDS = 600


def _reap_stale_drain_cycles(dbos) -> int:
    """Cancel stale PENDING drain cycles that no executor will advance.

    Returns the count of workflows reaped.
    """
    settings = load_drainer_settings()
    staleness_threshold_seconds = (
        settings.turn_timeout_seconds
        + (settings.max_jobs_per_cycle * 60)
        + _REAPER_STALENESS_MARGIN_SECONDS
    )
    stale_before = datetime.now(timezone.utc) - timedelta(
        seconds=staleness_threshold_seconds
    )

    # Query DBOS workflow_status table for stale PENDING drain cycles.
    # The drainer queue name is always "drainer" (from swarm/queues.py).
    sql = text(
        """
        SELECT workflow_id
          FROM dbos.workflow_status
         WHERE name LIKE '%drain_cycle'
           AND status = 'PENDING'
           AND queue_name = 'drainer'
           AND updated_at < :stale_before
         ORDER BY updated_at ASC
        """
    )

    reaped = 0
    try:
        with Session(get_engine()) as session:
            rows = session.execute(sql, {"stale_before": stale_before}).fetchall()

        for row in rows:
            workflow_id = row[0]
            age_seconds = (
                datetime.now(timezone.utc)
                - stale_before
                + timedelta(seconds=staleness_threshold_seconds)
            ).total_seconds()

            try:
                # Sync cancel_workflow is correct here: trigger_drain is a plain def,
                # not an async handler, so no event loop is running. The async version
                # would be wrong because DBOS's async call checks for an active loop
                # and raises if one is found.
                dbos.cancel_workflow(workflow_id, cancel_children=True)

                # Reap guest sessions that were spawned by the workflow.
                # reap_sessions_for_workflow is async, so we run it in a new event loop.
                from agent_sessions.api import reap_sessions_for_workflow

                asyncio.run(reap_sessions_for_workflow(workflow_id))

                logger.warning(
                    "qwen drainer reaped stale workflow %s (%.0f seconds stale)",
                    workflow_id,
                    age_seconds,
                )
                reaped += 1
            except Exception:  # noqa: BLE001
                logger.warning(
                    "qwen drainer failed to reap workflow %s",
                    workflow_id,
                    exc_info=True,
                )
    except Exception:  # noqa: BLE001
        logger.warning(
            "qwen drainer workflow query failed",
            exc_info=True,
        )

    return reaped


def _has_live_drain_cycle() -> bool:
    """Check if a PENDING or ENQUEUED drain_cycle exists."""
    sql = text(
        """
        SELECT 1
          FROM dbos.workflow_status
         WHERE name LIKE '%drain_cycle'
           AND status IN ('PENDING', 'ENQUEUED')
           AND queue_name = 'drainer'
         LIMIT 1
        """
    )
    try:
        with Session(get_engine()) as session:
            result = session.execute(sql).scalar()
            return result is not None
    except Exception:  # noqa: BLE001
        logger.warning(
            "qwen drainer status check failed",
            exc_info=True,
        )
        # On error, assume there is a live cycle to be conservative and avoid
        # enqueueing duplicates.
        return True


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
