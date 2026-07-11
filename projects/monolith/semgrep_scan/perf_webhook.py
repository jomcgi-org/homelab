"""Semgrep AppSec Platform scan webhook -> semgrep.scan_perf.

Semgrep POSTs a ``semgrep_scan`` object every time a scan completes, including
scans with NO findings. That is the key property the findings-derived harvest
lacks: the harvest can only discover a managed scan that left an open finding,
so a clean scan (or a re-scan that surfaces nothing new) is invisible to it.
This webhook captures every managed scan's runtime for the perf comparison in
near-real-time, findings-independent. The hourly harvest stays as a backstop
for any delivery this misses.

AUTH (fail-closed): Semgrep signs with ``SEMGREP_WEBHOOK_SECRET`` and sends
``X-Semgrep-Signature-256: <hex>`` (HMAC-SHA256, hex, no prefix). Semgrep's docs
compute the digest over the canonical ``json.dumps(payload, separators=(",",
":"))`` rather than necessarily the exact bytes on the wire, so we accept a
match against EITHER the raw body OR that canonical re-serialization. Both
require the shared secret, so accepting either is safe and it removes a whole
class of whitespace-mismatch silent-401s. An unset secret denies every request.

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

from fastapi import APIRouter, BackgroundTasks, Header, HTTPException, Request

logger = logging.getLogger("monolith.semgrep.perf_webhook")

router = APIRouter(prefix="/webhooks/semgrep", tags=["semgrep-scan-webhook"])


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
    raw = payload.get("id")
    try:
        return int(raw) if raw is not None else None
    except (ValueError, TypeError):
        return None


def _managed_row_for(scan_id: int):
    """Fetch the scan record and return a ScanPerf row iff it is a managed scan,
    else None. Reuses the harvest's fetch + classify so managed scans are
    captured identically; our own scans (env UNSPECIFIED) classify to None and
    are skipped here (report.py is authoritative for those)."""
    from semgrep_scan.perf_harvest import fetch_scan, scan_to_row

    token = os.environ.get("SEMGREP_APP_TOKEN", "")
    scan = fetch_scan(scan_id, token)
    return scan_to_row(scan) if scan else None


def _capture_scan(scan_id: int) -> None:
    """Off-request: pull the scan record and upsert it if managed. Best-effort;
    a failure is logged and swallowed so a bad delivery never 500s the webhook."""
    from sqlmodel import Session

    from app.db import get_engine
    from semgrep_scan.perf_store import upsert_scan_perf

    try:
        row = _managed_row_for(scan_id)
        if row is None:
            logger.info(
                "semgrep scan webhook: scan %s not a managed scan, skipped", scan_id
            )
            return
        with Session(get_engine()) as session:
            upsert_scan_perf(session, row)
        logger.info(
            "semgrep scan webhook: captured managed scan %s (%.1fs, %d findings)",
            scan_id,
            row.total_time,
            row.findings_total,
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
    _verify_signature(body, x_semgrep_signature_256)
    try:
        payload = json.loads(body)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="invalid json") from exc
    if not isinstance(payload, dict):
        return {"status": "ignored", "reason": "not a semgrep_scan object"}
    scan_id = _extract_scan_id(payload)
    if scan_id is None:
        return {"status": "ignored", "reason": "no scan id"}
    background_tasks.add_task(_capture_scan, scan_id)
    return {"status": "accepted", "scan_id": scan_id}


# webhook live-check: temporary no-op comment to trigger a scan (PR will be closed)
