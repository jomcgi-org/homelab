"""Public stats endpoint exposing non-sensitive cluster and knowledge metrics.

Gathers data from four sources:
1. Kubernetes API — node, deployment, pod, ArgoCD application counts
   plus aggregate CPU/memory usage and capacity from the metrics API,
   and the monolith ArgoCD Application's last sync time.
2. DCGM exporter Prometheus text, GPU utilization and frame buffer usage.
3. PostgreSQL — knowledge.notes, knowledge.chunks, knowledge.raw_inputs counts.
4. GitHub API — latest commit on main (unauthenticated; public repo).

build_stats() assembles the payload; the observability.stats_rollup job snapshots
it into Postgres (ADR 004) so the read endpoint never calls external metrics or
the K8s API. Kept here because it is the only DCGM/K8s caller for stats.
"""

from __future__ import annotations

import asyncio
import logging
import math
import os
from datetime import datetime, timezone

import httpx
from sqlalchemy import text

from cluster.api import KubernetesClient
from core.db import get_engine
from core.github import GITHUB_API, GITHUB_REPO

ARGOCD_APP_NAME = "monolith"

logger = logging.getLogger(__name__)


_GPU_METRICS = (
    "DCGM_FI_DEV_GPU_UTIL",
    "DCGM_FI_DEV_FB_USED",
    "DCGM_FI_DEV_FB_FREE",
)


async def _query_knowledge_counts(engine) -> dict:
    """Query knowledge graph table counts from PostgreSQL."""
    queries = {
        "facts": "SELECT count(*) FROM knowledge.notes",
        "chunks": "SELECT count(*) FROM knowledge.chunks",
        "raw_inputs": "SELECT count(*) FROM knowledge.raw_inputs",
    }
    counts = {}
    from sqlmodel import Session

    with Session(engine) as session:
        for key, query in queries.items():
            try:
                result = session.exec(text(query)).one()
                counts[key] = result[0]
            except Exception:
                logger.exception("Failed to query %s count", key)
                counts[key] = 0
    return counts


async def _query_cluster_counts() -> dict:
    """Query Kubernetes cluster resource counts and aggregate node usage."""
    k8s = KubernetesClient()
    try:
        nodes, deployments, pods, argo_apps, resources = await asyncio.gather(
            k8s.count_nodes(),
            k8s.count_deployments(),
            k8s.count_pods(),
            k8s.count_argocd_applications(),
            k8s.aggregate_node_resources(),
            return_exceptions=True,
        )
        result: dict = {
            "nodes": nodes if not isinstance(nodes, Exception) else 0,
            "deployments": deployments if not isinstance(deployments, Exception) else 0,
            "pods": pods if not isinstance(pods, Exception) else 0,
            "argocd_apps": argo_apps if not isinstance(argo_apps, Exception) else 0,
        }
        if not isinstance(resources, Exception):
            cpu_used = resources["cpu_used_cores"]
            cpu_cap = resources["cpu_capacity_cores"]
            mem_used = resources["memory_used_bytes"] / 1024**3
            mem_cap = resources["memory_capacity_bytes"] / 1024**3
            result.update(
                {
                    "cpu_used_cores": round(cpu_used, 2),
                    "cpu_capacity_cores": round(cpu_cap, 1),
                    "memory_used_gb": round(mem_used, 1),
                    "memory_capacity_gb": round(mem_cap, 1),
                }
            )
        else:
            # resources is an Exception from asyncio.gather(return_exceptions=True).
            # We are not in an except block so exc_info=True would attach no traceback
            # (sys.exc_info() returns (None, None, None)). The exception message is
            # already visible via the %s format arg.
            logger.warning(
                "Node resource aggregation failed: %s", resources, exc_info=False
            )
        return result
    finally:
        await k8s.close()


