"""GitHub PR webhook that fires the EmberVM -> Semgrep App reporting relay.

This is Phase 2 of self-hosted Semgrep CI reporting. GitHub POSTs a
``pull_request`` webhook here on every PR open/synchronize/reopen; we verify its
HMAC signature (fail-closed), acknowledge fast with a 200, and run the scan in a
background task so GitHub's delivery timeout is never held on the (slow) scan.

The background job gathers the PR's changed files, scans them on our own
EmberVM via ``client.scan_files``, then relays the resulting cli_output to the
Semgrep AppSec Platform via
``report.report_pr_scan`` (Phase 1, live-proven), which posts the native PR check.

AUTH SURFACE (two independent secrets, both fail-closed):
- ``GITHUB_SEMGREP_WEBHOOK_SECRET``: the HMAC secret GitHub signs the request body
  with (``X-Hub-Signature-256: sha256=<hex>``). Unset -> every request is 401; we
  never accept an unsigned or unverifiable webhook.
- ``GITHUB_API_TOKEN``: a real GitHub token used to call the GitHub REST API (list
  changed files, fetch file contents). This is NOT the monolith's ``GITHUB_TOKEN``
  env, which is a ``kloak:`` placeholder swapped at the guest egress proxy and is
  useless for a direct API call; the chart wires the real token
  (``monolith-chat-secrets`` key ``GITHUB_TOKEN``, the same one git-mirror uses)
  into this env name.

The webhook path (``/webhooks/github/semgrep``) is reachable through the private
HTTPRoute WITHOUT a Cloudflare Access SecurityPolicy: GitHub bypasses Access at
the Cloudflare edge via an IP-allowlist Bypass policy, so its JWT-less request
would be rejected by a SecurityPolicy. The HMAC check above is the real gate.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import logging
import os
import time
from typing import Any
from urllib.parse import quote

import httpx
from fastapi import APIRouter, BackgroundTasks, Header, HTTPException, Request

from semgrep_scan.client import scan_files
from semgrep_scan.cohorts import diff_cohort
from semgrep_scan.report import report_pr_scan

logger = logging.getLogger("monolith.semgrep.webhook")

router = APIRouter(prefix="/webhooks/github", tags=["semgrep-webhook"])

internal_router = APIRouter(prefix="/internal/semgrep", tags=["semgrep-internal"])


# Guards against overlapping harvests: a second trigger while one is in flight
# is a no-op rather than a second concurrent sweep of the findings + scans API.
_harvest_in_flight = False
_harvest_tasks: set[asyncio.Task] = set()


@internal_router.post("/harvest-scans")
async def trigger_harvest(repo: str = "jomcgi/homelab") -> dict:
    """Fire the SMS scan-perf harvest for ``repo`` as a background task and
    return immediately. Internal only (see internal_router).

    Runs semgrep_scan.perf_harvest.harvest_scans in-process against a fresh DB
    session: sweep the deployment's findings for scan ids, fetch each new one,
    and upsert Semgrep Managed Scans rows into semgrep.scan_perf. This is
    API+DB only (no heavy semgrep import), so it is safe to run in the backend
    pod rather than the semgrep-full workload.
    """
    global _harvest_in_flight
    if _harvest_in_flight:
        return {"status": "already-running", "repo": repo}

    def _harvest() -> None:
        from sqlmodel import Session

        from core.db import get_engine
        from semgrep_scan.perf_harvest import harvest_scans

        with Session(get_engine()) as session:
            harvest_scans(session, repo)

    async def _run() -> None:
        global _harvest_in_flight
        _harvest_in_flight = True
        try:
            await asyncio.to_thread(_harvest)
        except Exception:
            logger.exception("trigger_harvest: harvest_scans crashed for %s", repo)
        finally:
            _harvest_in_flight = False

    task = asyncio.create_task(_run())
    _harvest_tasks.add(task)
    task.add_done_callback(_harvest_tasks.discard)
    logger.info("trigger_harvest: launched background scan-perf harvest for %s", repo)
    return {"status": "started", "repo": repo}


# Guards against overlapping cohort backfills.
_cohort_backfill_in_flight = False
_cohort_backfill_tasks: set[asyncio.Task] = set()


async def _backfill_cohorts(repo: str) -> dict:
    """Backfill diff-cohort metadata onto route-b perf rows that lack it.

    For each route-b row whose ``scan_ref`` is a ``refs/pull/{N}/merge`` and whose
    ``file_count`` is NULL, fetch the PR's ``/pulls/{N}/files`` (the same endpoint
    and the same ``diff_cohort`` the live webhook uses) and stamp the cohort. DB
    reads/writes go through worker threads (own sessions) per the async-handler
    rule; the GitHub calls are async. Best-effort per row.
    """
    import re

    from sqlmodel import Session, select

    from core.db import get_engine
    from semgrep_scan.perf_store import ScanPerf

    def _needing() -> list[tuple[int, str]]:
        with Session(get_engine()) as session:
            rows = session.exec(
                select(ScanPerf).where(
                    ScanPerf.environment == "route-b",
                    ScanPerf.file_count.is_(None),  # type: ignore[union-attr]
                )
            ).all()
            return [(r.scan_id, r.scan_ref) for r in rows]

    def _persist(scan_id: int, cohort: dict) -> None:
        with Session(get_engine()) as session:
            row = session.exec(
                select(ScanPerf).where(ScanPerf.scan_id == scan_id)
            ).first()
            if row is None:
                return
            row.file_count = cohort["file_count"]
            row.changed_lines = cohort["changed_lines"]
            row.languages = cohort["languages"]
            session.add(row)
            session.commit()

    rows = await asyncio.to_thread(_needing)
    pr_re = re.compile(r"refs/pull/(\d+)/merge")
    updated = skipped = 0
    async with httpx.AsyncClient(timeout=httpx.Timeout(_GITHUB_TIMEOUT)) as client:
        for scan_id, scan_ref in rows:
            match = pr_re.match(scan_ref or "")
            if not match:
                skipped += 1
                continue
            try:
                entries = await _list_changed_files(client, repo, int(match.group(1)))
                await asyncio.to_thread(_persist, scan_id, diff_cohort(entries))
                updated += 1
            except Exception:
                logger.exception("cohort backfill failed for scan %s", scan_id)
                skipped += 1
    logger.info(
        "cohort backfill: updated=%d skipped=%d of %d route-b rows",
        updated,
        skipped,
        len(rows),
    )
    return {"updated": updated, "skipped": skipped, "total": len(rows)}


@internal_router.post("/backfill-cohorts")
async def trigger_cohort_backfill(repo: str = "jomcgi/homelab") -> dict:
    """Fire the one-time cohort backfill (GitHub API) as a background task.

    Internal only. Populates the diff-cohort columns on historical route-b rows
    from each PR's ``/pulls/{N}/files``; new rows get the cohort live in the
    webhook. Idempotent (only touches rows with a NULL file_count).
    """
    global _cohort_backfill_in_flight
    if _cohort_backfill_in_flight:
        return {"status": "already-running", "repo": repo}

    async def _run() -> None:
        global _cohort_backfill_in_flight
        _cohort_backfill_in_flight = True
        try:
            await _backfill_cohorts(repo)
        except Exception:
            logger.exception("trigger_cohort_backfill crashed for %s", repo)
        finally:
            _cohort_backfill_in_flight = False

    task = asyncio.create_task(_run())
    _cohort_backfill_tasks.add(task)
    task.add_done_callback(_cohort_backfill_tasks.discard)
    logger.info("trigger_cohort_backfill: launched cohort backfill for %s", repo)
    return {"status": "started", "repo": repo}


# PR actions worth scanning: a fresh PR, a new push to an open PR, and a reopen.
# Everything else (labeled, closed, review requests, ...) is acked with no work.
_SCAN_ACTIONS = {"opened", "synchronize", "reopened"}

# File extensions the EmberVM semgrep guest has rules for. A changed file with
# any other extension (or a deletion) is skipped: fetching + scanning it would be
# wasted work the guest has no rules to match against.
_SCANNABLE_EXTS = (".py", ".go", ".js", ".jsx", ".ts", ".tsx", ".rs")

# GitHub API base + the User-Agent it requires on every request.
_GITHUB_API = "https://api.github.com"
_USER_AGENT = "homelab-semgrep-webhook"

# Bounded page count so a pathological PR with thousands of files cannot make the
# background job page forever. 100 items/page * 30 pages = 3000 files is plenty.
_MAX_FILE_PAGES = 30
_GITHUB_TIMEOUT = 30.0
# Max concurrent GitHub contents fetches when gathering a PR's changed files.
# The per-file fetches are independent WAN round trips, so a serial loop made
# gather scale linearly with diff size (SigNoz: up to 63s on big PRs); a bounded
# gather collapses it toward one round trip. The bound keeps a large PR from
# opening hundreds of sockets or tripping GitHub secondary rate limits.
_GATHER_CONCURRENCY = 8


# Route diffs with at least this many changed files to the heavier semgrep-hi
# workload (6 vCPU, parallel multi-file match) instead of the warm 1-vCPU
# `semgrep`. Measured crossover (debug-pod sweep): semgrep-hi carries a fixed
# ~0.9s/scan thread-pool overhead that makes it a net loss below ~5 files, and a
# clear win above (8 heavy files: ~10.6s vs ~38.9s at 1 vCPU). Tune from the
# per-cohort perf data once collected.
def _verify_signature(body: bytes, signature_header: str | None) -> None:
    """Verify GitHub's ``X-Hub-Signature-256`` over the raw body, fail-closed.

    GitHub signs the exact request body with HMAC-SHA256 keyed on the shared
    secret and sends ``sha256=<hexdigest>``. We recompute and constant-time
    compare. An unset secret denies every request (never accept an unverifiable
    webhook), as does a missing or mismatched signature. Raises 401 on any
    failure; returns None on success.
    """
    secret = os.environ.get("GITHUB_SEMGREP_WEBHOOK_SECRET", "")
    if not secret:
        raise HTTPException(status_code=401, detail="webhook secret not configured")
    if not signature_header or not signature_header.startswith("sha256="):
        raise HTTPException(status_code=401, detail="missing signature")
    expected = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    presented = signature_header[len("sha256=") :]
    if not hmac.compare_digest(presented, expected):
        raise HTTPException(status_code=401, detail="invalid signature")


def _github_headers() -> dict[str, str]:
    """Auth + content headers for the GitHub REST API.

    Uses ``GITHUB_API_TOKEN`` (the REAL token wired from monolith-chat-secrets),
    NOT the ``kloak:`` placeholder in ``GITHUB_TOKEN``. Missing token raises so the
    background job logs a clear cause rather than 401-ing silently on every call.
    """
    token = os.environ.get("GITHUB_API_TOKEN", "")
    if not token:
        raise RuntimeError("GITHUB_API_TOKEN is not set; cannot call the GitHub API")
    return {
        "Authorization": f"Bearer {token}",
        "User-Agent": _USER_AGENT,
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def _is_scannable(path: str) -> bool:
    """Whether ``path`` has an extension the EmberVM semgrep guest scans."""
    return path.endswith(_SCANNABLE_EXTS)


async def _list_changed_files(
    client: httpx.AsyncClient, repo: str, pr_number: int
) -> list[dict]:
    """Return the scannable, non-removed changed-file ENTRIES for a PR (paginated).

    Each entry is the GitHub file object (``filename``, ``status``, ``additions``,
    ``deletions``): callers need the path to fetch content AND the diff stats for
    cohort metadata. GET /repos/{repo}/pulls/{n}/files, 100 per page, following
    pages until a short page or the page cap. Only files whose ``status`` is not
    ``removed`` and whose extension is scannable are kept: a deleted file has no
    head content to fetch, and an unscannable extension has no rules.
    """
    entries: list[dict] = []
    for page in range(1, _MAX_FILE_PAGES + 1):
        resp = await client.get(
            f"{_GITHUB_API}/repos/{repo}/pulls/{pr_number}/files",
            headers=_github_headers(),
            params={"per_page": 100, "page": page},
        )
        resp.raise_for_status()
        batch = resp.json()
        if not batch:
            break
        for entry in batch:
            path = entry.get("filename", "")
            status = entry.get("status", "")
            if status == "removed" or not _is_scannable(path):
                continue
            entries.append(entry)
        if len(batch) < 100:
            break
    return entries


async def _fetch_file_content(
    client: httpx.AsyncClient, repo: str, path: str, head_sha: str
) -> str | None:
    """Fetch one file's text at ``head_sha`` via the contents API, or None.

    GET /repos/{repo}/contents/{path}?ref={head_sha} returns base64 ``content``;
    we decode it to text. Returns None (and logs) on any failure so one unreadable
    file (e.g. a symlink or a too-large blob the contents API declines to inline)
    never aborts the whole scan.
    """
    try:
        resp = await client.get(
            f"{_GITHUB_API}/repos/{repo}/contents/{path}",
            headers=_github_headers(),
            params={"ref": head_sha},
        )
        resp.raise_for_status()
        payload = resp.json()
        encoded = payload.get("content")
        if not encoded or payload.get("encoding") != "base64":
            return None
        return base64.b64decode(encoded).decode("utf-8", errors="replace")
    except Exception:
        logger.exception("failed to fetch content for %s@%s", path, head_sha)
        return None


async def _gather_files(
    repo: str, pr_number: int, head_sha: str
) -> tuple[list[dict], dict]:
    """Build the ``[{path, content}]`` scan input plus the diff cohort for a PR.

    Lists the scannable changed-file entries, computes the cohort (file count,
    changed lines, per-language breakdown) from their diff stats, then fetches
    each file's head content. A file whose content cannot be fetched is dropped
    from the scan input but still counts toward the cohort (it was part of the
    diff). Shares one AsyncClient across all the GitHub calls.
    """
    timeout = httpx.Timeout(_GITHUB_TIMEOUT)
    async with httpx.AsyncClient(timeout=timeout) as client:
        entries = await _list_changed_files(client, repo, pr_number)
        cohort = diff_cohort(entries)

        # Fetch changed-file contents concurrently (bounded). asyncio.gather
        # preserves the input order, so files stay in changed-file order; a file
        # whose content cannot be fetched is dropped.
        sem = asyncio.Semaphore(_GATHER_CONCURRENCY)

        async def _fetch(path: str) -> dict | None:
            async with sem:
                content = await _fetch_file_content(client, repo, path, head_sha)
            if content is None:
                return None
            return {"path": path, "content": content}

        fetched = await asyncio.gather(
            *(_fetch(entry["filename"]) for entry in entries)
        )
    return [f for f in fetched if f is not None], cohort


# The commit-status context string GitHub shows on the PR. Namespaced so it is
# obviously the self-hosted Route B signal and never collides with SMS's native
# Semgrep check.
_STATUS_CONTEXT = "route-b/semgrep"


def _app_scan_url(org: str, project: str, scan_id: Any) -> str:
    """Best-effort App URL for a Route B scan, for the status ``target_url``.

    The App scans live under an org-scoped project slug. We url-encode the project
    (it contains a ``/``, e.g. ``jomcgi/homelab-selfhosted``) so it is one path
    segment. ASSUMPTION: this ``.../projects/{project}/scans/{scan_id}`` shape is
    the shadow-project scan URL; it is a convenience link, not load-bearing, so a
    format drift only breaks the click-through, never the scan itself.
    """
    return (
        f"https://semgrep.dev/orgs/{org}/projects/"
        f"{quote(project, safe='')}/scans/{scan_id}"
    )


async def _post_commit_status(
    *,
    repo: str,
    head_sha: str,
    state: str,
    description: str,
    target_url: str | None,
) -> None:
    """POST a GitHub commit status to the REAL repo's PR head sha, best-effort.

    Route B is in a VALIDATION (non-gating) phase, so we surface findings +
    scan_pr_wall_time as our own commit status on the real repo (a PAT with repo
    scope can post
    statuses even though it cannot create Check Runs, which is why we use statuses
    and not a Check Run). The status is posted to ``{real_repo}`` (from the webhook
    ``repository.full_name``), NOT the shadow project the App report used.

    A status failure must NEVER crash the scan job, so the whole POST is wrapped in
    try/except and only logged. GitHub caps ``description`` at 140 chars.
    """
    try:
        body: dict[str, Any] = {
            "state": state,
            "context": _STATUS_CONTEXT,
            "description": description[:140],
        }
        if target_url:
            body["target_url"] = target_url
        async with httpx.AsyncClient(timeout=httpx.Timeout(_GITHUB_TIMEOUT)) as client:
            resp = await client.post(
                f"{_GITHUB_API}/repos/{repo}/statuses/{head_sha}",
                headers=_github_headers(),
                json=body,
            )
            resp.raise_for_status()
    except Exception:
        logger.exception(
            "semgrep webhook: failed to post %s commit status to %s@%s",
            _STATUS_CONTEXT,
            repo,
            head_sha,
        )


async def _scan_and_report(payload: dict[str, Any], received: float) -> None:
    """Background job: gather changed files, scan on EmberVM, report to the App.

    Runs after the fast 200 so GitHub's delivery never blocks on the scan. Wrapped
    end-to-end in try/except: a failure here is logged, never crashes the app (an
    unhandled exception in a FastAPI BackgroundTask would otherwise surface as a
    500 for a response we already sent).

    ``received`` is the ``time.monotonic()`` stamp taken at webhook receipt, so
    the wall-time metric spans the whole PR check the developer waits on (gather +
    scan + report + status post), not just the engine.

    Two headline timing metrics are logged (both ``scan_*`` so they group in
    SigNoz), plus per-segment debug fields:

    - ``scan_execution_duration`` (s): the EmberVM engine scan ONLY. Also sent
      to the App as the scan's ``total_time``.
    - ``scan_pr_wall_time`` (s): webhook receipt -> developer-visible commit
      status posted. The whole PR check, minus only the GitHub-send-to-us hop we
      cannot clock here. Also shown in the commit-status description.
    - ``gather_ms`` / ``report_ms`` / ``status_ms``: secondary per-segment spans
      so a bad ``scan_pr_wall_time`` can be attributed without a headline metric.
    """
    try:
        pull_request = payload.get("pull_request", {})
        repository = payload.get("repository", {})
        repo = repository.get("full_name", "")
        repo_url = repository.get("html_url") or None
        pr_number = payload.get("number") or pull_request.get("number")
        head = pull_request.get("head", {})
        base = pull_request.get("base", {})
        head_ref = head.get("ref", "")
        head_sha = head.get("sha", "")
        base_sha = base.get("sha", "")

        if not (repo and pr_number and head_sha):
            logger.warning(
                "semgrep webhook: incomplete PR payload (repo=%r pr=%r head=%r)",
                repo,
                pr_number,
                head_sha,
            )
            return

        t_gather = time.monotonic()
        files, cohort = await _gather_files(repo, int(pr_number), head_sha)
        gather_ms = (time.monotonic() - t_gather) * 1000
        if not files:
            logger.info(
                "semgrep webhook: no scannable changed files for %s#%s, nothing to do",
                repo,
                pr_number,
            )
            return

        # scan_execution_duration: the engine scan ONLY (EmberVM round trip),
        # measured on its own so it is both the App total_time and a clean
        # "how fast is our scanner" signal, independent of gather/report/status.
        t_scan = time.monotonic()
        scan = await scan_files(files)
        scan_execution_duration = time.monotonic() - t_scan
        if not isinstance(scan, dict) or scan.get("error"):
            logger.error(
                "semgrep webhook: EmberVM scan failed for %s#%s: %s",
                repo,
                pr_number,
                (scan or {}).get("error") if isinstance(scan, dict) else scan,
            )
            return

        t_report = time.monotonic()
        result = await report_pr_scan(
            repo=repo,
            branch=head_ref,
            commit=head_sha,
            pr_id=str(pr_number),
            base_ref=base_sha or None,
            raw_cli_output=scan.get("raw_cli_output"),
            repo_url=repo_url,
            scan_execution_duration=scan_execution_duration,
            cohort=cohort,
        )
        report_ms = (time.monotonic() - t_report) * 1000

        scan_id = result.get("scan_id")
        findings = result.get("findings_reported")
        project = result.get("project", repo)
        org = result.get("org", "jomcgi")

        # Wall time up to just before the developer-visible status post. The
        # status body cannot contain its own post latency, so the description
        # shows this (receipt -> result ready); the log records the full
        # scan_pr_wall_time (including the post) after it returns.
        wall_pre_status = time.monotonic() - received

        if result.get("ok") and scan_id:
            # VALIDATION phase: non-gating, always "success". At cutover this maps
            # result["app_block_override"] -> "failure" else "success".
            t_status = time.monotonic()
            await _post_commit_status(
                repo=repo,
                head_sha=head_sha,
                state="success",
                description=f"{findings} findings, {wall_pre_status:.1f}s",
                target_url=_app_scan_url(org, project, scan_id),
            )
            status_ms = (time.monotonic() - t_status) * 1000
            scan_pr_wall_time = time.monotonic() - received
            logger.info(
                "semgrep webhook: reported %s#%s scan_id=%s findings=%s "
                "scan_execution_duration=%.2fs scan_pr_wall_time=%.2fs project=%s "
                "gather_ms=%.0f report_ms=%.0f status_ms=%.0f",
                repo,
                pr_number,
                scan_id,
                findings,
                scan_execution_duration,
                scan_pr_wall_time,
                project,
                gather_ms,
                report_ms,
                status_ms,
            )
        else:
            error = result.get("error")
            scan_pr_wall_time = time.monotonic() - received
            logger.error(
                "semgrep webhook: App report failed for %s#%s: %s "
                "(scan_execution_duration=%.2fs scan_pr_wall_time=%.2fs)",
                repo,
                pr_number,
                error,
                scan_execution_duration,
                scan_pr_wall_time,
            )
            # Report failed (no scan_id / not ok): surface it as an error status
            # with no target_url (there is no App scan to link to).
            await _post_commit_status(
                repo=repo,
                head_sha=head_sha,
                state="error",
                description=f"report failed: {error}",
                target_url=None,
            )
    except Exception:
        logger.exception("semgrep webhook: background scan/report crashed")


@router.post("/semgrep")
async def semgrep_webhook(
    request: Request,
    background_tasks: BackgroundTasks,
    x_hub_signature_256: str | None = Header(default=None),
    x_github_event: str | None = Header(default=None),
) -> dict:
    """GitHub PR webhook entrypoint: verify, filter, fast-ack, background-scan.

    1. Verify the HMAC signature over the raw body (fail-closed; 401 on any miss).
    2. Ignore non-``pull_request`` events (including ``ping``) and unsupported
       actions with a 200 no-op, so GitHub sees a healthy hook and does not retry.
    3. For a scannable action, dispatch ``_scan_and_report`` as a background task
       and return 200 immediately; the scan never blocks the response.
    """
    # Stamp receipt as early as possible: scan_pr_wall_time is measured from here
    # to the commit-status post, i.e. the whole PR check the developer waits on.
    received = time.monotonic()
    body = await request.body()
    _verify_signature(body, x_hub_signature_256)

    # Non-PR events (ping, push, issue, ...) are acknowledged with no work.
    if x_github_event != "pull_request":
        return {"status": "ignored", "reason": f"event={x_github_event}"}

    try:
        payload = await request.json()
    except Exception:
        # A pull_request event with an unparseable body is acked (not retried) but
        # not acted on; a genuine GitHub delivery is always valid JSON.
        logger.warning("semgrep webhook: pull_request event with unparseable body")
        return {"status": "ignored", "reason": "unparseable body"}

    action = payload.get("action", "")
    if action not in _SCAN_ACTIONS:
        return {"status": "ignored", "reason": f"action={action}"}

    background_tasks.add_task(_scan_and_report, payload, received)
    return {"status": "accepted", "action": action}
