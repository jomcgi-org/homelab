"""GitHub PR webhook that fires the fc-invoke -> Semgrep App reporting relay.

This is Phase 2 of self-hosted Semgrep CI reporting. GitHub POSTs a
``pull_request`` webhook here on every PR open/synchronize/reopen; we verify its
HMAC signature (fail-closed), acknowledge fast with a 200, and run the scan in a
background task so GitHub's delivery timeout is never held on the (slow) scan.

The background job gathers the PR's changed files, scans them on our own
Firecracker VM via ``client.scan_files`` (POST to the fc-invoke ``/invoke/semgrep``
daemon), then relays the resulting cli_output to the Semgrep AppSec Platform via
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
from semgrep_scan.report import report_pr_scan

logger = logging.getLogger("monolith.semgrep.webhook")

router = APIRouter(prefix="/webhooks/github", tags=["semgrep-webhook"])

# PR actions worth scanning: a fresh PR, a new push to an open PR, and a reopen.
# Everything else (labeled, closed, review requests, ...) is acked with no work.
_SCAN_ACTIONS = {"opened", "synchronize", "reopened"}

# File extensions the fc-invoke semgrep guest has rules for. A changed file with
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
    """Whether ``path`` has an extension the fc-invoke semgrep guest scans."""
    return path.endswith(_SCANNABLE_EXTS)


async def _list_changed_files(
    client: httpx.AsyncClient, repo: str, pr_number: int
) -> list[str]:
    """Return the scannable, non-removed changed-file paths for a PR (paginated).

    GET /repos/{repo}/pulls/{n}/files, 100 per page, following pages until a short
    page or the page cap. Only files whose ``status`` is not ``removed`` and whose
    extension is scannable are kept: a deleted file has no head content to fetch,
    and an unscannable extension has no rules.
    """
    paths: list[str] = []
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
            paths.append(path)
        if len(batch) < 100:
            break
    return paths


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


async def _gather_files(repo: str, pr_number: int, head_sha: str) -> list[dict]:
    """Build the ``[{path, content}]`` scan input for a PR's changed files.

    Lists the scannable changed files then fetches each one's head content. A file
    whose content cannot be fetched is dropped. Shares one AsyncClient across all
    the GitHub calls.
    """
    files: list[dict] = []
    timeout = httpx.Timeout(_GITHUB_TIMEOUT)
    async with httpx.AsyncClient(timeout=timeout) as client:
        paths = await _list_changed_files(client, repo, pr_number)
        for path in paths:
            content = await _fetch_file_content(client, repo, path, head_sha)
            if content is not None:
                files.append({"path": path, "content": content})
    return files


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

    Route B is in a VALIDATION (non-gating) phase, so we surface findings + latency
    as our own commit status on the real repo (a PAT with repo scope can post
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


async def _scan_and_report(payload: dict[str, Any]) -> None:
    """Background job: gather changed files, scan on fc-invoke, report to the App.

    Runs after the fast 200 so GitHub's delivery never blocks on the scan. Wrapped
    end-to-end in try/except: a failure here is logged, never crashes the app (an
    unhandled exception in a FastAPI BackgroundTask would otherwise surface as a
    500 for a response we already sent).
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

        files = await _gather_files(repo, int(pr_number), head_sha)
        if not files:
            logger.info(
                "semgrep webhook: no scannable changed files for %s#%s, nothing to do",
                repo,
                pr_number,
            )
            return

        # Wall-clock timer around the scan + report: the in-cluster (SigNoz-
        # queryable, via the structured log below) latency signal for comparing
        # Route B against SMS.
        started = time.monotonic()

        scan = await scan_files(files)
        if not isinstance(scan, dict) or scan.get("error"):
            logger.error(
                "semgrep webhook: fc-invoke scan failed for %s#%s: %s",
                repo,
                pr_number,
                (scan or {}).get("error") if isinstance(scan, dict) else scan,
            )
            return

        result = await report_pr_scan(
            repo=repo,
            branch=head_ref,
            commit=head_sha,
            pr_id=str(pr_number),
            base_ref=base_sha or None,
            raw_cli_output=scan.get("raw_cli_output"),
            repo_url=repo_url,
        )
        latency = time.monotonic() - started

        scan_id = result.get("scan_id")
        findings = result.get("findings_reported")
        project = result.get("project", repo)
        org = result.get("org", "jomcgi")

        if result.get("ok") and scan_id:
            logger.info(
                "semgrep webhook: reported %s#%s scan_id=%s findings=%s "
                "latency=%.2fs project=%s",
                repo,
                pr_number,
                scan_id,
                findings,
                latency,
                project,
            )
            # VALIDATION phase: non-gating, always "success". At cutover this maps
            # result["app_block_override"] -> "failure" else "success".
            await _post_commit_status(
                repo=repo,
                head_sha=head_sha,
                state="success",
                description=f"{findings} findings, {latency:.1f}s",
                target_url=_app_scan_url(org, project, scan_id),
            )
        else:
            error = result.get("error")
            logger.error(
                "semgrep webhook: App report failed for %s#%s: %s (latency=%.2fs)",
                repo,
                pr_number,
                error,
                latency,
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

    background_tasks.add_task(_scan_and_report, payload)
    return {"status": "accepted", "action": action}