async def _query_gpu() -> dict:
    """Scrape DCGM GPU utilization and frame buffer usage."""
    # The old query averaged samples over five minutes. This instantaneous
    # exporter scrape is a deliberate behavior change.
    try:
        base_url = os.environ.get("DCGM_EXPORTER_URL", "")
        async with httpx.AsyncClient(timeout=5.0) as client:
            response = await client.get(f"{base_url}/metrics")
            response.raise_for_status()

        samples: dict[str, list[float]] = {name: [] for name in _GPU_METRICS}
        for raw_line in response.text.splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            for name in _GPU_METRICS:
                if not line.startswith(name):
                    continue
                suffix = line[len(name) :]
                if not suffix or suffix[0] not in "{ \t":
                    continue
                if suffix.startswith("{"):
                    label_end = suffix.rfind("}")
                    if label_end < 0:
                        continue
                    suffix = suffix[label_end + 1 :]
                fields = suffix.split()
                if fields:
                    # Parse per line, not per scrape. The old ClickHouse path
                    # queried each metric independently, so one bad value could
                    # not take the others down, and that stays true here: a
                    # malformed sample drops only its own line.
                    #
                    # isfinite also rejects NaN and inf, which float() accepts
                    # happily. A NaN would survive round(), then json.dumps
                    # would emit the bare token NaN, which the jsonb column
                    # rejects: one bad GPU sample would fail the write for the
                    # whole stats snapshot, cluster counts included.
                    try:
                        value = float(fields[0])
                    except ValueError:
                        break
                    if math.isfinite(value):
                        samples[name].append(value)
                break

        def _average(name: str) -> float | None:
            values = samples[name]
            return sum(values) / len(values) if values else None

        util_v = _average("DCGM_FI_DEV_GPU_UTIL")
        used_v = _average("DCGM_FI_DEV_FB_USED")
        free_v = _average("DCGM_FI_DEV_FB_FREE")
        result: dict = {
            "utilization_pct": round(util_v, 1) if util_v is not None else None
        }
        if used_v is not None and free_v is not None:
            used_v = round(used_v, 0)
            free_v = round(free_v, 0)
            result["memory_used_gb"] = round(used_v / 1024, 1)
            result["memory_total_gb"] = round((used_v + free_v) / 1024, 1)
        return result
    except Exception:
        logger.exception("GPU scrape failed")
        return {"utilization_pct": None}


async def _query_github_latest_commit() -> dict | None:
    """Fetch the latest commit on main from the public GitHub API.

    Returns {"sha": <7-char>, "committed_at": <iso>} or None on any failure.
    Unauthenticated (60 req/hr per IP); the 60s stats cache keeps us under that.
    """
    try:
        async with httpx.AsyncClient(timeout=5.0, follow_redirects=True) as http:
            resp = await http.get(
                f"{GITHUB_API}/repos/{GITHUB_REPO}/commits/main",
                headers={"Accept": "application/vnd.github+json"},
            )
        if resp.status_code != 200:
            return None
        data = resp.json()
        return {
            "sha": data["sha"][:7],
            "committed_at": data["commit"]["committer"]["date"],
        }
    except Exception:
        logger.exception("GitHub commit fetch failed")
        return None


async def _query_argocd_monolith_deploy() -> dict | None:
    """Last-sync timestamp of the monolith ArgoCD Application.

    Reads status.operationState.finishedAt — the wall-clock moment the most
    recent sync (manual or auto) finished, regardless of result. None if the
    Application is missing or has no operationState yet.
    """
    k8s = KubernetesClient()
    try:
        status = await k8s.get_argocd_app_status(ARGOCD_APP_NAME)
        if not status:
            return None
        finished_at = (status.get("operationState") or {}).get("finishedAt")
        return {"finished_at": finished_at} if finished_at else None
    except Exception:
        logger.exception("ArgoCD monolith status fetch failed")
        return None
    finally:
        await k8s.close()


async def _query_deploy() -> dict:
    """Combine 'latest commit on main' + 'last deploy' into one block.

    Each subquery is independent and fail-soft — if one source is unavailable,
    the other still surfaces. Returns {} if both fail; the frontend skips
    items whose data is absent.
    """
    commit, deploy = await asyncio.gather(
        _query_github_latest_commit(),
        _query_argocd_monolith_deploy(),
    )
    out: dict = {}
    if commit:
        out["latest_commit_sha"] = commit["sha"]
        out["latest_commit_at"] = commit["committed_at"]
    if deploy:
        out["deployed_at"] = deploy["finished_at"]
    return out


async def build_stats() -> dict:
    """Collect all stats and return the response payload."""
    engine = get_engine()

    cluster_counts, knowledge_counts, gpu, deploy = await asyncio.gather(
        _query_cluster_counts(),
        _query_knowledge_counts(engine),
        _query_gpu(),
        _query_deploy(),
    )

    return {
        "cluster": cluster_counts,
        "knowledge": knowledge_counts,
        "gpu": gpu,
        "deploy": deploy,
        "platform": {
            "in_production_since": "2025-01",
        },
        "cached_at": datetime.now(timezone.utc).isoformat(),
    }
