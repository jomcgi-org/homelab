"""Temporal worker skeleton (ADR agents/015).

A worker polls one task queue and runs the workflows/activities registered with
it. This unit ships the *skeleton* only: ``run_worker`` accepts injected
workflows/activities so Wavefront 3 can supply real ones (and an auto-discovery
walker — see :func:`discover_workflows`) without touching this file's contract.

Entrypoint (wired to an image in Wavefront 3)::

    python -m projects.lakehouse.orchestrator.worker

reads the ``TASK_QUEUE`` env var and runs a worker against it.
"""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import Sequence
from typing import Any

import temporalio.client
import temporalio.worker

from projects.lakehouse.orchestrator.client import get_client
from projects.lakehouse.orchestrator.schedules import register_schedules

logger = logging.getLogger(__name__)


def discover_workflows() -> list[type]:
    """Auto-discover workflow classes for registration.

    STUB — returns ``[]`` in this skeleton unit. Wavefront 3 will implement
    package-walking discovery (import every module under
    ``projects.lakehouse.orchestrator.workflows`` and collect classes decorated
    with ``@temporalio.workflow.defn``) so worker Deployments need only set
    ``TASK_QUEUE`` rather than enumerate workflows by hand. Kept as a named
    seam so callers and the W3 image unit can target a stable symbol.
    """
    return []


async def run_worker(
    task_queue: str,
    *,
    workflows: Sequence[type] | None = None,
    activities: Sequence[Any] | None = None,
    client: temporalio.client.Client | None = None,
    seed_schedules: bool = False,
) -> None:
    """Build a Temporal worker for ``task_queue`` and run it until cancelled.

    Connects a client via :func:`get_client` when one isn't injected (tests pass
    a mock). ``workflows``/``activities`` default to empty lists — a worker with
    nothing registered is valid and simply polls; Wavefront 3 supplies real
    registrations (or ``discover_workflows()``).

    ``seed_schedules`` (set by the real entrypoint, off for tests) idempotently
    registers the lakehouse Temporal Schedules on boot — the cadence that drives
    IcebergBatchCommit / BuildServingArtifact / TagRotation / gap-drain-sweep.
    Without it those workflows never run on their own (they only fire when started
    by hand). Registration is best-effort: a failure is logged but does not stop
    the worker from serving its queue.
    """
    worker_client = await get_client() if client is None else client
    if seed_schedules:
        try:
            await register_schedules(worker_client)
        except Exception:
            logger.exception(
                "Temporal schedule registration failed; worker will still run %s",
                task_queue,
            )
    worker = temporalio.worker.Worker(
        worker_client,
        task_queue=task_queue,
        workflows=list(workflows) if workflows is not None else [],
        activities=list(activities) if activities is not None else [],
    )
    await worker.run()


def _main() -> None:
    """Console entrypoint: run a worker for ``$TASK_QUEUE``."""
    task_queue = os.environ.get("TASK_QUEUE")
    if not task_queue:
        raise SystemExit("TASK_QUEUE environment variable is required")
    asyncio.run(run_worker(task_queue))


if __name__ == "__main__":
    _main()
