"""Radar-style trimming for cluster objects.

Turns raw (sanitized) Kubernetes JSON into lean dicts: one-line resource rows,
deduped events, filtered logs, and unhealthy-only health rollups. The goal is
token efficiency — never echo a full manifest unless the caller asks.

Sanitized objects use the API JSON shape (camelCase keys). Typed list items
carry no ``kind``, so the requested kind is passed in explicitly.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone


def _age(creation_ts: str | None) -> str | None:
    """Compact age (e.g. ``3d4h``) from an ISO creationTimestamp."""
    if not creation_ts:
        return None
    try:
        created = datetime.fromisoformat(creation_ts.replace("Z", "+00:00"))
    except ValueError:
        return None
    secs = max(0, int((datetime.now(timezone.utc) - created).total_seconds()))
    days, rem = divmod(secs, 86400)
    hours, rem = divmod(rem, 3600)
    mins, _ = divmod(rem, 60)
    if days:
        return f"{days}d{hours}h"
    if hours:
        return f"{hours}h{mins}m"
    return f"{mins}m"


def _compact(d: dict) -> dict:
    """Drop None values to keep rows lean."""
    return {k: v for k, v in d.items() if v is not None}


def _pod_status(obj: dict) -> dict:
    status = obj.get("status") or {}
    containers = status.get("containerStatuses") or []
    ready = sum(1 for c in containers if c.get("ready"))
    restarts = sum(c.get("restartCount", 0) for c in containers)
    waiting = None
    for c in containers:
        state = (c.get("state") or {}).get("waiting") or {}
        if state.get("reason"):
            waiting = state["reason"]
            break
    return _compact(
        {
            "phase": status.get("phase"),
            "ready": f"{ready}/{len(containers)}" if containers else None,
            "restarts": restarts or None,
            "reason": waiting,
            # restartPolicy distinguishes run-to-completion pods (Jobs, Argo
            # Workflow steps: Never/OnFailure) from long-running ones (Always).
            # The health rollup uses it so an in-flight batch pod is not judged
            # by a readiness invariant it is never meant to satisfy.
            "restart_policy": (obj.get("spec") or {}).get("restartPolicy"),
        }
    )


def _replica_status(obj: dict, ready_key: str = "readyReplicas") -> dict:
    spec = obj.get("spec") or {}
    status = obj.get("status") or {}
    desired = spec.get("replicas", 0)
    return {"ready": f"{status.get(ready_key, 0) or 0}/{desired}"}


def _daemonset_status(obj: dict) -> dict:
    status = obj.get("status") or {}
    desired = status.get("desiredNumberScheduled", 0) or 0
    return {"ready": f"{status.get('numberReady', 0) or 0}/{desired}"}


def _app_status(obj: dict) -> dict:
    status = obj.get("status") or {}
    return _compact(
        {
            "sync": (status.get("sync") or {}).get("status"),
            "health": (status.get("health") or {}).get("status"),
        }
    )


def _node_status(obj: dict) -> dict:
    conds = (obj.get("status") or {}).get("conditions") or []
    ready = next((c for c in conds if c.get("type") == "Ready"), None)
    return _compact({"ready": ready.get("status") if ready else None})


def _status_for(kind: str, obj: dict) -> dict:
    if kind == "pods":
        return _pod_status(obj)
    if kind in ("deployments", "statefulsets", "replicasets"):
        return _replica_status(obj)
    if kind == "daemonsets":
        return _daemonset_status(obj)
    if kind == "applications":
        return _app_status(obj)
    if kind == "nodes":
        return _node_status(obj)
    return {}


def resource_row(kind: str, obj: dict) -> dict:
    """One-line summary of a resource for list views."""
    meta = obj.get("metadata") or {}
    row = {
        "name": meta.get("name"),
        "namespace": meta.get("namespace"),
        "age": _age(meta.get("creationTimestamp")),
    }
    row.update(_status_for(kind, obj))
    return _compact(row)


def resource_detail(kind: str, obj: dict, full: bool = False) -> dict:
    """Trimmed key fields for a single resource; full manifest when ``full``."""
    if full:
        return obj
    meta = obj.get("metadata") or {}
    detail = {
        "kind": kind,
        "name": meta.get("name"),
        "namespace": meta.get("namespace"),
        "labels": meta.get("labels") or None,
        "age": _age(meta.get("creationTimestamp")),
        "status": _status_for(kind, obj),
        "conditions": _conditions(obj),
    }
    return _compact(detail)


def _conditions(obj: dict) -> list[dict] | None:
    conds = (obj.get("status") or {}).get("conditions") or []
    out = [
        _compact(
            {
                "type": c.get("type"),
                "status": c.get("status"),
                "reason": c.get("reason"),
                "message": (c.get("message") or "")[:200] or None,
            }
        )
        for c in conds
        if c.get("status") not in ("True", None) or c.get("reason")
    ]
    return out or None


def dedupe_events(events: list[dict]) -> list[dict]:
    """Collapse events by (object, type, reason, message) with a count.

    Returns newest-last-seen first. Messages are capped to keep rows lean.
    """
    grouped: dict[tuple, dict] = {}
    for e in events:
        obj = e.get("involvedObject") or {}
        key = (
            obj.get("kind"),
            obj.get("name"),
            e.get("type"),
            e.get("reason"),
            e.get("message"),
        )
        last_seen = e.get("lastTimestamp") or e.get("eventTime")
        first_seen = e.get("firstTimestamp") or last_seen
        if key in grouped:
            g = grouped[key]
            g["count"] += e.get("count", 1)
            if last_seen and (g["last_seen"] is None or last_seen > g["last_seen"]):
                g["last_seen"] = last_seen
        else:
            grouped[key] = {
                "object": f"{obj.get('kind')}/{obj.get('name')}",
                "namespace": obj.get("namespace"),
                "type": e.get("type"),
                "reason": e.get("reason"),
                "message": (e.get("message") or "")[:240],
                "count": e.get("count", 1),
                "first_seen": first_seen,
                "last_seen": last_seen,
            }
    rows = sorted(grouped.values(), key=lambda r: r["last_seen"] or "", reverse=True)
    return [_compact(r) for r in rows]


def filter_logs(
    text: str,
    grep: str | None = None,
    max_lines: int = 200,
    max_bytes: int = 16_000,
) -> dict:
    """Optionally regex-filter, then tail to ``max_lines`` and cap bytes."""
    lines = text.splitlines()
    matched = False
    if grep:
        pattern = re.compile(grep)
        lines = [ln for ln in lines if pattern.search(ln)]
        matched = True
    truncated = len(lines) > max_lines
    lines = lines[-max_lines:]
    body = "\n".join(lines)
    byte_capped = False
    if len(body.encode("utf-8")) > max_bytes:
        body = body.encode("utf-8")[-max_bytes:].decode("utf-8", "ignore")
        byte_capped = True
    return _compact(
        {
            "logs": body,
            "lines": len(lines),
            "filtered": matched or None,
            "truncated": (truncated or byte_capped) or None,
        }
    )


def _row_unhealthy(kind: str, row: dict) -> bool:
    if kind == "pods":
        # Run-to-completion pods (Jobs, Argo Workflow / CronWorkflow steps:
        # restartPolicy Never/OnFailure) are not meant to become Ready and churn
        # through Pending -> ContainerCreating -> Running (with a wait sidecar
        # that keeps ready at x/N, x != N) before exiting. Judged by the
        # long-running checks below they always look unhealthy while in flight,
        # so a per-minute health snapshot almost always catches one mid-run and
        # reports the cluster unhealthy. Only a terminal Failed is unhealthy; a
        # pending/initializing/running/succeeded batch pod is doing its job.
        if row.get("restart_policy") in ("Never", "OnFailure"):
            return row.get("phase") == "Failed"
        if row.get("reason"):
            return True
        phase = row.get("phase")
        if phase not in ("Running", "Succeeded", None):
            return True
        # A Succeeded pod has terminated all its containers, so it reports
        # ready=0/N by design. That is a completed Job/CronWorkflow pod, not an
        # unhealthy one, so the readiness check below (which flags any x/y with
        # x != y) must not apply to it. Without this guard every finished batch
        # pod is miscounted as unhealthy.
        if phase == "Succeeded":
            return False
        ready = row.get("ready")
        return bool(ready and ready.split("/")[0] != ready.split("/")[1])
    if kind in ("deployments", "statefulsets", "daemonsets", "replicasets"):
        ready = row.get("ready")
        return bool(ready and ready.split("/")[0] != ready.split("/")[1])
    if kind == "applications":
        return row.get("sync") not in ("Synced", None) or row.get("health") not in (
            "Healthy",
            None,
        )
    return False


def build_health(resources: dict[str, list[dict]]) -> dict:
    """Given {kind: [raw objs]}, return only the unhealthy rows, grouped by kind."""
    unhealthy: dict[str, list[dict]] = {}
    total = 0
    for kind, objs in resources.items():
        total += len(objs)
        bad = [resource_row(kind, o) for o in objs]
        bad = [r for r in bad if _row_unhealthy(kind, r)]
        if bad:
            unhealthy[kind] = bad
    return {
        "healthy": not unhealthy,
        "scanned": total,
        "unhealthy": unhealthy,
    }
