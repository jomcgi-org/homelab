"""Worker entrypoint — discover + run all lakehouse workflows for ``$TASK_QUEUE``.

    python -m projects.lakehouse.orchestrator.workflows.run

Reads ``TASK_QUEUE`` from the environment, discovers every workflow/activity
registered under :mod:`projects.lakehouse.orchestrator.workflows` via the
package loader, and runs a Temporal worker against that queue. One image, many
Deployments differentiated only by ``TASK_QUEUE`` (ADR 015 §"Worker pools, not
the orchestrator"). The Wavefront-3 worker image (``projects/lakehouse/image``)
uses this module as its command.

This module is also where the lakehouse Temporal **Schedules** are seeded: it
sits above both the worker skeleton (``orchestrator.worker``) and the
``orchestrator.schedules`` package, so composing "register schedules, then run a
worker" here keeps the worker a dependency-cycle-free leaf.
"""

from __future__ import annotations

import asyncio
import logging
import os

from projects.lakehouse.orchestrator.client import get_client
from projects.lakehouse.orchestrator.schedules import register_schedules
from projects.lakehouse.orchestrator.worker import run_worker
from projects.lakehouse.orchestrator.workflows import (
    discover_activities,
    discover_workflows,
)

logger = logging.getLogger(__name__)


async def _run() -> None:
    """Seed schedules (best-effort) then run a worker for ``$TASK_QUEUE``.

    The client is connected here and injected into both ``register_schedules``
    and ``run_worker`` so they share one connection. Schedule registration is
    idempotent (``register_schedules`` swallows AlreadyRunning, so concurrent
    pool boots converge) and best-effort: a Temporal hiccup at boot is logged but
    must NOT stop the worker from serving its queue — crash-looping the whole
    pool because a schedule couldn't be (re)created is strictly worse than
    running without the (already-registered) schedules being refreshed.
    """
    task_queue = os.environ.get("TASK_QUEUE")
    if not task_queue:
        raise SystemExit("TASK_QUEUE environment variable is required")

    client = await get_client()
    try:
        await register_schedules(client)
    except Exception:
        logger.exception(
            "Temporal schedule registration failed; worker for %s will still run",
            task_queue,
        )

    await run_worker(
        task_queue,
        workflows=discover_workflows(),
        activities=discover_activities(),
        client=client,
    )


def _main() -> None:
    asyncio.run(_run())


if __name__ == "__main__":
    _main()
