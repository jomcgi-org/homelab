"""Private-tier internal endpoint that runs the ember synthetic probes.

The cron job only TRIGGERS this; the probing happens in the API pod, mirroring
semgrep_scan.router's /internal/semgrep/harvest-scans ("the harvest runs in the
API pod, not this ephemeral job pod, so the job needs no tokens or DB access,
just HTTP").

That split is not stylistic here, it is the fix for the #4065 rollout. Probing
from the job pod failed on three counts, none of which CI could catch:

- bazel: embervm's auth.allowedServiceAccounts admits
  system:serviceaccount:monolith:monolith, but an Argo job runs as
  monolith-workflows:argo-workflow, so the control plane 403s.
- semgrep: semgrep_scan.client needs EmberVM credentials, which the backend
  Deployment sets and the job pod never had.
- postgres: reaching the demo over the public origin returns the SvelteKit HTML
  shell, because /api/ember/* is not on the public HTTPRoute.

This pod holds the admitted ServiceAccount and all three env vars, so each of
those disappears rather than being worked around.

A failing probe still returns 200: the failure travels through the recorded
latch row into /api/health, which is the alerting surface. Only an unreachable
endpoint or a failed DB write should fail the triggering job.
"""

from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter

logger = logging.getLogger(__name__)

internal_router = APIRouter(prefix="/internal/ember", tags=["ember-internal"])

# Guards against overlapping runs: a second trigger while one is in flight is a
# no-op rather than a second concurrent sweep competing for the same demo VMs
# (mirrors semgrep_scan.router's _harvest_in_flight).
_probe_in_flight = False


@internal_router.post("/synthetic-probe")
async def synthetic_probe_endpoint() -> dict:
    """Run all four ember probes concurrently and record their latch rows."""
    global _probe_in_flight

    if _probe_in_flight:
        logger.info("synthetic probe already in flight, skipping this trigger")
        return {"skipped": True, "detail": "already running"}

    _probe_in_flight = True
    try:
        from ember_public import synthetic_probe

        probes = {
            "bazel": synthetic_probe.probe_bazel,
            "semgrep": synthetic_probe.probe_semgrep,
            "pages": synthetic_probe.probe_pages,
            "postgres": synthetic_probe.probe_postgres,
        }
        # Concurrent, so wall time is the slowest probe rather than their sum.
        # Every probe returns its failure in-band, so gather never raises here.
        outcomes = await asyncio.gather(*(probe() for probe in probes.values()))
        results = dict(zip(probes, outcomes))

        for demo, result in results.items():
            if not result["ok"]:
                logger.warning("ember synthetic %s failed: %s", demo, result["detail"])

        # Recording IS allowed to raise: a failed write means the detector is
        # blind, which the triggering job should surface as a job failure.
        await asyncio.gather(
            *(synthetic_probe.record(demo, result) for demo, result in results.items())
        )
        return results
    finally:
        _probe_in_flight = False
