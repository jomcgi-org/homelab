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

from chat_public import turnstile
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
async def postgres_status(request: Request) -> dict:
    """The demo-postgres lifecycle snapshot driving the sleep indicator.

    A management-API read only: the frontend polls this sub-second while the
    exhibit is on screen, and because no TCP connection ever reaches the
    workload's listener the poll cannot keep the VM awake or wake it. Errors
    come back in-band (not as 5xx) so one flaky poll doesn't redline the UI.

    Reads through the 500ms single-flight cache (core.cached_demo_pg_status)
    so a burst of concurrent pollers shares one control-plane read.

    The optional ``p`` query param is the caller's ephemeral per-page client id
    (not the insert session cookie, which the status proxy never forwards). We
    stamp it into the presence tracker and return ``present``, the live count
    of visitors watching, so the shared VM's warmth reads as a crowd rather
    than a ghost wake. Presence is computed per-request (outside the status
    cache), so concurrent pollers each record their own id.
    """
    if not core.EMBERVM_URL or not core.demo_pg_dsn():
        return {"configured": False}
    client_id = request.query_params.get("p", "")
    if client_id:
        core.record_presence(client_id)
    present = core.present_count()
    try:
        status = await core.cached_demo_pg_status()
    except Exception as exc:  # noqa: BLE001 - poll errors are data, not faults
        logger.warning("demo-postgres status poll failed: %s", exc)
        return {"configured": True, "present": present, "error": str(exc)}
    shaped = core.shape_pg_status(status)
    total = await core.record_demo_pg_savings(shaped["state"], shaped["generation"])
    if total is not None:
        return {
            "configured": True,
            "present": present,
            **shaped,
            "total_saved_mib_s": total,
        }
    return {"configured": True, "present": present, **shaped}


@router.post("/query")
async def postgres_query(body: PostgresQueryRequest, request: Request) -> dict:
    """Timed roundtrip against demo-postgres: the wake IS the connect time.

    Captures the lifecycle state just before connecting to classify what this
    connect is about to pay for (warm / relight / cold), then measures the
    connect and the SQL work separately. A failed connect is returned in-band
    (mirroring the python demo's error shape): the likeliest causes are the
    wake-rate limiter refusing a burst and a wake genuinely failing, both of
    which the visitor recovers from by waiting a beat and retrying.

    Three in-band gates guard the roundtrip, each returned as its own error
    shape so the frontend's in-band backoff can distinguish them:
    - a session is required for inserts once Turnstile is configured (public
      tier); the private tier, which leaves TURNSTILE_SECRET_KEY unset, still
      allows sessionless inserts.
    - one insert per session per second (core.check_and_record_insert).
    - a global semaphore around the roundtrip (core.try_acquire_query_slot),
      exhausted under a concurrent burst.
    """
    dsn = core.demo_pg_dsn()
    if not dsn:
        raise HTTPException(
            status_code=503, detail="DEMO_POSTGRES_DSN is not configured"
        )

    session_tag = core.demo_pg_session_tag(
        request.cookies.get(_DEMO_PG_SESSION_COOKIE, "")
    )

    if body.mode == "insert":
        if session_tag is None:
            if turnstile.SECRET_KEY:
                return {
                    "error": "solve the challenge first",
                    "session_required": True,
                    "mode": body.mode,
                }
        elif not core.check_and_record_insert(session_tag):
            return {
                "error": "one order per second",
                "rate_limited": True,
                "mode": body.mode,
            }

    before = None
    if core.EMBERVM_URL:
        try:
            before = await core.cached_demo_pg_status()
        except Exception as exc:  # noqa: BLE001 - classification is best-effort
            logger.warning("demo-postgres pre-query status failed: %s", exc)
            before = None

    if not core.try_acquire_query_slot():
        return {
            "error": "busy, one moment",
            "busy": True,
            "mode": body.mode,
            "classification": core.classify_wake(before),
            "phase_before": (before or {}).get("state"),
        }

    started = perf_counter()
    try:
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
    finally:
        core.release_query_slot()

    return {
        **result,
        "total_ms": (perf_counter() - started) * 1000,
        "classification": core.classify_wake(before),
        "phase_before": (before or {}).get("state"),
        "generation": (before or {}).get("generation"),
        "error": None,
    }


@router.get("/savings")
async def postgres_savings() -> dict:
    """The all-time "memory saved while asleep" counter, cached 30s.

    Reads through core.cached_demo_pg_savings (reader engine, single-flight
    TTL cache) so a burst of pollers shares one DB read. Never 5xxs: a
    missing table or read failure comes back as total_saved_mib_s: null.
    """
    return await core.cached_demo_pg_savings()


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
