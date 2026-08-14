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

import logging
import os
import threading

logger = logging.getLogger(__name__)

# --------------------------------------------------------------------------
# Config knobs (ADR 005 / plan Phase 0 starting values). One place only.
# --------------------------------------------------------------------------

# Per-message character cap on the user's single submitted message.
CHAR_CAP = int(os.environ.get("CHAT_PUBLIC_CHAR_CAP", "8000"))

# Max conversational turns per session (one turn = one user message answered).
MAX_TURNS = int(os.environ.get("CHAT_PUBLIC_MAX_TURNS", "20"))

# Max output tokens the model may emit in a single turn.
MAX_OUTPUT_TOKENS = int(os.environ.get("CHAT_PUBLIC_MAX_OUTPUT_TOKENS", "4000"))

# Max total tokens (in + out, accumulated) a single session may spend.
MAX_SESSION_TOKENS = int(os.environ.get("CHAT_PUBLIC_MAX_SESSION_TOKENS", "32000"))

# Session / cookie time-to-live, in seconds. After this idle window a session
# is treated as expired and cannot be used for another turn.
SESSION_TTL_SECONDS = int(os.environ.get("CHAT_PUBLIC_SESSION_TTL_SECONDS", "1800"))

# No per-IP session-mint cap: Turnstile is anonymous proof-of-humanity, not a
# user identity, and a per-IP cap mostly penalises NAT-shared legitimate users
# without stopping IP-rotating abusers. The real protections are aggregate (the
# global circuit breaker below + the Phase 3 reserved-headroom semaphore) plus
# the per-session budgets above. ip_hash is still stored (see sessions.py) for
# reactive abuse forensics and targeted blocking, not a pre-emptive cap.

# Global in-flight ceiling: the maximum number of public inference calls in
# flight at once, across the WHOLE cluster (ADR 005 layer 2+3). Over the ceiling
# the message path sheds with a "busy" event rather than spending a slot.
#
# This is the reserved-headroom GPU-isolation control: it bounds how many public
# requests are in flight to the shared vLLM at once, leaving decode slots for the
# Discord bot, private chat, and the agent platform. It is now CLUSTER-WIDE, not
# per-pod: the public web backend's HPA can scale to several replicas, so a
# per-pod counter would multiply public load on the shared GPU and starve trusted
# callers. The ceiling is enforced in Postgres (advisory locks, see acquire_slot)
# so it holds across every replica; the in-process _CircuitBreaker below is only
# the single-process fallback for the SQLite dev/test path.
#
# SIZING RULE (reserved-headroom): public aggregate in-flight inference is capped
# at GLOBAL_MAX_CONCURRENT across ALL pods, and the remaining decode slots are
# left for trusted callers (Discord, private chat, agents, grimoire extraction).
# The in-cluster engine is llama.cpp with 3 slots, so public is capped at 1.
#
# Keep this proportional if the slot count changes. It is an abuse boundary, not
# a latency knob, so it deliberately does NOT follow the "synchronous callers are
# uncapped" policy that trusted interactive traffic gets: public chat is
# synchronous too, and the point is precisely that anonymous traffic cannot crowd
# out the people the GPU exists for.
#
# Production always sets the env explicitly (monolith-public chart, web.env), so
# this default governs dev and test only. It is kept in step with the chart so
# the two cannot tell different stories about what the ceiling is.
GLOBAL_MAX_CONCURRENT = int(os.environ.get("CHAT_PUBLIC_GLOBAL_MAX_CONCURRENT", "1"))

# Advisory-lock namespace (classid) for the GPU limiter. A stable app-specific
# magic so these locks never collide with any other advisory lock; the objid is
# the slot index in [0, GLOBAL_MAX_CONCURRENT).
_ADVISORY_CLASSID = int(os.environ.get("CHAT_PUBLIC_ADVISORY_CLASSID", "1129270594"))

# --------------------------------------------------------------------------
# Compaction knobs (ADR 005 layer 4 / plan Phase 3). When the live context
# (system prompt + rolling summary + recent turns) approaches a fraction of the
# model window, older turns are folded into the rolling summary so each request
# stays bounded. See chat_public.summarizer + sessions.compact_if_needed.
# --------------------------------------------------------------------------

# Usable context window of the shared model, in tokens. Compaction is sized as a
# fraction of this. It is a tuning knob, not a hard model property: keep it at or
# below the real vLLM context length.
MODEL_WINDOW_TOKENS = int(os.environ.get("CHAT_PUBLIC_MODEL_WINDOW_TOKENS", "32768"))

# Fraction of the model window at which compaction triggers. At 0.70 the older
# turns are summarised once the estimated live context crosses ~70% of the
# window, keeping headroom for the new turn and its reply.
COMPACTION_TRIGGER = float(os.environ.get("CHAT_PUBLIC_COMPACTION_TRIGGER", "0.70"))

# How many of the most recent transcript messages are kept verbatim after a
# compaction (everything older is folded into the rolling summary). Six messages
# is roughly the last three turns. This caps summary frequency: once a summary
# exists the live context is summary + this tail, which stays small.
COMPACTION_KEEP_MESSAGES = int(
    os.environ.get("CHAT_PUBLIC_COMPACTION_KEEP_MESSAGES", "6")
)

# Max tokens the rolling-summary generation may emit. A summary is meant to be
# short, and it spends GPU under the same in-flight slot as a real turn, so cap
# it tightly (ADR 005: a summary is far cheaper than an unbounded context).
SUMMARY_MAX_TOKENS = int(os.environ.get("CHAT_PUBLIC_SUMMARY_MAX_TOKENS", "512"))

