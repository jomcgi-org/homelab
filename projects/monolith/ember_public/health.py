"""demo_postgres /api/health component (see framework/core.py's register_health).

Sourced ENTIRELY from the cached control-plane status read
(``core.cached_demo_pg_status``) and the in-process query-outcome record
(``core.last_query_outcome``): this check must never open a DB connection to
the demo VM, since an asleep demo is healthy and a health probe that woke it
would defeat the whole sleep story.

Five unhealthy conditions (see the ember public-pages design, "Health +
alerting"), checked in order and short-circuited at the first hit:

1. The control plane is unreachable or the demo is unconfigured.
2. The workload reports a broken snapshot/volume pairing while banked.
3. The workload is stuck in a fault eviction (pair_broken) past the same 90s
   window, a passive sign the wake path is broken (a benign ttl eviction is
   never flagged). See the demo-postgres provisioning wedge RCA.
4. A transitional state (relighting/cold_booting/starting/banking) has
   persisted past 90s (wakeTimeoutSeconds is 60s plus margin), a passive
   sign of a wedged cold boot.
5. The most recent real query in the last 10 minutes failed or its connect
   exceeded 60s, with no newer success superseding it.
"""

from __future__ import annotations

from datetime import datetime, timezone
from time import monotonic

from ember_public import core
from ember_public.synthetic import read_probe

_TRANSITIONAL_STATES = frozenset({"relighting", "cold_booting", "starting", "banking"})

# Eviction reasons that signal a fault (a broken snapshot/volume pairing), not
# benign recycling. A `ttl` eviction (banked-TTL expiry) is normal and never
# flags unhealthy; only a `pair_broken` eviction that fails to recover does.
_FAULT_EVICTION_REASONS = frozenset({"pair_broken"})

_STUCK_TRANSITION_S = 90.0
_SLOW_WAKE_CONNECT_MS = 60000.0
_RECENT_OUTCOME_WINDOW_S = 600.0

# All four synthetic probes run in the one ember-synthetic CronWorkflow every
# 5 minutes (see the jobs.cronWorkflows entry). 2.5x that cadence, so a single
# missed or slow run never flaps the check but a dead prober still surfaces.
EMBER_SYNTHETIC_STALENESS_S = 750.0


async def demo_postgres_health() -> dict:
    """The demo_postgres /api/health component. Never connects to the demo DB."""
    if not core.EMBERVM_URL or not core.demo_pg_dsn():
        return {"ok": False, "detail": "control plane unreachable or unconfigured"}

    try:  # nosemgrep: no-broad-except-swallow - the exception itself is the finding, returned in-band
        status = await core.cached_demo_pg_status()
    except Exception as exc:  # noqa: BLE001 - an unreachable control plane is the finding
        return {"ok": False, "detail": f"control plane unreachable: {exc}"}

    state = status.get("state")

    if state == "banked" and status.get("pair_valid") is False:
        return {"ok": False, "detail": "banked with invalid snapshot/volume pairing"}

    # A fault eviction (pair_broken) is normally recoverable: the next wake
    # cold-boots from the volume. But a demo that stays evicted past the wake
    # window is not re-banking or re-serving, a passive sign the wake path is
    # broken (observed 2026-07-19: an unprovisioned base image failed every cold
    # boot, so the demo sat evicted/pair_broken for hours while a bare
    # /api/health read looked healthy once the last failed-query outcome aged
    # out). Gated on the same stuck window as a transitional state so a brief
    # warmth discard never flaps. A benign ttl eviction never lands here.
    instance = status.get("instance") or {}
    if (
        state == "evicted"
        and instance.get("terminal_reason") in _FAULT_EVICTION_REASONS
    ):
        changed_at = core.status_cache_state_changed_at()
        if changed_at is not None:
            stuck_for = monotonic() - changed_at
            if stuck_for > _STUCK_TRANSITION_S:
                return {
                    "ok": False,
                    "detail": f"evicted (pair_broken) for {stuck_for:.0f}s, wake path likely broken",
                }

    if state in _TRANSITIONAL_STATES:
        changed_at = core.status_cache_state_changed_at()
        if changed_at is not None:
            stuck_for = monotonic() - changed_at
            if stuck_for > _STUCK_TRANSITION_S:
                return {
                    "ok": False,
                    "detail": f"{state} for {stuck_for:.0f}s, wake likely wedged",
                }

    outcome = core.last_query_outcome()
    at_monotonic = outcome.get("at_monotonic")
    if at_monotonic is not None:
        age_s = monotonic() - at_monotonic
        if age_s <= _RECENT_OUTCOME_WINDOW_S:
            ok = outcome.get("ok")
            connect_ms = outcome.get("connect_ms")
            if not ok:
                return {"ok": False, "detail": "most recent wake attempt failed"}
            if connect_ms is not None and connect_ms > _SLOW_WAKE_CONNECT_MS:
                return {
                    "ok": False,
                    "detail": f"most recent wake took {connect_ms:.0f}ms, exceeds threshold",
                }

    if state == "banked":
        return {"ok": True, "detail": "banked, pair valid"}
    return {"ok": True, "detail": f"{state}"}


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
