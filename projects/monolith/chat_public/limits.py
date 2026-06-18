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

# No per-IP session-mint cap: Turnstile is anonymous proof-of-humanity, not a
# user identity, and a per-IP cap mostly penalises NAT-shared legitimate users
# without stopping IP-rotating abusers. The real protections are aggregate (the
# global circuit breaker below + the Phase 3 reserved-headroom semaphore) plus
# the per-session budgets above. ip_hash is still stored (see sessions.py) for
# reactive abuse forensics and targeted blocking, not a pre-emptive cap.

# Global circuit-breaker ceiling: the maximum number of public message/inference
# calls in flight across this process at once (ADR 005 layer 2, global backstop).
# Default 1 matches the Phase-0 reserved-headroom semaphore intent (start at 1,
# validate at load test). Over the ceiling the message path sheds with a "busy"
# event rather than spending a slot.
#
# This is ALSO the reserved-headroom GPU-isolation control (ADR 005 layer 3): it
# is per-pod (deliberately, see _CircuitBreaker) and bounds how many public
# requests are in flight to the shared vLLM at once, leaving decode slots for the
# Discord bot, private chat, and the agent platform. Phase 6 (load test) tunes
# the final reservation; the sizing rule is
#     GLOBAL_MAX_CONCURRENT * web.maxReplicas + reserved_trusted <= max_num_seqs
# (vLLM batch capacity, today max_num_seqs=3). Per-pod is Joe's decision: do NOT
# make it a distributed counter.
GLOBAL_MAX_CONCURRENT = int(os.environ.get("CHAT_PUBLIC_GLOBAL_MAX_CONCURRENT", "1"))

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
    """An in-process counter of in-flight public message/inference calls.

    This is a process-local counter: it bounds in-flight turns PER REPLICA. A
    distributed counter is deliberately NOT used; per-pod is fine at this scale as
    long as the AGGREGATE is sized sensibly. The web component has an HPA, so the
    cluster-wide public in-flight ceiling is GLOBAL_MAX_CONCURRENT * replicas.

    SIZING RULE (reserved-headroom, ADR 005 layer 3): choose GLOBAL_MAX_CONCURRENT
    and the inference-bearing replica count together so that
        GLOBAL_MAX_CONCURRENT * web.maxReplicas + reserved_trusted <= max_num_seqs
    (vLLM batch capacity, today max_num_seqs=3). That keeps decode slots reserved
    for the Discord bot, private chat, and agents, with a simple per-pod limit.
    Final reservation tuning is a Phase 6 load-test concern.

    A threading.Lock (not asyncio) is used so the counter is correct whether it is
    touched from the event loop (the Phase 3 async streaming path acquires/releases
    around the whole generation) or from a threadpool worker. try_acquire/release
    are non-blocking and hold no lock across IO, so calling them from async code is
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


# Module-global breaker, sized from the env ceiling at import.
_breaker = _CircuitBreaker(GLOBAL_MAX_CONCURRENT)


def current_inflight() -> int:
    """In-flight public call count, for observability and tests."""
    return _breaker.inflight


def try_acquire_slot() -> bool:
    """Try to take a global in-flight slot. False when the ceiling is reached.

    The async streaming path (router._turn_stream) acquires the slot at the start
    of the SSE generator and releases it in a finally, so the slot is held for the
    ENTIRE generation, not just while the handler is building the response. This
    is the Phase 3 fix for the earlier release-before-stream behaviour: a public
    request occupies a reserved-headroom slot for exactly as long as it is on the
    GPU. The module global is looked up dynamically so tests can swap _breaker.
    """
    return _breaker.try_acquire()


def release_slot() -> None:
    """Release a previously acquired global in-flight slot."""
    _breaker.release()


@contextlib.contextmanager
def inflight_slot():
    """Context-manager form of try_acquire_slot/release_slot.

    Raises ``LimitExceeded("busy")`` when the global ceiling is already reached,
    so a synchronous caller sheds without spending a slot, and releases on exit.
    The async streaming path uses try_acquire_slot/release_slot directly so the
    slot can be held across the SSE generator's lifetime rather than just a
    ``with`` block.
    """
    if not try_acquire_slot():
        raise LimitExceeded("busy", BUSY_MESSAGE)
    try:
        yield
    finally:
        release_slot()
