"""Dashboard aggregation for the private landing page.

Fans out to cluster health, firing alerts, GitHub PR/CI status, knowledge
review queues and task rollups, scheduler job health, and today's calendar
events. Each collector is fail-soft: a failure in one section is caught and
reported as {"error": str(exc)} for that section only, so a single flaky
upstream (SigNoz, GitHub, the K8s API) never blanks the whole dashboard.

Private-tier only (registered in home.register, not home.register_public):
the GitHub token, SigNoz alerts and cluster health rollup are not meant for
the public surface.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from datetime import datetime, timezone

import httpx

logger = logging.getLogger(__name__)

GITHUB_REPO = "jomcgi/homelab"
_GITHUB_CACHE_TTL_SECS = 60

# Workload kinds scanned by the health rollup, mirrors cluster.mcp._HEALTH_KINDS.
_HEALTH_KINDS = ("deployments", "statefulsets", "daemonsets", "pods", "applications")

# Module-level cache: {"data": dict, "expires_at": float} or None until first fill.
_github_cache: dict | None = None


def _github_headers() -> dict:
    token = os.environ.get("GITHUB_TOKEN", "")
    headers = {"Accept": "application/vnd.github+json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


async def _fetch_check_run_status(client: httpx.AsyncClient, head_sha: str) -> str:
    """Reduce a commit's check-runs to one of passing/failing/pending."""
    try:
        resp = await client.get(f"/repos/{GITHUB_REPO}/commits/{head_sha}/check-runs")
        resp.raise_for_status()
        runs = resp.json().get("check_runs", []) or []
    except Exception:
        logger.exception("dashboard: check-runs fetch failed for %s", head_sha)
        return "pending"

    if not runs:
        return "pending"

    statuses = {run.get("status") for run in runs}
    conclusions = {run.get("conclusion") for run in runs}

    if statuses - {"completed"}:
        return "pending"
    if conclusions & {"failure", "timed_out", "cancelled", "action_required"}:
        return "failing"
    return "passing"


async def _fetch_github_live() -> dict:
    """Fetch open PRs (with CI status) and recent merges from the GitHub API."""
    async with httpx.AsyncClient(
        base_url="https://api.github.com",
        headers=_github_headers(),
        timeout=10,
    ) as client:
        open_resp, closed_resp = await asyncio.gather(
            client.get(f"/repos/{GITHUB_REPO}/pulls", params={"state": "open"}),
            client.get(
                f"/repos/{GITHUB_REPO}/pulls",
                params={
                    "state": "closed",
                    "sort": "updated",
                    "direction": "desc",
                    "per_page": 10,
                },
            ),
        )
        open_resp.raise_for_status()
        closed_resp.raise_for_status()
        open_prs = open_resp.json()
        closed_prs = closed_resp.json()

        ci_statuses = await asyncio.gather(
            *(_fetch_check_run_status(client, pr["head"]["sha"]) for pr in open_prs)
        )

    prs = [
        {
            "number": pr["number"],
            "title": pr["title"],
            "author": pr["user"]["login"],
            "draft": pr["draft"],
            "ci": ci,
            "updated_at": pr["updated_at"],
            "url": pr["html_url"],
        }
        for pr, ci in zip(open_prs, ci_statuses)
    ]

    merges = [
        {
            "number": pr["number"],
            "title": pr["title"],
            "merged_at": pr["merged_at"],
            "url": pr["html_url"],
        }
        for pr in closed_prs
        if pr.get("merged_at") is not None
    ][:5]

    return {"open_prs": prs, "recent_merges": merges}


async def _collect_github() -> dict:
    """GitHub open PRs (with CI status) + recent merges, 60s in-process cache."""
    global _github_cache

    now = time.monotonic()
    if _github_cache is not None and _github_cache["expires_at"] > now:
        return _github_cache["data"]

    data = await _fetch_github_live()
    _github_cache = {"data": data, "expires_at": now + _GITHUB_CACHE_TTL_SECS}
    return data


async def _collect_health() -> dict:
    """Cluster health rollup, reusing the same internals as k8s_health_summary."""
    from cluster.api import KubernetesClient, build_health

    k8s = KubernetesClient()
    try:
        resources: dict[str, list[dict]] = {}
        for kind in _HEALTH_KINDS:
            try:
                resources[kind] = await k8s.list_resources(kind)
            except Exception:
                logger.exception("dashboard: listing %s failed", kind)
                resources[kind] = []
        return build_health(resources)
    finally:
        await k8s.close()


async def _collect_alerts() -> dict:
    """Firing SigNoz alert rules."""
    from agent.api import check_firing_alerts

    firing = await check_firing_alerts()
    return {"firing": firing}


async def _collect_queues(session) -> dict:
    """Knowledge review-queue counts, task rollups, and scheduler job health."""
    from knowledge.api import (
        count_gaps_review_queue,
        count_notes_review_queue,
        list_tasks_daily,
        list_tasks_weekly,
    )
    from scheduler.api import list_jobs

    jobs = [
        {
            "name": job.name,
            "last_status": job.last_status,
            "last_run_at": job.last_run_at,
            "next_run_at": job.next_run_at,
        }
        for job in list_jobs(session)
    ]
    # Failing/stuck jobs first: a job with a non-ok last_status (including
    # never-run, last_status is None) sorts ahead of healthy ones.
    jobs.sort(key=lambda j: 0 if j["last_status"] != "ok" else 1)

    return {
        "notes_review_queue": count_notes_review_queue(session),
        "gaps_review_queue": count_gaps_review_queue(session),
        "tasks_daily": list_tasks_daily(session),
        "tasks_weekly": list_tasks_weekly(session),
        "scheduler_jobs": jobs,
    }


async def _collect_today(session) -> dict:
    """Today's calendar events (same-domain, home.schedule)."""
    from home.schedule import get_today_events

    return {"events": get_today_events(session)}


async def build_dashboard(session) -> dict:
    """Assemble every dashboard section, isolating failures per-section.

    Each collector runs concurrently; a raised exception in one is mapped to
    {"error": str(exc)} for that section rather than failing the whole
    response.
    """
    names = ("health", "alerts", "github", "queues", "today")
    results = await asyncio.gather(
        _collect_health(),
        _collect_alerts(),
        _collect_github(),
        _collect_queues(session),
        _collect_today(session),
        return_exceptions=True,
    )

    sections: dict = {}
    for name, result in zip(names, results):
        if isinstance(result, Exception):
            # Not in an except block (result comes from gather(return_exceptions=True)),
            # so there is no live traceback to attach via exc_info: log the message only.
            logger.warning("dashboard: section %s failed: %s", name, result)
            sections[name] = {"error": str(result)}
        else:
            sections[name] = result

    sections["cached_at"] = datetime.now(timezone.utc).isoformat()
    return sections
