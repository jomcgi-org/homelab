"""Semgrep AppSec Platform scan webhook -> semgrep.scan_perf.

Semgrep POSTs a ``semgrep_scan`` object every time a scan completes, including
scans with NO findings. That is the key property the findings-derived harvest
lacks: the harvest can only discover a managed scan that left an open finding,
so a clean scan (or a re-scan that surfaces nothing new) is invisible to it.
This webhook captures every managed scan's runtime for the perf comparison in
near-real-time, findings-independent. The hourly harvest stays as a backstop
for any delivery this misses.

AUTH (fail-closed, defense-in-depth): Semgrep signs ``semgrep_finding`` events
with ``SEMGREP_WEBHOOK_SECRET`` (``X-Semgrep-Signature-256: <hex>``, HMAC-SHA256)
but delivers ``semgrep_scan`` (scan-completion) events UNSIGNED. So:
  - Signed request -> HMAC-verify (match against the raw body OR Semgrep's
    canonical compact re-serialization; both need the secret).
  - Unsigned request -> accept ONLY from Semgrep's egress IPs (CF-Connecting-IP,
    set by Cloudflare and not client-spoofable since the origin is reachable only
    via the CF tunnel), then rely on the API fetch in _capture_scan to verify the
    scan is real. The webhook payload data is never trusted; we use it only for
    the scan id and re-fetch the authoritative record.
This captures the unsigned scan events (the ones carrying runtime) while never
accepting an unverifiable, un-sourced request.

The path ``/webhooks/semgrep`` is reachable through the private ingress via a
Cloudflare Access IP-Bypass for Semgrep's egress IPs (Semgrep sends no Access
JWT); the route carries no SecurityPolicy, so this HMAC is the only gate.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import time

from fastapi import APIRouter, BackgroundTasks, Header, HTTPException, Request

logger = logging.getLogger("monolith.semgrep.perf_webhook")

router = APIRouter(prefix="/webhooks/semgrep", tags=["semgrep-scan-webhook"])

# Semgrep's documented egress IPs (observed as the webhook source: 52.34.137.110
# and 52.35.248.246). Semgrep signs semgrep_finding events but delivers
# semgrep_scan (scan completion) events UNSIGNED, so we cannot HMAC-verify those.
# We instead accept an unsigned request only from these source IPs. CF-Connecting-IP
# is set by Cloudflare and is not client-spoofable (the origin has no direct
# internet exposure, only the CF tunnel reaches it), so it is a trustworthy gate.
# The scan is then further verified by fetching its authoritative record from the
# Semgrep API in _capture_scan (the webhook payload data is never trusted).
_SEMGREP_WEBHOOK_IPS = frozenset(
    {
        "35.166.231.235",
        "52.35.248.246",
        "52.34.137.110",
        "44.225.64.41",
    }
)


def _verify_signature(body: bytes, signature_header: str | None) -> None:
    """Verify Semgrep's ``X-Semgrep-Signature-256`` over the body, fail-closed.

    Accepts an HMAC-SHA256 hex digest that matches either the raw body or its
    canonical compact re-serialization. An unset secret, a missing header, or a
    mismatch all raise 401 (never accept an unverifiable webhook).
    """
    secret = os.environ.get("SEMGREP_WEBHOOK_SECRET", "")
    if not secret:
        raise HTTPException(status_code=401, detail="webhook secret not configured")
    if not signature_header:
        raise HTTPException(status_code=401, detail="missing signature")
    presented = signature_header.strip()
    key = secret.encode("utf-8")
    candidates = [hmac.new(key, body, hashlib.sha256).hexdigest()]
    try:
        canonical = json.dumps(json.loads(body), separators=(",", ":")).encode("utf-8")
        candidates.append(hmac.new(key, canonical, hashlib.sha256).hexdigest())
    except (ValueError, TypeError):
        pass
    if not any(hmac.compare_digest(presented, c) for c in candidates):
        raise HTTPException(status_code=401, detail="invalid signature")


def _extract_scan_id(payload: dict) -> int | None:
    # Semgrep delivers the scan event wrapped in an envelope:
    # {"semgrep_scan": {"id": ..., ...}} (confirmed live). Unwrap it, then read
    # the id, tolerating a "scan_id" alias or a nested "scan": {"id": ...} too so
    # a minor shape change does not silently drop the capture.
    if isinstance(payload.get("semgrep_scan"), dict):
        payload = payload["semgrep_scan"]
    raw = payload.get("id")
    if raw is None:
        raw = payload.get("scan_id")
    if raw is None and isinstance(payload.get("scan"), dict):
        raw = payload["scan"].get("id")
    try:
        return int(raw) if raw is not None else None
    except (ValueError, TypeError):
        return None


def _managed_row_for(scan_id: int, attempts: int = 8, delay_s: float = 8.0):
    """Fetch the scan record and return a ScanPerf row iff it is a managed scan,
    else None. Reuses the harvest's fetch + classify so managed scans are
    captured identically; our own scans (env UNSPECIFIED) classify to None and
    are skipped here (report.py is authoritative for those).

    The completion webhook races the API: totalTime can still read 0 for a few
    seconds after the scan completes. Since a real scan is never genuinely 0s,
    retry the fetch until total_time is populated (a 0-finding managed scan is
    invisible to the findings harvest, so nothing else would ever correct a 0)."""
    from semgrep_scan.perf_harvest import fetch_scan, scan_to_row

    token = os.environ.get("SEMGREP_APP_TOKEN", "")
    row = None
    for attempt in range(attempts):
        scan = fetch_scan(scan_id, token)
        row = scan_to_row(scan) if scan else None
        # Not a managed scan (or not found): stop, nothing to wait for.
        if row is None:
            return None
        if row.total_time > 0 or attempt == attempts - 1:
            return row
        time.sleep(delay_s)
    return row


def _capture_scan(scan_id: int) -> None:
    """Off-request: pull the scan record and upsert it if managed. Best-effort;
    a failure is logged and swallowed so a bad delivery never 500s the webhook."""
    from sqlmodel import Session

    from core.db import get_engine
    from semgrep_scan.perf_store import upsert_scan_perf

    try:
        row = _managed_row_for(scan_id)
        if row is None:
            logger.info(
                "semgrep scan webhook: scan %s not a managed scan, skipped", scan_id
            )
            return
        # Read the values before the session closes: SQLAlchemy expires ORM
        # attributes on commit, so accessing row.total_time after the `with`
        # block would lazy-reload against a closed session and raise.
        total_time = row.total_time
        findings_total = row.findings_total
        with Session(get_engine()) as session:
            upsert_scan_perf(session, row)
        logger.info(
            "semgrep scan webhook: captured managed scan %s (%.1fs, %d findings)",
            scan_id,
            total_time,
            findings_total,
        )
    except Exception:
        logger.exception("semgrep scan webhook: failed to capture scan %s", scan_id)


@router.post("")
@router.post("/")
async def semgrep_scan_webhook(
    request: Request,
    background_tasks: BackgroundTasks,
    x_semgrep_signature_256: str | None = Header(default=None),
) -> dict:
    """Verify the signature, ack fast, and capture the scan off-request.

    Only the ``semgrep_scan`` object (a JSON dict with an ``id``) is handled;
    ``semgrep_finding`` events (JSON arrays) are acknowledged and ignored.
    """
    body = await request.body()
    client_ip = request.headers.get("cf-connecting-ip") or (
        request.client.host if request.client else "?"
    )
    # Observability: log every inbound delivery before verification so a real
    # Semgrep scan webhook is visible even if it later 401s or is ignored (a
    # 401 otherwise leaves no trace). Cheap: webhooks are low-frequency.
    logger.info(
        "semgrep webhook inbound: ip=%s bytes=%d has_sig=%s",
        client_ip,
        len(body),
        bool(x_semgrep_signature_256),
    )
    # Auth: a signed request (semgrep_finding events) is HMAC-verified. An
    # unsigned request (semgrep_scan / scan-completion events, which Semgrep
    # delivers without a signature) is accepted only from Semgrep's egress IPs;
    # _capture_scan then fetch-verifies the scan against the Semgrep API, so the
    # payload data is never trusted. Anything else is rejected (fail-closed).
    if x_semgrep_signature_256:
        _verify_signature(body, x_semgrep_signature_256)
    elif client_ip not in _SEMGREP_WEBHOOK_IPS:
        raise HTTPException(
            status_code=401, detail="unsigned request from unrecognized source"
        )
    try:
        payload = json.loads(body)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="invalid json") from exc
    if not isinstance(payload, dict):
        logger.info(
            "semgrep webhook: non-dict payload (type=%s), ignored",
            type(payload).__name__,
        )
        return {"status": "ignored", "reason": "not a semgrep_scan object"}
    scan_id = _extract_scan_id(payload)
    # Log the payload keys (not values) so we can see the real semgrep_scan
    # shape and confirm which key carries the scan id.
    logger.info(
        "semgrep webhook payload: keys=%s scan_id=%s",
        sorted(payload.keys()),
        scan_id,
    )
    if scan_id is None:
        return {"status": "ignored", "reason": "no scan id"}
    background_tasks.add_task(_capture_scan, scan_id)
    return {"status": "accepted", "scan_id": scan_id}
