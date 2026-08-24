"""Dashboard aggregation for the private landing page.

Fans out to cluster health, firing alerts, GitHub PR/CI status, and today's
calendar events. Each collector is fail-soft: a failure in one section is
caught and reported as {"error": str(exc)} for that section only, so a single
flaky upstream (SigNoz, GitHub, the K8s API) never blanks the whole dashboard.

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

from core.github import GITHUB_API, GITHUB_REPO

logger = logging.getLogger(__name__)

_GITHUB_CACHE_TTL_SECS = 60

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
        base_url=GITHUB_API,
        headers=_github_headers(),
        timeout=10,
        follow_redirects=True,
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


async def _collect_health(session) -> dict:
    """Cluster health rollup, served from the background snapshot when fresh.

    The scan itself (~235 resources across all namespaces) runs off-request in
    home.cluster_snapshot_refresh, so the normal path here is a single-row read.
    Falls back to a live scan only when no fresh snapshot exists: a fresh deploy
    before the first refresh, a wedged refresher, or an unmigrated env.
    """
    from home.cluster_snapshot import read_cluster_snapshot, scan_health_live

    snap = read_cluster_snapshot(session)
    if snap is not None:
        # Surface freshness without disturbing the {healthy, scanned, unhealthy}
        # shape the frontend reads.
        health = dict(snap["health"])
        health["snapshot_at"] = snap["snapshot_at"]
        return health
    return await scan_health_live()


async def _collect_alerts(session) -> dict:
    """Firing SigNoz alert rules, served from the background snapshot when fresh.

    Falls back to a live SigNoz fetch only when no fresh snapshot exists (see
    _collect_health for when that happens)."""
    from home.cluster_snapshot import fetch_alerts_live, read_cluster_snapshot

    snap = read_cluster_snapshot(session)
    if snap is not None:
        return snap["alerts"]
    return await fetch_alerts_live()


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
    names = ("health", "alerts", "github", "today")
    results = await asyncio.gather(
        _collect_health(session),
        _collect_alerts(session),
        _collect_github(),
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
