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

import os

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


class LimitExceeded(Exception):
    """Raised when a public-chat budget is exceeded.

    ``code`` is a stable machine string the router maps to a 4xx status and a
    "busy"/"limit" SSE event; ``message`` is a human-readable reason.
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
