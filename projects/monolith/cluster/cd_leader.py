"""Private-tier writer for the cd platform probe.

Leader-elected so exactly one replica probes, mirroring every other singleton
in this app. The loop computes CD health with the checks in cd_health.py (which
need ArgoCD reads and a GitHub token, both private-tier only) and writes the
result to the platform_probe latch. The public tier reads that latch, because
it is the tier UptimeRobot can reach and the tier that must not hold privilege.

The default-off deployment observation writer runs in the same cycle but only
AFTER the latch is written, and only on a fresh poll (a cache hit records
nothing), bounded by DEPLOYMENT_OBSERVATIONS_TIMEOUT_S. The latch must never
wait on it: its probe interval plus that timeout stays under the reader's
staleness bound (home/module.py ``_CD_STALENESS_S``).
"""

from __future__ import annotations

import asyncio
import logging
import os

from cluster.cd_health import cd_health_cycle, run_deployment_observation_cycle
from core.platform_probe import write_probe
from framework import log_task_exception, register_leader_tasks

logger = logging.getLogger(__name__)

PROBE_NAME = "cd"


def _interval_s() -> float:
    raw = os.environ.get("CD_PROBE_INTERVAL_S", "")
    try:
        return float(raw) if raw else 300.0
    except ValueError:
        logger.warning("cd probe: CD_PROBE_INTERVAL_S=%r is not a number", raw)
        return 300.0


async def _cycle() -> None:
    """One iteration: the latch first, then the bounded observation lane."""
    result, poll_time = await cd_health_cycle()
    await write_probe(PROBE_NAME, bool(result["ok"]), str(result["detail"]))
    if poll_time is not None:
        await run_deployment_observation_cycle(poll_time)


async def _loop() -> None:
    interval = _interval_s()
    while True:
        try:  # nosemgrep: no-broad-except-swallow - a dead loop is worse, logged here
            await _cycle()
        except asyncio.CancelledError:
            raise
        except Exception:
            # Never let one bad cycle kill the writer: a dead writer makes the
            # latch go stale, which the reader reports as a fault, which pages
            # for a broken probe rather than a broken platform.
            logger.exception("cd probe cycle failed")
        await asyncio.sleep(interval)


async def leader_start(app) -> list[asyncio.Task]:
    task = asyncio.create_task(_loop(), name="cd-probe")
    task.add_done_callback(log_task_exception)
    register_leader_tasks(app, [task])
    return [task]
