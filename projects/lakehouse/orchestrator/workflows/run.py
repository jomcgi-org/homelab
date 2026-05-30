"""Worker entrypoint — discover + run all lakehouse workflows for ``$TASK_QUEUE``.

    python -m projects.lakehouse.orchestrator.workflows.run

Reads ``TASK_QUEUE`` from the environment, discovers every workflow/activity
registered under :mod:`projects.lakehouse.orchestrator.workflows` via the
package loader, and runs a Temporal worker against that queue. One image, many
Deployments differentiated only by ``TASK_QUEUE`` (ADR 015 §"Worker pools, not
the orchestrator"). The Wavefront-3 worker image (``projects/lakehouse/image``)
uses this module as its command.
"""

from __future__ import annotations

import asyncio
import os

from projects.lakehouse.orchestrator.worker import run_worker
from projects.lakehouse.orchestrator.workflows import (
    discover_activities,
    discover_workflows,
)


def _main() -> None:
    task_queue = os.environ.get("TASK_QUEUE")
    if not task_queue:
        raise SystemExit("TASK_QUEUE environment variable is required")
    asyncio.run(
        run_worker(
            task_queue,
            workflows=discover_workflows(),
            activities=discover_activities(),
        )
    )


if __name__ == "__main__":
    _main()
