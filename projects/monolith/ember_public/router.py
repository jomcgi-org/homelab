"""Public-safe HTTP API for the demo-postgres exhibit.

Mounted at ``/api/ember/postgres`` on BOTH the public app (Turnstile-gated) and
the private app (the authenticated demos panel), so the two tiers serve
identical paths against one implementation:

- ``GET  /status``  the demo-postgres stateful lifecycle (sleep indicator).
- ``POST /query``   timed connect (the wake) + insert or aggregate against demo-postgres.
- ``POST /session`` mint a session cookie for ledger attribution (Turnstile-gated when public).

``POST /reset`` (destroy the live VM + evict its snapshot) stays private-only;
see ``demos/firecracker_api.py``.

Session admission uses ``chat_public.turnstile.siteverify`` (retries transient
egress failures, fails closed): when ``TURNSTILE_SECRET_KEY`` is unset (the
private tier, behind Cloudflare Access) it stub-accepts, matching this
endpoint's private-tier "mint without token" requirement.
"""

from __future__ import annotations

import asyncio
import logging
import secrets
from time import perf_counter
from typing import Literal

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel

from chat_public.turnstile import siteverify
from ember_public import core

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/ember/postgres", tags=["ember"])

_DEMO_PG_SESSION_COOKIE = "demo_pg_session"


class PostgresQueryRequest(BaseModel):
    mode: Literal["insert", "aggregate"] = "insert"


class PostgresSessionRequest(BaseModel):
    turnstile_token: str = ""


@router.get("/status")
async def postgres_status() -> dict:
    """The demo-postgres lifecycle snapshot driving the sleep indicator.

    A management-API read only: the frontend polls this sub-second while the
    exhibit is on screen, and because no TCP connection ever reaches the
    workload's listener the poll cannot keep the VM awake or wake it. Errors
    come back in-band (not as 5xx) so one flaky poll doesn't redline the UI.
    """
    if not core.EMBERVM_URL or not core.demo_pg_dsn():
        return {"configured": False}
    try:
        status = await core.fetch_demo_pg_status()
    except Exception as exc:  # noqa: BLE001 - poll errors are data, not faults
        logger.warning("demo-postgres status poll failed: %s", exc)
        return {"configured": True, "error": str(exc)}
    shaped = core.shape_pg_status(status)
    total = await core.record_demo_pg_savings(shaped["state"], shaped["generation"])
    if total is not None:
        return {"configured": True, **shaped, "total_saved_mib_s": total}
    return {"configured": True, **shaped}


@router.post("/query")
async def postgres_query(body: PostgresQueryRequest, request: Request) -> dict:
    """Timed roundtrip against demo-postgres: the wake IS the connect time.

    Captures the lifecycle state just before connecting to classify what this
    connect is about to pay for (warm / relight / cold), then measures the
    connect and the SQL work separately. A failed connect is returned in-band
    (mirroring the python demo's error shape): the likeliest causes are the
    wake-rate limiter refusing a burst and a wake genuinely failing, both of
    which the visitor recovers from by waiting a beat and retrying.
    """
    dsn = core.demo_pg_dsn()
    if not dsn:
        raise HTTPException(
            status_code=503, detail="DEMO_POSTGRES_DSN is not configured"
        )

    session_tag = core.demo_pg_session_tag(
        request.cookies.get(_DEMO_PG_SESSION_COOKIE, "")
    )

    before = None
    if core.EMBERVM_URL:
        try:
            before = await core.fetch_demo_pg_status()
        except Exception as exc:  # noqa: BLE001 - classification is best-effort
            logger.warning("demo-postgres pre-query status failed: %s", exc)
            before = None

    started = perf_counter()
    try:
        result = await asyncio.to_thread(
            core.demo_pg_orders_roundtrip, dsn, body.mode, session_tag
        )
    except Exception as exc:  # noqa: BLE001 - surface connect/query failures in-band
        logger.warning("demo-postgres query roundtrip failed: %s", exc)
        return {
            "error": str(exc),
            "mode": body.mode,
            "classification": core.classify_wake(before),
            "phase_before": (before or {}).get("state"),
            "total_ms": (perf_counter() - started) * 1000,
        }

    return {
        **result,
        "total_ms": (perf_counter() - started) * 1000,
        "classification": core.classify_wake(before),
        "phase_before": (before or {}).get("state"),
        "generation": (before or {}).get("generation"),
        "error": None,
    }


@router.post("/session")
async def postgres_session(
    body: PostgresSessionRequest, request: Request, response: Response
) -> dict:
    """Mint a session cookie for ledger attribution, gated by Turnstile when
    the demo is public.

    Private tier (behind Cloudflare Access) leaves TURNSTILE_SECRET_KEY unset,
    so ``siteverify`` stub-accepts and this mints without verification. Once a
    public page sits in front of this endpoint, setting the env turns on
    real server-side Turnstile verification so anonymous visitors can't mint
    sessions without solving the challenge.
    """
    existing = request.cookies.get(_DEMO_PG_SESSION_COOKIE, "")
    if existing:
        return {"ok": True, "existing": True}

    result = await siteverify(body.turnstile_token)
    if not result.success:
        raise HTTPException(status_code=403, detail="turnstile verification failed")

    response.set_cookie(
        _DEMO_PG_SESSION_COOKIE,
        secrets.token_hex(16),
        httponly=True,
        samesite="lax",
        secure=True,
        max_age=3600,
        path="/api/ember/postgres",
    )
    return {"ok": True, "existing": False}
