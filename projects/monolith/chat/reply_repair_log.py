"""Persist chat-agent reply-repair events (tool-call leak shield).

Every reply where the shield (chat.reply_sanitize) detected leaked tool-call
scaffolding is logged here, so the copy can be evaluated later and the reply /
plan prompts iterated against real failures. Mirrors ``attention_log``:
synchronous (opens its own session), best-effort (never raises into the caller,
since a logging failure must not block a reply). Call via
``asyncio.to_thread`` from the bot's async handlers.
"""

from __future__ import annotations

import logging

from sqlmodel import Session

from core.db import get_engine
from chat.models import AgentReplyRepair
from chat.reply_sanitize import RepairOutcome

logger = logging.getLogger(__name__)

# Guard rail so a pathological megabyte of leaked code never bloats the table.
_MAX_TEXT = 8000


def log_repair(
    channel_id: object,
    author_id: object,
    outcome: RepairOutcome,
    route: str = "chat",
) -> None:
    """Persist one reply-repair event. No-op when nothing leaked."""
    if not outcome.leaked:
        return
    try:
        with Session(get_engine()) as session:
            session.add(
                AgentReplyRepair(
                    channel_id=str(channel_id),
                    author_id=str(author_id),
                    route=route,
                    markers="tool_call",
                    raw_text=(outcome.raw or "")[:_MAX_TEXT],
                    scrubbed_text=(outcome.scrubbed or "")[:_MAX_TEXT],
                    final_text=(outcome.final or "")[:_MAX_TEXT],
                    repair_attempts=int(outcome.attempts),
                    outcome=outcome.outcome,
                )
            )
            session.commit()
    except Exception:
        logger.exception("reply_repair_log: failed to log repair event")
