"""CD health: can the platform's desired state actually reach the cluster?

Folded into the deep ``/api/health`` response as the ``cd`` component, which
UptimeRobot already polls offsite. Issue #4597 has the full rationale; the short
version is that on 2026-08-09 a chart bump merged, its `deploy` failed, the
chart was never published, and ArgoCD's ``targetRevision`` pointed at something
that does not exist for ~35 minutes. Nothing alerted, because every alerting
path in this repo lives inside the cluster it is watching.

Two signals, both derived from timestamps the upstreams already carry, so this
component holds no state and needs no prober or latch table:

1. An ArgoCD Application not Synced+Healthy for longer than the grace period.
   Covers the unreachable-chart case above, a failed sync, and a degraded
   workload, without needing registry credentials: an app pinned to a chart
   that was never published does not reach Synced.
2. main's CI: the most recent commit WITH A COMPLETED STATUS is red and older
   than the red window, or there were commits inside that window and none of
   them completed at all.

The shape of (2) took several passes and the reasoning is worth keeping:

- Red only matters once it persists. A red run being actively fixed must not
  page; red for an hour means nobody is on it.
- No commits does NOT mean no signal. The last completed status still stands,
  so a quiet green main is healthy at any age. Treating silence as staleness
  would page every night and every weekend.
- Commits with nothing completed is the CI-is-down case, and it is the gap a
  naive "is the latest status red" check leaves wide open: if CI stops
  reporting entirely, that check reports green forever.

A scheduled canary was considered and rejected: it would have cost roughly
0.4% of this repo's BuildBuddy volume to detect CI being down during quiet
hours, which is exactly when that fault has no consequence. The
commits-with-nothing-completed rule catches it at the first merge instead,
which is when it starts to matter.
"""

from __future__ import annotations

import logging
import os
import time
from datetime import datetime, timedelta, timezone

import httpx

logger = logging.getLogger(__name__)

GITHUB_API = "https://api.github.com"
REPO = "jomcgi/homelab"

# Cache window. UptimeRobot polls frequently and neither the k8s API nor the
# GitHub API should be hit per request. Short enough that a fault surfaces
# within a poll or two of appearing.
_CACHE_TTL_S = 60.0
_cache: tuple[float, dict] | None = None


def _env_seconds(name: str, default: float) -> float:
    raw = os.environ.get(name, "")
    try:
        return float(raw) if raw else default
    except ValueError:
        logger.warning("cd health: %s=%r is not a number, using %s", name, raw, default)
        return default


def _parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


async def _argocd_fault(grace_s: float) -> str | None:
    """Fetch the apps, then judge them. I/O here, decision in _argocd_fault_real."""
    from cluster.kubernetes import KubernetesClient  # noqa: PLC0415

    apps = await KubernetesClient().list_argocd_app_health()
    return await _argocd_fault_real(apps, grace_s)


def _non_prod_apps() -> frozenset[str]:
    """ArgoCD apps whose health is reported but does NOT fault this component.

    This check was written when every ArgoCD app WAS production, so "any app
    unhealthy" and "production unhealthy" were the same predicate. Introducing
    monolith-dev broke that equivalence silently, and the check kept evaluating
    the old one: a dev environment with a stuck Atlas migration took
    jomcgi.dev/health to 503 while production was entirely fine.

    These are still worth reporting. A dev environment that cannot sync is a
    real signal and often an early one, since it runs the same chart production
    is about to. It is just not a reason to tell the outside world that this
    site is down.
    """
    raw = os.environ.get("CD_HEALTH_NON_PROD_APPS", "monolith-dev")
    return frozenset(n.strip() for n in raw.split(",") if n.strip())


async def _argocd_fault_real(
    apps: list[dict], grace_s: float, non_prod: frozenset[str] | None = None
) -> tuple[str | None, str | None]:
    """Split unhealthy apps into (production faults, non-production notes).

    Returns a pair so the caller can decide differently about each. Only the
    first makes /api/health 503; the second rides along in the detail string so
    the signal is kept rather than discarded.
    """
    if non_prod is None:
        non_prod = _non_prod_apps()
    now = datetime.now(timezone.utc)
    faults: list[str] = []
    notes: list[str] = []
    for app in apps:
        if app.get("sync") == "Synced" and app.get("health") == "Healthy":
            continue
        finished = _parse_ts(app.get("finished_at"))
        # No finish time means we cannot date the fault. Treat it as in-flight
        # rather than page: a genuinely stuck app acquires one on its next
        # attempt, and guessing here would page on every fresh rollout.
        if finished is None:
            continue
        age_s = (now - finished).total_seconds()
        if age_s > grace_s:
            line = (
                f"{app['name']} is {app.get('sync')}/{app.get('health')} "
                f"for {age_s / 60:.0f}m"
            )
            (notes if app.get("name") in non_prod else faults).append(line)
    return (
        "; ".join(sorted(faults)) if faults else None,
        "; ".join(sorted(notes)) if notes else None,
    )


