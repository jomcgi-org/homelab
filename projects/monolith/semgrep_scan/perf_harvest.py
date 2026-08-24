"""Harvest Semgrep Managed Scans (SMS) scan records into semgrep.scan_perf.

There is no list-scans endpoint, so SMS scan ids can only be discovered by
sweeping the deployment's findings for distinct ``first_seen_scan_id`` values
(see the plan's background section). For each newly-seen id this fetches the
scan record from the Semgrep App and, if it is an SMS scan (``environment ==
SCAN_ENVIRONMENT_MANAGED_SCANS``), upserts a ScanPerf row via perf_store. Route
B rows are written directly by report.py at scan-complete and are never touched
here.

Auth: ``SEMGREP_APP_TOKEN``, the same token report.py already authenticates
with, confirmed to read both the findings and scans endpoints.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime
from typing import Any

import httpx
from sqlmodel import Session, select

from core.github import GITHUB_REPO
from semgrep_scan.perf_store import ScanPerf, upsert_scan_perf

logger = logging.getLogger("monolith.semgrep.perf_harvest")

SEMGREP_BASE = "https://semgrep.dev"
DEPLOYMENT_SLUG = "jomcgi"

_HTTP_TIMEOUT = 30.0


def _token() -> str:
    """The Semgrep App token, injected from env (the same one report.py uses)."""
    return os.environ.get("SEMGREP_APP_TOKEN", "")


def _headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def fetch_finding_scan_ids(repo: str, token: str) -> set[int]:
    """Return the distinct ``first_seen_scan_id`` values across open findings.

    This is the only way to discover SMS scan ids (no list-scans endpoint
    exists). Returns an empty set on a missing token or a non-200 response,
    logging a warning rather than raising, so a harvest failure never crashes
    the caller.
    """
    if not token:
        logger.warning("perf_harvest: SEMGREP_APP_TOKEN is not set, skipping fetch")
        return set()

    try:
        resp = httpx.get(
            f"{SEMGREP_BASE}/api/v1/deployments/{DEPLOYMENT_SLUG}/findings",
            headers=_headers(token),
            params={"repos": repo, "page_size": 3000},
            timeout=_HTTP_TIMEOUT,
        )
    except httpx.HTTPError:
        logger.warning("perf_harvest: findings request failed", exc_info=True)
        return set()

    if resp.status_code != 200:
        logger.warning("perf_harvest: findings request returned %s", resp.status_code)
        return set()

    findings = resp.json().get("findings", [])
    ids: set[int] = set()
    for finding in findings:
        scan_id = finding.get("first_seen_scan_id")
        if scan_id is not None:
            ids.add(int(scan_id))
    return ids


def fetch_scan(scan_id: int, token: str) -> dict | None:
    """Fetch one scan record from the Semgrep App, or None on any failure."""
    try:
        resp = httpx.get(
            f"{SEMGREP_BASE}/api/agent/scans/{scan_id}",
            headers=_headers(token),
            timeout=_HTTP_TIMEOUT,
        )
    except httpx.HTTPError:
        logger.warning("perf_harvest: scan %s request failed", scan_id, exc_info=True)
        return None

    if resp.status_code != 200:
        return None
    return resp.json().get("scan")


def _normalize_env(raw: str) -> str:
    """Map the raw Semgrep environment enum to our normalized value.

    We only harvest SMS scans here (Route B rows come from report.py), so
    anything other than the managed-scans enum normalizes to "".
    """
    return "managed-scans" if raw == "SCAN_ENVIRONMENT_MANAGED_SCANS" else ""


def _parse_dt(s: Any) -> datetime | None:
    """Parse an ISO8601 timestamp, tolerating None/empty and a trailing 'Z'."""
    if not s:
        return None
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def scan_to_row(scan: dict) -> ScanPerf | None:
    """Map an API scan dict to a ScanPerf row, or None to skip.

    Only SMS scans (``environment == SCAN_ENVIRONMENT_MANAGED_SCANS``) are
    harvested; anything else (including our own Route B scans, which report to
    the App under a different environment) is skipped here.
    """
    raw_env = scan.get("environment", "")
    env = _normalize_env(raw_env)
    if env != "managed-scans":
        return None

    findings_counts = scan.get("findingsCounts") or {}
    branch = scan.get("branch", "")
    started = _parse_dt(scan.get("startedAt"))
    completed = _parse_dt(scan.get("completedAt"))
    # Comparison basis: the managed scan's startedAt->completedAt window (their
    # whole scan job). Semgrep's own `totalTime` is engine-only (~40-45% of this
    # window: it strips their queue/checkout/upload), which understated their real
    # turnaround against Route B's request-to-scan-complete time
    # (scan_execution_duration). start->end is the comparable whole-scan-job
    # number. Fall back to totalTime only when a timestamp is missing.
    if started is not None and completed is not None:
        total_time = (completed - started).total_seconds()
    else:
        total_time = float(scan.get("totalTime") or 0)
    return ScanPerf(
        scan_id=int(scan["id"]),
        environment=env,
        raw_environment=raw_env,
        is_full_scan=bool(scan.get("isFullScan")),
        branch=branch,
        scan_ref=branch,  # SMS has no separate PR ref field
        commit_sha=scan.get("commit", ""),
        total_time=total_time,
        findings_total=int(findings_counts.get("total") or 0),
        cli_version=scan.get("cliVersion", ""),
        scan_started_at=started,
        scan_completed_at=completed,
    )


def harvest_scans(session: Session, repo: str = GITHUB_REPO) -> dict:
    """Discover new SMS scan ids via findings, fetch each, and upsert SMS rows.

    Skips scan ids already stored (regardless of which environment they were
    stored under) so a repeat run is cheap. Returns a small summary dict and
    logs it, since this runs as a background task with no caller waiting on
    the result.
    """
    token = _token()
    ids = fetch_finding_scan_ids(repo, token)
    # SQLModel session.exec unwraps a single-column select to scalars (ints),
    # not Row tuples, so iterate the scalars directly (no unpacking).
    stored = set(session.exec(select(ScanPerf.scan_id)))

    harvested = 0
    for scan_id in sorted(ids - stored):
        scan = fetch_scan(scan_id, token)
        row = scan_to_row(scan) if scan else None
        if row is not None:
            upsert_scan_perf(session, row)
            harvested += 1

    summary = {
        "harvested": harvested,
        "candidates": len(ids),
        "skipped_existing": len(stored & ids),
    }
    logger.info(
        "perf_harvest: harvested=%s candidates=%s skipped_existing=%s",
        summary["harvested"],
        summary["candidates"],
        summary["skipped_existing"],
    )
    return summary
