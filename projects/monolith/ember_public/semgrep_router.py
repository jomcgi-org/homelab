"""Public-safe HTTP API for the semgrep scan demo.

Mounted at ``/api/ember/semgrep`` on the public app: ``POST /session`` mints a
Turnstile-gated session cookie the same way the demo-postgres and bazel-query
sessions do, ``POST /scan`` session-gates, rate-limits, and queue-bounds a
scan against the production EmberVM semgrep workload
(``semgrep_scan.client.scan_files``), and ``GET /savings`` reads the all-time
scan-time-saved counter through a 30s cache.

Session admission uses ``chat_public.turnstile.siteverify`` (see
ember_public/router.py's ``postgres_session`` for the same pattern): when
``TURNSTILE_SECRET_KEY`` is unset (private tier) it stub-accepts.
"""

from __future__ import annotations

import logging
import secrets
import time

from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from chat_public.turnstile import siteverify
from ember_public import semgrep_core

# Module-level so the public binary's srcs must include semgrep_scan and
# main_public_imports_test verifies the closure stays public-safe in CI.
from semgrep_scan.client import scan_files

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/ember/semgrep", tags=["ember"])

_DEMO_SG_SESSION_COOKIE = "demo_sg_session"


class SemgrepScanRequest(BaseModel):
    language: str = ""
    content: str = ""


class SemgrepSessionRequest(BaseModel):
    turnstile_token: str = ""


@router.post("/session")
async def semgrep_session(
    body: SemgrepSessionRequest, request: Request, response: Response
) -> dict:
    """Mint a session cookie for the scan demo, gated by Turnstile when
    public. Mirrors ember_public/router.py's postgres_session.
    """
    existing = request.cookies.get(_DEMO_SG_SESSION_COOKIE, "")
    if existing:
        return {"ok": True, "existing": True}

    result = await siteverify(body.turnstile_token)
    if not result.success:
        raise HTTPException(status_code=403, detail="turnstile verification failed")

    response.set_cookie(
        _DEMO_SG_SESSION_COOKIE,
        secrets.token_hex(16),
        httponly=True,
        samesite="lax",
        secure=True,
        max_age=3600,
        path="/api/ember/semgrep",
    )
    return {"ok": True, "existing": False}


@router.post("/scan")
async def semgrep_scan_endpoint(body: SemgrepScanRequest, request: Request) -> dict:
    """Validate, session-gate, rate-limit, queue-bound, then scan against the
    production EmberVM semgrep workload.

    Check order: session cookie present (401), snippet validation (422), the
    per-session rate bucket (429), then the bounded queue (503 when both every
    slot and every waiting position are taken). A scan-side error from the
    workload comes back as 502 without accruing savings; a successful scan
    accrues savings best-effort and returns findings plus timing.
    """
    session_cookie = request.cookies.get(_DEMO_SG_SESSION_COOKIE, "")
    if not session_cookie:
        raise HTTPException(status_code=401, detail="scan a session first")

    error = semgrep_core.validate_snippet(body.language, body.content)
    if error is not None:
        raise HTTPException(status_code=422, detail=error)

    if not semgrep_core.check_and_record_scan(session_cookie):
        return JSONResponse(
            status_code=429,
            content={"error": "one scan per few seconds", "retry_after_s": 3},
        )

    before_slot = time.monotonic()
    try:
        async with semgrep_core.QUEUE.slot():
            queued_ms = (time.monotonic() - before_slot) * 1000
            scan_started = time.monotonic()
            snippet_file = {
                "path": semgrep_core.snippet_path(body.language),
                "content": body.content,
            }
            result = await scan_files([snippet_file], dedupe=False)
            scan_ms = (time.monotonic() - scan_started) * 1000
    except semgrep_core.QueueFullError:
        return JSONResponse(
            status_code=503,
            content={"busy": True, "waiting": semgrep_core.QUEUE.waiting},
        )

    if "error" in result:
        return JSONResponse(status_code=502, content={"error": result["error"]})

    await semgrep_core.record_demo_sg_savings(int(scan_ms))

    return {
        "findings": result.get("findings", []),
        "errors": result.get("errors", []),
        "scan_ms": scan_ms,
        "queued_ms": queued_ms,
        "saved_ms": semgrep_core.saved_ms(),
        "cold_start_ms": semgrep_core.COLD_START_MS,
    }


@router.get("/savings")
async def semgrep_savings() -> dict:
    """The all-time scan-time-saved counter, cached 30s.

    Reads through semgrep_core.cached_demo_sg_savings (reader engine,
    single-flight TTL cache), mirroring ember_public/router.py's
    postgres_savings. Never 5xxs: a missing table or read failure comes back
    with null fields.
    """
    return await semgrep_core.cached_demo_sg_savings()
