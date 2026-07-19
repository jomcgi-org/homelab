"""Public-safe HTTP API for the bazel skyframe query demo (ADR embervm/010).

Mounted at ``/api/ember/bazel`` on the public app, Turnstile-gated the same
way the demo-postgres session is: ``POST /session`` mints a cookie once a
Turnstile token is verified, and ``POST /query`` requires that cookie before
submitting to the ``bazel-query`` EmberVM workload.

Session admission uses ``chat_public.turnstile.siteverify`` (see
bazel_core.py's neighbour, ember_public/router.py, for the same pattern):
when ``TURNSTILE_SECRET_KEY`` is unset (private tier) it stub-accepts.
"""

from __future__ import annotations

import logging
import secrets

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel

from chat_public import turnstile
from chat_public.turnstile import siteverify
from ember_public import bazel_core

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/ember/bazel", tags=["ember"])

_BAZEL_SESSION_COOKIE = "bazel_query_session"


class BazelQueryRequest(BaseModel):
    expression: str = ""


class BazelSessionRequest(BaseModel):
    turnstile_token: str = ""


@router.post("/query")
async def bazel_query(body: BazelQueryRequest, request: Request) -> dict:
    """Validate, session-gate, rate-limit, then submit to the bazel-query
    workload. Every rejection is returned in-band (never a 5xx for a visitor
    mistake), matching the demo-postgres query endpoint's shape.
    """
    error = bazel_core.validate_expr(body.expression)
    if error is not None:
        return {"error": error}

    session_cookie = request.cookies.get(_BAZEL_SESSION_COOKIE, "")
    if not session_cookie:
        if turnstile.SECRET_KEY:
            return {"error": "solve the challenge first", "session_required": True}
        session_cookie = "sessionless"

    if not bazel_core.check_and_record_query(session_cookie):
        return {"error": "one query per few seconds", "rate_limited": True}

    if not bazel_core.try_acquire_query_slot():
        return {"error": "busy, one moment", "busy": True}

    try:
        status, payload = await bazel_core.run_query(body.expression)
    finally:
        bazel_core.release_query_slot()

    if status != 200:
        raise HTTPException(status_code=status, detail=payload.get("error"))
    return payload


@router.post("/session")
async def bazel_session(
    body: BazelSessionRequest, request: Request, response: Response
) -> dict:
    """Mint a session cookie for the query rate limit, gated by Turnstile when
    the demo is public. Mirrors ember_public/router.py's postgres_session.
    """
    existing = request.cookies.get(_BAZEL_SESSION_COOKIE, "")
    if existing:
        return {"ok": True, "existing": True}

    result = await siteverify(body.turnstile_token)
    if not result.success:
        raise HTTPException(status_code=403, detail="turnstile verification failed")

    response.set_cookie(
        _BAZEL_SESSION_COOKIE,
        secrets.token_hex(16),
        httponly=True,
        samesite="lax",
        secure=True,
        max_age=3600,
        path="/api/ember/bazel",
    )
    return {"ok": True, "existing": False}
