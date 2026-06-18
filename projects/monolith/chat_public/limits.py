"""The single home for every public-chat budget check (ADR 005, layer 2+4).

Mirrors the ``visibility.py`` discipline from the V1 notes work: there is no
``if len(x) > ...`` anywhere else in ``chat_public``. Every per-session and
per-message ceiling is enforced here, so the limits are auditable in one place
and the tests assert against one set of knobs.

The config knobs are read from the environment once at import, with the Phase 0
starting values from the plan as defaults. The chart supplies them via env vars
on the public binary; changing a default here means grepping the test tree for
the old value (per CLAUDE.md).
"""

from __future__ import annotations

import contextlib
import os
import threading
from datetime import datetime, timedelta, timezone

from sqlalchemy import func
from sqlmodel import Session, select

from chat_public.models import ChatSession

# --------------------------------------------------------------------------
# Config knobs (ADR 005 / plan Phase 0 starting values). One place only.
# --------------------------------------------------------------------------

# Per-message character cap on the user's single submitted message.
CHAR_CAP = int(os.environ.get("CHAT_PUBLIC_CHAR_CAP", "8000"))

# Max conversational turns per session (one turn = one user message answered).
MAX_TURNS = int(os.environ.get("CHAT_PUBLIC_MAX_TURNS", "20"))

# Max output tokens the model may emit in a single turn.
MAX_OUTPUT_TOKENS = int(os.environ.get("CHAT_PUBLIC_MAX_OUTPUT_TOKENS", "1024"))

# Max total tokens (in + out, accumulated) a single session may spend.
MAX_SESSION_TOKENS = int(os.environ.get("CHAT_PUBLIC_MAX_SESSION_TOKENS", "32000"))

# Session / cookie time-to-live, in seconds. After this idle window a session
# is treated as expired and cannot be used for another turn.
SESSION_TTL_SECONDS = int(os.environ.get("CHAT_PUBLIC_SESSION_TTL_SECONDS", "1800"))

# Per-IP session-mint cap: the most sessions one (hashed) client IP may create in
# the trailing window below (ADR 005 layer 2, per-IP). This is the backend
# counter that complements Envoy's per-IP rate limit, so one IP cannot mint
# sessions without bound. The forwarded CF-Connecting-IP is trusted only because
# the backend is structurally reachable solely from the SSR mesh identity (see
# the -web Server + AuthorizationPolicy in the monolith-public linkerd-policy),
# so no untrusted peer can spoof it.
PER_IP_MINT_RATE = int(os.environ.get("CHAT_PUBLIC_PER_IP_MINT_RATE", "5"))

# Trailing window for the per-IP mint cap, in seconds (default 1 hour).
IP_MINT_WINDOW_SECONDS = int(
    os.environ.get("CHAT_PUBLIC_IP_MINT_WINDOW_SECONDS", "3600")
)

# Global circuit-breaker ceiling: the maximum number of public message/inference
# calls in flight across this process at once (ADR 005 layer 2, global backstop).
# Default 1 matches the Phase-0 reserved-headroom semaphore intent (start at 1,
# validate at load test). Over the ceiling the message path sheds with a "busy"
# event rather than spending a slot.
GLOBAL_MAX_CONCURRENT = int(os.environ.get("CHAT_PUBLIC_GLOBAL_MAX_CONCURRENT", "1"))


