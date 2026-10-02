"""Background snapshot of the cluster health rollup.

The private dashboard's ``health`` section used to recompute on every request:
a live scan of every pod/deployment/statefulset/daemonset/ArgoCD app across all
namespaces (~235 objects), uncached. A cold page load blocked on the whole scan.
This module moves that work to a scheduled job
(``home.cluster_snapshot_refresh``) that upserts a single
``home.cluster_snapshot`` row, so the dashboard read path (see
``home.dashboard``) becomes a one-row lookup. The retained alerts column is
written as an empty object for schema compatibility. The same scan supplies a
bounded, read-only agent summary in ``agent_view.cluster_snapshot``.
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


def _scan_error(exc: Exception) -> str:
    """Bound the diagnostic stored in the agent summary."""
    return f"{type(exc).__name__}: {exc}"[:200]


async def scan_cluster_resources_live() -> tuple[dict, dict[str, str]]:
    """List each curated kind once, retaining listing failures separately."""
    from cluster.api import KubernetesClient

    k8s = KubernetesClient()
    try:
        resources: dict[str, list[dict]] = {}
        errors: dict[str, str] = {}
        for kind in _HEALTH_KINDS:
            try:
                resources[kind] = await k8s.list_resources(kind)
            except Exception as exc:
                logger.exception("cluster snapshot: listing %s failed", kind)
                resources[kind] = []
                errors[kind] = _scan_error(exc)
        return resources, errors
    finally:
        await k8s.close()


async def scan_health_live() -> dict:
    """Keep the dashboard's fail-soft live fallback semantics."""
    from cluster.api import build_health

    resources, _ = await scan_cluster_resources_live()
    return build_health(resources)


def _application_revisions(application: dict) -> tuple[str | None, str | None]:
    """Separate actually deployed status from the desired source revision."""
    sync = (application.get("status") or {}).get("sync") or {}
    revisions = sync.get("revisions") or []
    revision = sync.get("revision") or (revisions[0] if revisions else None)
    spec = application.get("spec") or {}
    sources = spec.get("sources") or []
    target = (spec.get("source") or {}).get("targetRevision") or (
        sources[0].get("targetRevision") if sources else None
    )
    return revision or None, target or None


def build_agent_cluster_summary(resources: dict, errors: dict[str, str]) -> dict:
    """Project successful listings onto bounded resource rows, never manifests."""
    from cluster.api import build_health, resource_row

    successful = {
        kind: resources[kind]
        for kind in _HEALTH_KINDS
        if kind in resources and kind not in errors
    }
    applications = []
    for application in successful.get("applications", []):
        row = resource_row("applications", application)
        revision, target = _application_revisions(application)
        applications.append(
            {
                "name": row.get("name"),
                "namespace": row.get("namespace"),
                "sync": row.get("sync"),
                "health": row.get("health"),
                "revision": revision,
                "target_revision": target,
            }
        )
    applications.sort(key=lambda row: (row["name"] or "", row["namespace"] or ""))
    unhealthy = build_health(successful)["unhealthy"]
    payload = {
        "schema_version": 1,
        "complete": not errors and len(successful) == len(_HEALTH_KINDS),
        "errors": {kind: message[:200] for kind, message in errors.items()},
        "scanned": {kind: len(objects) for kind, objects in successful.items()},
        "applications": applications[:500],
        "unhealthy": {kind: rows[:100] for kind, rows in unhealthy.items()},
    }
    if len(applications) > 500:
        payload["applications_truncated"] = len(applications) - 500
    truncated = {
        kind: len(rows) - 100 for kind, rows in unhealthy.items() if len(rows) > 100
    }
    if truncated:
        payload["unhealthy_truncated"] = truncated
    return payload


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


def _write_agent_cluster_snapshot(payload: dict) -> None:
    """Upsert the agent snapshot in a fresh worker-thread session."""
    from core.db import get_engine

    with Session(get_engine()) as session:
        session.execute(
            text(
                """
                INSERT INTO agent_view.cluster_snapshot (id, payload, snapshot_at)
                VALUES (1, :payload, now())
                ON CONFLICT (id) DO UPDATE
                    SET payload = EXCLUDED.payload,
                        snapshot_at = EXCLUDED.snapshot_at
                """
            ),
            {"payload": json.dumps(payload)},
        )
        session.commit()


async def refresh_cluster_snapshot() -> None:
    """Scan once and independently persist dashboard and agent projections.

    If the health scan fails, persist an error marker so the scheduled job
    remains fail-soft and the read path can report the failure.
    """
    from cluster.api import build_health

    try:
        resources, errors = await scan_cluster_resources_live()
    except Exception as exc:
        logger.exception("cluster snapshot: health scan failed")
        health = {"error": str(exc)}
        resources, errors = {}, {"scan": _scan_error(exc)}
    else:
        health = build_health(resources)
    payload = build_agent_cluster_summary(resources, errors)
    try:
        await asyncio.to_thread(_write_cluster_snapshot, health, {})
    except Exception:
        logger.exception("cluster snapshot: dashboard write failed")
    try:
        await asyncio.to_thread(_write_agent_cluster_snapshot, payload)
    except Exception:
        logger.exception("cluster snapshot: agent write failed")
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