# Human-readable shed message reused by the router's "busy" SSE event.
BUSY_MESSAGE = "Public chat is busy right now. Please try again in a moment."


def estimate_tokens(text: str) -> int:
    """Rough token estimate (~4 chars/token).

    Used to decide when compaction should trigger and as a fallback for per-turn
    token accounting when the model does not report usage. Real usage from the
    model is always preferred when available; this is only an estimate.
    """
    return max(1, len(text or "") // 4)


def should_compact(estimated_context_tokens: int) -> bool:
    """True when the estimated live context has crossed the compaction trigger."""
    return estimated_context_tokens >= COMPACTION_TRIGGER * MODEL_WINDOW_TOKENS


class LimitExceeded(Exception):
    """Raised when a public-chat admission/budget control rejects a request.

    ``code`` is a stable machine string the router maps to an HTTP status and a
    "busy"/"limit" SSE event; ``message`` is a human-readable reason. It covers
    both the per-session budgets (char_cap / max_turns / max_session_tokens) and
    the admission/shed controls (turnstile_failed / busy), so the router has one
    rejection channel.
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


class _CircuitBreaker:
    """In-process counter of in-flight public inference calls (FALLBACK path).

    The cluster-wide ceiling is enforced in Postgres (advisory locks, see
    acquire_slot). This process-local counter is only the single-process fallback
    for the SQLite dev/test path, where there is no Postgres to hold an advisory
    lock and the process is the whole cluster anyway.

    A threading.Lock (not asyncio) is used so the counter is correct whether it is
    touched from the event loop (the async streaming path acquires/releases around
    the whole generation) or from a threadpool worker. try_acquire/release are
    non-blocking and hold no lock across IO, so calling them from async code is
    safe and never blocks the loop.
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


# Module-global in-process breaker (SQLite dev/test fallback only), sized from
# the env ceiling at import. The cluster-wide ceiling is the Postgres advisory
# locks in acquire_slot; this counter is never used on the Postgres path.
_breaker = _CircuitBreaker(GLOBAL_MAX_CONCURRENT)


def current_inflight() -> int:
    """In-flight count on the in-process fallback breaker (dev/test gauge)."""
    return _breaker.inflight


class _LocalSlot:
    """A held in-process slot (SQLite dev/test path): release decrements the
    in-process breaker."""

    def __init__(self, breaker: _CircuitBreaker) -> None:
        self._breaker = breaker

    def release(self) -> None:
        self._breaker.release()


class _AdvisorySlot:
    """A held cluster-wide slot: a Postgres session-level advisory lock on a
    dedicated connection.

    Crash-safety: a session-level advisory lock is held only for the lifetime of
    its connection. If the pod crashes mid-stream the connection drops and
    Postgres releases the lock automatically, so a slot is never permanently
    leaked. On a clean release we unlock and then invalidate the connection, so a
    pooled connection can never carry a stale advisory lock back into the pool.
    """

    def __init__(self, conn, objid: int) -> None:
        self._conn = conn
        self._objid = objid

    def release(self) -> None:
        try:
            self._conn.exec_driver_sql("SELECT pg_advisory_unlock_all()")
        except Exception:  # noqa: BLE001 - invalidate still drops the lock
            logger.warning(
                "chat_public.slot.unlock_failed; invalidating connection",
                exc_info=True,
            )
        finally:
            # invalidate() drops the physical connection so no pooled connection
            # can carry a still-held advisory lock; close() returns the wrapper.
            self._conn.invalidate()
            self._conn.close()


def _acquire_advisory_slot(bind) -> _AdvisorySlot | None:
    """Grab any free cluster-wide slot via pg_try_advisory_lock, or None if all
    GLOBAL_MAX_CONCURRENT slots are held. The acquiring connection is held by the
    returned handle for the whole stream and released in release_slot."""
    # AUTOCOMMIT so the long-lived lock connection never sits idle-in-transaction
    # for the duration of a stream; the advisory lock is session-scoped, so it is
    # held by the connection regardless of transaction state.
    conn = bind.connect().execution_options(isolation_level="AUTOCOMMIT")
    try:
        for objid in range(GLOBAL_MAX_CONCURRENT):
            got = conn.exec_driver_sql(
                "SELECT pg_try_advisory_lock(%s, %s)", (_ADVISORY_CLASSID, objid)
            ).scalar()
            if got:
                return _AdvisorySlot(conn, objid)
        # Every slot is held elsewhere: we acquired none, so just drop our probe
        # connection and shed.
        conn.close()
        return None
    except Exception:
        conn.close()
        raise


def acquire_slot(db):
    """Acquire a global in-flight GPU slot, or return None when the ceiling is hit.

    Cluster-wide via Postgres advisory locks on the public_writer chat engine
    (the engine behind ``db``); falls back to the in-process breaker on the SQLite
    path (single-process dev/tests). The slot is held by the caller for the whole
    SSE stream and freed via release_slot in a finally. A Postgres/limiter error
    fails CLOSED (shed busy) rather than running ungoverned inference.
    """
    bind = db.get_bind()
    if bind.dialect.name == "postgresql":
        try:
            return _acquire_advisory_slot(bind)
        except Exception:  # noqa: BLE001 - fail closed: shed rather than run ungoverned
            logger.warning(
                "chat_public.slot.acquire_failed; shedding as busy", exc_info=True
            )
            return None
    if _breaker.try_acquire():
        return _LocalSlot(_breaker)
    return None


def release_slot(slot) -> None:
    """Release a previously acquired slot handle (no-op for None)."""
    if slot is not None:
        slot.release()