class LimitExceeded(Exception):
    """Raised when a public-chat admission/budget control rejects a request.

    ``code`` is a stable machine string the router maps to an HTTP status and a
    "busy"/"limit" SSE event; ``message`` is a human-readable reason. It covers
    both the per-session budgets (char_cap / max_turns / max_session_tokens) and
    the admission/shed controls (turnstile_failed / ip_mint_rate / busy), so the
    router has one rejection channel.
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def check_message_length(content: str) -> None:
    """Reject a user message longer than the per-message character cap."""
    if len(content) > CHAR_CAP:
        raise LimitExceeded(
            "char_cap",
            f"Message exceeds the {CHAR_CAP} character limit.",
        )


def check_turns(turn_count: int) -> None:
    """Reject a new turn once the per-session turn ceiling is reached."""
    if turn_count >= MAX_TURNS:
        raise LimitExceeded(
            "max_turns",
            f"This conversation has reached its {MAX_TURNS} turn limit.",
        )


def check_session_tokens(total_tokens: int) -> None:
    """Reject a new turn once the per-session token ceiling is reached."""
    if total_tokens >= MAX_SESSION_TOKENS:
        raise LimitExceeded(
            "max_session_tokens",
            f"This conversation has reached its {MAX_SESSION_TOKENS} token limit.",
        )


def check_ip_mint_rate(db: Session, ip_hash: str | None) -> None:
    """Reject session creation once a hashed IP has minted too many sessions.

    Counts sessions already created with the same ``ip_hash`` inside the trailing
    window and rejects over ``PER_IP_MINT_RATE``. The IP is keyed by its salted
    hash only (the raw IP is never stored). A None ip_hash (no forwarded IP, e.g.
    local dev) skips the check, since there is nothing to key on.
    """
    if not ip_hash:
        return
    window_start = datetime.now(timezone.utc) - timedelta(
        seconds=IP_MINT_WINDOW_SECONDS
    )
    count = db.exec(
        select(func.count())
        .select_from(ChatSession)
        .where(
            ChatSession.ip_hash == ip_hash,
            ChatSession.created_at >= window_start,
        )
    ).one()
    if count >= PER_IP_MINT_RATE:
        raise LimitExceeded(
            "ip_mint_rate",
            "Too many chat sessions from this network. Please try again later.",
        )


class _CircuitBreaker:
    """An in-process counter of in-flight public message/inference calls.

    ADR 005 layer 2 wants this global ceiling to be effective cluster-wide; a
    process-local counter is the simplest thing that is correct for a single
    replica and unit-testable now. Phase 3 wires the real reserved-headroom
    semaphore in front of vLLM, at which point this becomes (or is replaced by)
    the cluster-aware control. Deliberately NOT a Postgres-backed distributed
    counter: that is over-engineering for the current single-replica shape.

    A threading.Lock (not asyncio) is used because the Phase-2 message endpoint
    is a sync FastAPI handler dispatched to the threadpool, so concurrent turns
    run on different threads.
    """

    def __init__(self, limit: int) -> None:
        self._limit = limit
        self._inflight = 0
        self._lock = threading.Lock()

    def try_acquire(self) -> bool:
        with self._lock:
            if self._inflight >= self._limit:
                return False
            self._inflight += 1
            return True

    def release(self) -> None:
        with self._lock:
            if self._inflight > 0:
                self._inflight -= 1

    @property
    def inflight(self) -> int:
        with self._lock:
            return self._inflight


# Module-global breaker, sized from the env ceiling at import.
_breaker = _CircuitBreaker(GLOBAL_MAX_CONCURRENT)


def current_inflight() -> int:
    """In-flight public call count, for observability and tests."""
    return _breaker.inflight


@contextlib.contextmanager
def inflight_slot():
    """Acquire a global in-flight slot for one public message/inference call.

    Raises ``LimitExceeded("busy")`` when the global ceiling is already reached,
    so the caller sheds the request without spending a slot. Releases the slot on
    exit.

    TODO(Phase 3): when the message path becomes async and streams real vLLM
    tokens, the slot MUST be held for the whole generation (acquire before the
    stream, release when it completes), not just while the handler builds the
    response. In Phase 2 the canned reply is produced synchronously inside this
    context, so holding it for the handler body is sufficient.
    """
    if not _breaker.try_acquire():
        raise LimitExceeded(
            "busy",
            "Public chat is busy right now. Please try again in a moment.",
        )
    try:
        yield
    finally:
        _breaker.release()
