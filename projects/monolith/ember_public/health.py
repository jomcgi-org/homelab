"""Ember health components backed entirely by synthetic probe latches.

The active prober replaced an earlier passive check. Each component reads the
latest latch row written by its synthetic probe, so the health endpoint has
one source of truth for the agent-lane probes.
"""

from __future__ import annotations

from datetime import datetime, timezone

from ember_public.synthetic import read_probe

# The Codex lane synthetic runs hourly (the ember-codex-session-synthetic
# CronWorkflow, see the jobs.cronWorkflows entry). 2.5x that cadence, so a
# single missed or slow run never flaps the check but a dead prober still
# surfaces. Explicit probe failures still report immediately; only the
# missing-probe staleness bound follows the cadence. The Spark lane probe is
# manual-only (its CronWorkflow is suspended), so it has no automatic health
# component; probe_spark and its endpoint remain for manual diagnostics.
EMBER_CODEX_STALENESS_S = 9000.0


def synthetic_probe_health(demo: str, staleness_s: float):
    """Build a health component backed by one synthetic probe latch row."""

    async def check() -> dict:
        row = await read_probe(demo)
        # Fail open on bootstrap: a missing row means the prober has not run
        # yet (or the table is not migrated yet on a fresh database).
        if row is None:
            return {
                "ok": True,
                "detail": "no probe recorded yet",
                "trace_id": None,
            }
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
            return {"ok": False, "detail": detail, "trace_id": row.trace_id}
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
                "trace_id": row.trace_id,
            }
        detail = (
            f"probe ok, {row.latency_ms:.0f}ms"
            if row.latency_ms is not None
            else "probe ok"
        )
        return {"ok": True, "detail": detail, "trace_id": row.trace_id}

    return check