async def _ci_fault(red_window_s: float) -> str | None:
    """Return a description of the CI fault, or None if CI is fine."""
    # GITHUB_TOKEN in this deployment is a kloak placeholder for the guest
    # egress swap and is useless for a direct API call; the chart wires the real
    # credential under GITHUB_API_TOKEN for exactly this reason. Reading the
    # obvious name would fail auth and page for a broken health check rather
    # than a broken deploy. Do not "fix" this to GITHUB_TOKEN.
    token = os.environ.get("GITHUB_API_TOKEN", "")
    if not token:
        # A config gap is not a platform outage, so do not page. Say so in the
        # detail rather than silently reporting nothing.
        return None

    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
    }
    now = datetime.now(timezone.utc)
    since = (now - timedelta(seconds=red_window_s)).isoformat()

    async with httpx.AsyncClient(timeout=10.0) as http:
        resp = await http.get(
            f"{GITHUB_API}/repos/{REPO}/commits",
            params={"sha": "main", "per_page": 20},
            headers=headers,
        )
        resp.raise_for_status()
        commits = resp.json()
        if not commits:
            return None

        recent = [
            c
            for c in commits
            if (c.get("commit") or {}).get("committer", {}).get("date", "") >= since
        ]

        newest_completed: dict | None = None
        for commit in commits:
            status = await http.get(
                f"{GITHUB_API}/repos/{REPO}/commits/{commit['sha']}/status",
                headers=headers,
            )
            status.raise_for_status()
            body = status.json()
            if body.get("state") in ("success", "failure", "error"):
                newest_completed = {"sha": commit["sha"], **body}
                break

    return _evaluate_ci(len(recent), newest_completed, red_window_s)


def _evaluate_ci(
    recent_commit_count: int, newest_completed: dict | None, red_window_s: float
) -> str | None:
    """The CI judgement, pure so the rules are unit-testable.

    See the module docstring for why each branch is shaped this way.
    """
    # Commits landed inside the window but not one of them finished. Either the
    # runners are wedged or nothing is reporting back: the CI-is-down case, and
    # the gap a naive "is the latest status red" check leaves wide open.
    if recent_commit_count and newest_completed is None:
        return (
            f"{recent_commit_count} commit(s) in the last {red_window_s / 60:.0f}m and "
            "none have a completed status; CI may be down"
        )

    # Nothing completed and nothing recent: nothing to assert.
    if newest_completed is None:
        return None
    # A quiet green main is healthy at any age.
    if newest_completed["state"] == "success":
        return None

    updated = _parse_ts(newest_completed.get("updated_at"))
    if updated is None:
        return None
    age_s = (datetime.now(timezone.utc) - updated).total_seconds()
    if age_s <= red_window_s:
        # Red but recent: someone is probably mid-fix. Not a page yet.
        return None
    return (
        f"main is {newest_completed['state']} at {newest_completed['sha'][:9]} "
        f"for {age_s / 60:.0f}m"
    )


async def cd_health() -> dict:
    """The ``cd`` health component. Any fault makes /api/health return 503."""
    global _cache
    if _cache is not None and (time.monotonic() - _cache[0]) < _CACHE_TTL_S:
        return _cache[1]

    grace_s = _env_seconds("CD_HEALTH_SYNC_GRACE_S", 900.0)
    red_window_s = _env_seconds("CD_HEALTH_CI_RED_WINDOW_S", 3600.0)

    details: list[str] = []
    ok = True

    try:  # nosemgrep: no-broad-except-swallow - reported in-band, logged below
        argocd = await _argocd_fault(grace_s)
    except Exception as exc:  # noqa: BLE001
        # Includes the Forbidden an RBAC gap would produce. Report it rather
        # than 503: a broken checker must not masquerade as a broken platform.
        logger.warning("cd health: argocd check failed: %s", exc)
        details.append(f"argocd check unavailable ({exc})")
    else:
        argocd_fault, argocd_note = argocd
        if argocd_fault:
            ok = False
            details.append(argocd_fault)
        if argocd_note:
            # Reported, deliberately NOT faulting. See _non_prod_apps: a dev
            # environment failing to sync is worth surfacing and is not a
            # reason to tell the outside world this site is down.
            details.append(f"non-prod: {argocd_note}")

    if not os.environ.get("GITHUB_API_TOKEN", ""):
        details.append("ci check disabled: no GITHUB_API_TOKEN")
    else:
        try:  # nosemgrep: no-broad-except-swallow - reported in-band, logged below
            ci = await _ci_fault(red_window_s)
        except Exception as exc:  # noqa: BLE001
            logger.warning("cd health: ci check failed: %s", exc)
            details.append(f"ci check unavailable ({exc})")
        else:
            if ci:
                ok = False
                details.append(ci)

    result = {"ok": ok, "detail": "; ".join(details) if details else "cd ok"}
    _cache = (time.monotonic(), result)
    return result
