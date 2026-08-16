"""Ember public health components backed entirely by synthetic probe latches.

The active prober replaced an earlier passive check. Each component reads the
latest latch row written by its synthetic probe, so the public health endpoint
has one source of truth for the ember demos.
"""

from __future__ import annotations

from datetime import datetime, timezone

from ember_public.synthetic import read_probe

# All four of the ORIGINAL synthetic probes run in the one ember-synthetic
# CronWorkflow every 5 minutes (see the jobs.cronWorkflows entry). 2.5x that
# cadence, so a single missed or slow run never flaps the check but a dead
# prober still surfaces.
EMBER_SYNTHETIC_STALENESS_S = 750.0

# The qwen session synthetic is a SEPARATE CronWorkflow
# (ember-qwen-session-synthetic) on an HOURLY schedule, deliberately not folded
# into the 5-minute run: it cold-boots a real EmberVM guest and runs a full
# agent turn, which is not something to do twice a minute.
#
# It therefore must NOT reuse EMBER_SYNTHETIC_STALENESS_S. At 750s an hourly
# latch is stale 12.5 minutes into every hour, so the component would report
# "prober may be dead" for roughly 80% of the time it is working perfectly.
# Same 2.5x rule, applied to the cadence this probe actually has.
EMBER_QWEN_STALENESS_S = 9000.0


def synthetic_probe_health(demo: str, staleness_s: float):
    """Build a health component backed by one synthetic probe latch row."""

    async def check() -> dict:
        row = await read_probe(demo)
        # Fail open on bootstrap: monolith-public rolls out before the migration
        # creates the table, and a missing row means the prober has not run yet.
        if row is None:
            return {"ok": True, "detail": "no probe recorded yet"}
        if not row.ok:
            detail = row.detail
            if row.last_ok_at is not None:
                now = datetime.now(timezone.utc)
                last_ok_at = (
                    row.last_ok_at.replace(tzinfo=timezone.utc)
                    if row.last_ok_at.tzinfo is None
                    else row.last_ok_at
                )
                downtime_s = max(0.0, (now - last_ok_at).total_seconds())
                detail = f"{detail}, down for {downtime_s / 60:.1f}m"
            return {"ok": False, "detail": detail}
        checked_at = (
            row.checked_at.replace(tzinfo=timezone.utc)
            if row.checked_at.tzinfo is None
            else row.checked_at
        )
        age_s = (datetime.now(timezone.utc) - checked_at).total_seconds()
        if age_s > staleness_s:
            return {
                "ok": False,
                "detail": f"last probe was {age_s / 60:.0f}m ago, prober may be dead",
            }
        detail = (
            f"probe ok, {row.latency_ms:.0f}ms"
            if row.latency_ms is not None
            else "probe ok"
        )
        return {"ok": True, "detail": detail}

    return check
