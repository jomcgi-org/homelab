"""Background snapshot of the cluster health rollup + firing SigNoz alerts.

The private dashboard's ``health`` and ``alerts`` sections used to recompute on
every request: a live scan of every pod/deployment/statefulset/daemonset/ArgoCD
app across all namespaces (~235 objects), plus a SigNoz ``/api/v1/rules`` fetch,
uncached. A cold page load blocked on the whole scan. This module moves that
work to a scheduled job (``home.cluster_snapshot_refresh``) that upserts a
single ``home.cluster_snapshot`` row, so the dashboard read path (see
``home.dashboard``) becomes a one-row lookup.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone

from sqlalchemy.exc import OperationalError, ProgrammingError
from sqlmodel import Session, text

logger = logging.getLogger(__name__)

# Workload kinds scanned by the health rollup. Mirrors dashboard._HEALTH_KINDS
# and cluster.mcp._HEALTH_KINDS.
_HEALTH_KINDS = ("deployments", "statefulsets", "daemonsets", "pods", "applications")

# Serve a stored snapshot up to this age. Older than this, the read path treats
# the refresher as wedged and returns None so the caller falls back to a live
# scan, rather than leave the dashboard showing hours-stale "all healthy". Set
# well above the 60s refresh cadence so ordinary jitter never trips it.
_STALE_FALLBACK_SECS = 600


async def scan_health_live() -> dict:
    """Run the live cluster health rollup (the expensive path).

    Fail-soft per kind: a listing error for one kind logs and yields an empty
    list for it rather than aborting the whole scan.
    """
    from cluster.api import KubernetesClient, build_health

    k8s = KubernetesClient()
    try:
        resources: dict[str, list[dict]] = {}
        for kind in _HEALTH_KINDS:
            try:
                resources[kind] = await k8s.list_resources(kind)
            except Exception:
                logger.exception("cluster snapshot: listing %s failed", kind)
                resources[kind] = []
        return build_health(resources)
    finally:
        await k8s.close()


async def fetch_alerts_live() -> dict:
    """Fetch firing SigNoz alert rules (the live path)."""
    from agent.api import check_firing_alerts

    return {"firing": await check_firing_alerts()}


def _write_cluster_snapshot(health: dict, alerts: dict) -> None:
    """Upsert the single snapshot row. Opens its own session so it can run in a
    worker thread off the event loop."""
    from core.db import get_engine

    with Session(get_engine()) as session:
        session.execute(
            text(
                """
                INSERT INTO home.cluster_snapshot (id, health, alerts, snapshot_at)
                VALUES (1, :health, :alerts, now())
                ON CONFLICT (id) DO UPDATE
                    SET health = EXCLUDED.health,
                        alerts = EXCLUDED.alerts,
                        snapshot_at = EXCLUDED.snapshot_at
                """
            ),
            {"health": json.dumps(health), "alerts": json.dumps(alerts)},
        )
        session.commit()


async def refresh_cluster_snapshot() -> None:
    """Scan health + fetch alerts concurrently and upsert the snapshot row.

    Fail-soft per section: if the health scan fails the alerts are still
    persisted (and vice versa), each failed section stored as {"error": ...} so
    a flaky SigNoz never blanks health and a K8s API blip never blanks alerts.
    """
    health, alerts = await asyncio.gather(
        scan_health_live(), fetch_alerts_live(), return_exceptions=True
    )
    if isinstance(health, Exception):
        logger.warning("cluster snapshot: health scan failed: %s", health)
        health = {"error": str(health)}
    if isinstance(alerts, Exception):
        logger.warning("cluster snapshot: alerts fetch failed: %s", alerts)
        alerts = {"error": str(alerts)}
    await asyncio.to_thread(_write_cluster_snapshot, health, alerts)
    logger.info("cluster snapshot refreshed (scanned=%s)", health.get("scanned"))


def read_cluster_snapshot(session: Session) -> dict | None:
    """Return the stored snapshot, or None when the caller should live-scan.

    Shape when present:
        {"health": dict, "alerts": dict, "snapshot_at": iso str, "age_secs": float}

    None is returned (meaning "fall back to a live scan") when the row is
    absent (fresh deploy before the first refresh), too stale (a wedged
    refresher, older than ``_STALE_FALLBACK_SECS``), or the table does not
    exist (an unmigrated env, or the SQLite test fixtures which build tables
    from SQLModel metadata rather than the raw-SQL migrations).
    """
    try:
        row = session.execute(
            text(
                "SELECT health, alerts, snapshot_at "
                "FROM home.cluster_snapshot WHERE id = 1"
            )
        ).first()
    except (OperationalError, ProgrammingError):
        session.rollback()
        return None
    if row is None:
        return None

    health, alerts, snapshot_at = row
    # SQLite hands JSON/timestamps back as strings; Postgres parses them.
    if isinstance(health, str):
        health = json.loads(health)
    if isinstance(alerts, str):
        alerts = json.loads(alerts)
    if isinstance(snapshot_at, str):
        snapshot_at = datetime.fromisoformat(snapshot_at)
    if snapshot_at.tzinfo is None:
        snapshot_at = snapshot_at.replace(tzinfo=timezone.utc)

    age_secs = (datetime.now(timezone.utc) - snapshot_at).total_seconds()
    if age_secs > _STALE_FALLBACK_SECS:
        return None

    return {
        "health": health,
        "alerts": alerts,
        "snapshot_at": snapshot_at.isoformat(),
        "age_secs": age_secs,
    }
