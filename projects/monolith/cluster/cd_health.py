"""Advisory CD health: is production keeping up with published chart Freight?

Folded into the deep ``/api/health`` response as the ``cd`` component, which
UptimeRobot already polls offsite. Issue #4597 has the full rationale; the short
version is that on 2026-08-09 a chart bump merged, its `deploy` failed, the
chart was never published, and ArgoCD's ``targetRevision`` pointed at something
that does not exist for ~35 minutes. Nothing alerted, because every alerting
path in this repo lives inside the cluster it is watching.

This component is advisory. A deploy in progress or production behind a chart
is not the public site being down, so the signal is reported as metadata and
does not make the health endpoint return 503.

Coverage is configured, not hardcoded (#4890): ``CD_HEALTH_KARGO_APPS`` lists
every Kargo-managed production app (monolith and embervm today, rendered from
``cdHealth.apps`` in the chart), and the lag judgement below runs once per
entry. Before embervm joined, its four-version lag on 2026-08-14 (#4884) was
invisible here while `Synced`/`Healthy` stayed true, because those describe
agreement with the pinned version, not currency with main.

1. A production app's live chart is older than the oldest unpromoted chart
   Kargo has discovered for it, for longer than the lag window. The old ArgoCD
   sweep faulted on permanently unconvergeable OutOfSync/Healthy diffs in
   authentik, argocd, kyverno, and context-forge-gateway, so it was always red
   and monitored nothing. The original #4597 fault class, a targetRevision for
   a chart that was never published, is structurally impossible now that Kargo
   owns targetRevision and only promotes discovered Freight. What can still
   happen is production sitting on an old chart while newer charts pile up.
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

import json
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


# One entry per Kargo-managed production app, rendered from
# cdHealth.apps by the chart (issue #4890). Each entry carries the three
# names the lag check needs: app is the ArgoCD Application whose deployed
# revision counts as that app's production, kargo_namespace is the Kargo
# project namespace whose Warehouse discovers its chart Freight, and
# chart_repo_suffix picks that chart out of a Freight's repoURL. The RBAC
# for both reads is already ClusterRole-scoped, so a new app costs a values
# change only.
def _parse_apps(raw: str | None) -> list[dict]:
    """Parse CD_HEALTH_KARGO_APPS into per-app check inputs.

    Malformed or incomplete entries are dropped with a warning rather than
    aborting the rest of the list, but an empty RESULT is not silently
    healthy: _chart_lag_fault faults on it, mirroring how empty Freight is
    treated below. A signal watching nothing must not read as green.
    """
    if not raw:
        return []
    try:
        entries = json.loads(raw)
    except ValueError:
        logger.warning("cd health: CD_HEALTH_KARGO_APPS is not valid JSON")
        return []
    if not isinstance(entries, list):
        logger.warning("cd health: CD_HEALTH_KARGO_APPS is not a JSON list")
        return []
    apps = []
    for entry in entries:
        if not isinstance(entry, dict):
            logger.warning("cd health: dropping non-object app entry %r", entry)
            continue
        app = entry.get("app")
        namespace = entry.get("kargo_namespace")
        suffix = entry.get("chart_repo_suffix")
        if not app or not namespace or not suffix:
            logger.warning("cd health: dropping incomplete app entry %r", entry)
            continue
        apps.append({"app": app, "namespace": namespace, "suffix": suffix})
    return apps


def _configured_apps() -> list[dict]:
    return _parse_apps(os.environ.get("CD_HEALTH_KARGO_APPS", ""))


def _chart_version(value: str | None) -> tuple[int, int, int] | None:
    try:
        parts = value.split(".") if value is not None else []
        if len(parts) != 3 or any(not part.isdigit() for part in parts):
            return None
        return tuple(int(part) for part in parts)  # type: ignore[return-value]
    except (AttributeError, ValueError):
        return None


def _evaluate_chart_lag(
    app: str,
    live_version: str | None,
    freight: list[dict],
    lag_s: float,
    now: datetime,
) -> tuple[str | None, str | None]:
    """Judge one app's production chart lag without doing any I/O.

    Every message names its app so several apps can share one detail string.
    """
    live = _chart_version(live_version)
    if live is None:
        return None, f"{app}: prod chart version unreadable"
    if not freight:
        # This component is advisory and pages nobody, so a false positive is
        # acceptable, but a false negative when the entire signal dies is not.
        return f"{app}: no Freight found, chart discovery may be broken", None

    newer = []
    for item in freight:
        version = _chart_version(item.get("version"))
        if version is not None and version > live and _parse_ts(item.get("created_at")):
            newer.append(item)
    if not newer:
        return None, f"{app}: prod on {live_version}, up to date"

    # The oldest unpromoted Freight owns the clock. New charts arriving every
    # 20 minutes must not reset how long production has actually been behind.
    oldest = min(newer, key=lambda item: _parse_ts(item["created_at"]))
    since = _parse_ts(oldest["created_at"])
    assert since is not None
    age_s = max(0.0, (now - since).total_seconds())
    count = len(newer)
    newest = max(newer, key=lambda item: _chart_version(item["version"]) or (0, 0, 0))
    # Kargo's creationTimestamp is when its Warehouse discovered the chart, up
    # to one poll interval (5m) after publication. Against 2h, that slack is
    # immaterial, but it should not surprise the next reader.
    if age_s > lag_s:
        return (
            f"{app}: prod on {live_version}, {count} chart(s) behind through "
            f"{newest['version']}, waiting {age_s / 60:.0f}m",
            None,
        )
    return None, (
        f"{app}: prod {count} chart(s) behind through {newest['version']} "
        f"for {age_s / 60:.0f}m"
    )


async def _chart_lag_fault(lag_s: float) -> tuple[bool, list[str]]:
    """Read every configured app's live Application and Kargo Freight.

    Returns (ok, details): ok is False when any app faults. The clock stays
    the oldest unpromoted Freight per app; nothing about the advisory tier
    changes with coverage.
    """
    from cluster.kubernetes import KubernetesClient  # noqa: PLC0415

    apps = _configured_apps()
    if not apps:
        # Same treatment as empty Freight: watching nothing is a dead signal
        # and must fault, never read as an all-clear.
        return False, ["no Kargo-managed apps configured, chart lag watches nothing"]

    kubernetes = KubernetesClient()
    ok = True
    details: list[str] = []
    for entry in apps:
        live = await kubernetes.get_argocd_app_deployed_revision(entry["app"])
        freight = await kubernetes.list_kargo_freight(
            entry["namespace"], repo_suffix=entry["suffix"]
        )
        app_ok = True
        fault, note = _evaluate_chart_lag(
            entry["app"], live, freight, lag_s, datetime.now(timezone.utc)
        )
        if fault:
            app_ok = False
            details.append(fault)
        if note:
            details.append(note)
        ok = ok and app_ok
    return ok, details


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
    """The advisory ``cd`` health component, plus the existing CI signal."""
    global _cache
    if _cache is not None and (time.monotonic() - _cache[0]) < _CACHE_TTL_S:
        return _cache[1]

    lag_s = _env_seconds("CD_HEALTH_CHART_LAG_S", 7200.0)
    red_window_s = _env_seconds("CD_HEALTH_CI_RED_WINDOW_S", 3600.0)

    details: list[str] = []
    ok = True

    try:  # nosemgrep: no-broad-except-swallow - reported in-band, logged below
        lag_ok, lag_details = await _chart_lag_fault(lag_s)
    except Exception as exc:  # noqa: BLE001
        # Includes the Forbidden an RBAC gap would produce. Report it rather
        # than 503: a broken checker must not masquerade as a broken platform.
        logger.warning("cd health: chart lag check failed: %s", exc)
        details.append(f"chart lag check unavailable ({exc})")
    else:
        if not lag_ok:
            ok = False
        details.extend(lag_details)

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
