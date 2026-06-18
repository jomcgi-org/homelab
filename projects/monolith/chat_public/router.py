"""Internal-only HTTP API for public chat (ADR 005).

Mounted under ``/internal/chat`` and deliberately NOT on the public HTTPRoute:
the only internet-facing origin for chat is the public SvelteKit SSR app, which
proxies turns here over in-cluster Linkerd mTLS. The browser never talks to this
router directly.

Phase 1 stands up the session + limit machinery with NO GPU: the message
endpoint echoes a canned assistant response over SSE so the transport, session
authority, and budgets can be built and tested in isolation. Real inference
lands in Phase 3.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Body, Depends, Header, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlmodel import Session

from chat_public import limits, sessions
from chat_public.db import get_chat_session
from chat_public.sse import SSEEmitter

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/internal/chat", tags=["chat_public"])

# Phase 1 placeholder reply. Replaced by streamed vLLM output in Phase 3.
_CANNED_RESPONSE = (
    "Thanks for the message. Public chat is still being wired up, so this is a "
    "placeholder reply while the session and limit machinery is built. Real "
    "answers grounded in the public knowledge graph arrive in a later phase."
)


def _estimate_tokens(text: str) -> int:
    """Rough token estimate (~4 chars/token) for Phase 1 budget accounting.

    Real per-turn usage from the model replaces this in Phase 3.
    """
    return max(1, len(text) // 4)


class SessionCreateRequest(BaseModel):
    # Turnstile token forwarded by SSR. Verification is stubbed in Phase 1
    # (sessions.verify_turnstile), real siteverify in Phase 2.
    turnstile_token: str | None = None


class SessionCreateResponse(BaseModel):
    session_id: str


class MessageRequest(BaseModel):
    # SSR forwards the session id from the opaque cookie. Accepted in the body
    # or via the X-Chat-Session-Id header (body wins if both are set).
    session_id: str | None = None
    message: str


def _limit_status(code: str) -> int:
    """Map a LimitExceeded code to an HTTP status.

    char_cap is malformed input (400); the budget ceilings are quota
    exhaustion (429).
    """
    return 400 if code == "char_cap" else 429


@router.post("/session", response_model=SessionCreateResponse)
def create_chat_session(
    payload: SessionCreateRequest = Body(default_factory=SessionCreateRequest),
    db: Session = Depends(get_chat_session),
    cf_ipcountry: str | None = Header(default=None, alias="CF-IPCountry"),
    user_agent: str | None = Header(default=None, alias="User-Agent"),
) -> SessionCreateResponse:
    """Verify the (stubbed) Turnstile token and open a server-side session.

    Returns the opaque session id; SSR stores it in an httpOnly cookie. The
    row, not the cookie, is the authority for every later budget. The real
    client IP (for hashing) is forwarded by SSR in Phase 2; Phase 1 stores only
    coarse country and a user-agent hash.
    """
    session = sessions.create_session(
        db,
        turnstile_token=payload.turnstile_token,
        country=cf_ipcountry,
        user_agent=user_agent,
    )
    logger.info("chat_public.session.created turn_limit=%d", limits.MAX_TURNS)
    return SessionCreateResponse(session_id=session.id)


@router.post("/message")
def post_chat_message(
    payload: MessageRequest,
    db: Session = Depends(get_chat_session),
    header_session_id: str | None = Header(default=None, alias="X-Chat-Session-Id"),
) -> StreamingResponse:
    """Accept one user message for a session and stream the assistant reply.

    Server-authoritative: the only client input honored is the session id and a
    single user message string. Any client-supplied history is ignored; the
    transcript on the session row is the sole record. Budgets (char cap, max
    turns, per-session token ceiling) are enforced via ``limits.py`` before any
    reply is produced. Phase 1 streams a canned response over SSE.
    """
    session_id = payload.session_id or header_session_id
    session = sessions.load_active_session(db, session_id)
    if session is None:
        # Identical 404 for missing/expired/invalid: never leak which.
        raise HTTPException(status_code=404, detail="Session not found")

    try:
        limits.check_message_length(payload.message)
        limits.check_turns(session.turn_count)
        limits.check_session_tokens(session.total_tokens)
    except limits.LimitExceeded as exc:
        raise HTTPException(
            status_code=_limit_status(exc.code),
            detail={"code": exc.code, "message": exc.message},
        ) from exc

    # Persist the user message, then the canned assistant reply, then bump the
    # per-session counters. All client-supplied history (if any) is ignored.
    user_tokens = _estimate_tokens(payload.message)
    sessions.append_message(
        db, session, role="user", content=payload.message, tokens=user_tokens
    )

    reply = _CANNED_RESPONSE
    reply_tokens = min(_estimate_tokens(reply), limits.MAX_OUTPUT_TOKENS)
    sessions.append_message(
        db, session, role="assistant", content=reply, tokens=reply_tokens
    )
    sessions.record_turn(db, session, tokens=user_tokens + reply_tokens)

    emitter = SSEEmitter()
    emitter.emit("token", {"text": reply})
    emitter.emit(
        "done",
        {
            "turn_count": session.turn_count,
            "total_tokens": session.total_tokens,
        },
    )
    emitter.close()
    logger.info(
        "chat_public.message.served turn=%d total_tokens=%d",
        session.turn_count,
        session.total_tokens,
    )
    return StreamingResponse(emitter.stream(), media_type="text/event-stream")
