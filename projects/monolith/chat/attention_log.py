"""Attention-decision logging (ADR 035 phase 3).

Engages are always logged; ignores are sampled by
``ATTENTION_IGNORE_SAMPLE_RATE`` (default 0.1) to bound volume, since most
ambient-channel traffic is an ignore.
"""

from __future__ import annotations

import logging
import os
import random

from sqlmodel import Session, select

from core.db import get_engine
from chat.models import AttentionDecision

logger = logging.getLogger(__name__)

ATTENTION_IGNORE_SAMPLE_RATE = float(
    os.environ.get("ATTENTION_IGNORE_SAMPLE_RATE", "0.1")
)


def log_decision(
    channel_id: object,
    message_id: object,
    decision: str,
    confidence: float,
    directive_version: int = 0,
    *,
    _rng=random.random,
) -> None:
    """Persist an attention decision.

    Engages are always logged; ignores are logged only when ``_rng()`` falls
    within ``ATTENTION_IGNORE_SAMPLE_RATE``. ``_rng`` is injectable so tests can
    force sampling deterministically. Synchronous (opens its own session); call
    via ``asyncio.to_thread`` from the bot's async handlers. Best-effort: never
    raises into the caller, since a logging failure should not block a reply.
    """
    if decision == "ignore" and _rng() > ATTENTION_IGNORE_SAMPLE_RATE:
        return
    try:
        with Session(get_engine()) as session:
            session.add(
                AttentionDecision(
                    channel_id=str(channel_id),
                    message_id=str(message_id),
                    decision=decision,
                    confidence=float(confidence),
                    directive_version=int(directive_version),
                )
            )
            session.commit()
    except Exception:
        logger.exception("attention: failed to log decision")


def set_reply_message(
    channel_id: object, trigger_message_id: object, reply_message_id: object
) -> None:
    """Attach the bot reply's discord id to the most recent engage decision for
    this trigger message, so reactions on the reply join back to the engage.

    Matched by channel_id + message_id + decision='engage', newest first. No-op
    if there is no engage row (e.g. a non-ambient reply, or an engage whose
    ignore was sampled out). Synchronous (opens its own session); call via
    ``asyncio.to_thread`` from the bot's async handlers. Best-effort: never
    raises into the caller, since a logging failure should not block a reply.
    """
    try:
        with Session(get_engine()) as session:
            row = session.exec(
                select(AttentionDecision)
                .where(AttentionDecision.channel_id == str(channel_id))
                .where(AttentionDecision.message_id == str(trigger_message_id))
                .where(AttentionDecision.decision == "engage")
                .order_by(AttentionDecision.id.desc())
            ).first()
            if row is None:
                return
            row.reply_message_id = str(reply_message_id)
            session.add(row)
            session.commit()
    except Exception:
        logger.exception("attention: failed to set reply message")


# The silent-path vocabulary written to attention_decision.withheld_reason. Kept
# here (not a DB CHECK) so a new path can be added without a schema migration;
# /improve-ambient groups on these to measure how often each gate withholds.
WITHHELD_AGENT_THREAD = "agent_thread"
WITHHELD_NO_REPLY = "no_reply"
WITHHELD_SEND_GATE = "send_gate"
WITHHELD_EMPTY_REPLY = "empty_reply"
# The classifier would have engaged, but the author is trust-locked-out (ADR
# chat/003): the engage was suppressed to a brig emoji, never sent. Logged so
# /improve-ambient can measure how often lockout withholds reply-worthy ambient.
WITHHELD_LOCKED_OUT = "locked_out"


def set_withheld_reason(
    channel_id: object, trigger_message_id: object, reason: str
) -> None:
    """Record WHY an ambient engage produced no in-channel reply, on the most
    recent engage decision for this trigger message.

    Matched by channel_id + message_id + decision='engage', newest first (same
    as ``set_reply_message``). No-op if there is no engage row (a non-ambient
    reply has none, and a live reply is never withheld anyway). Synchronous
    (opens its own session); call via ``asyncio.to_thread`` from the bot's async
    handlers. Best-effort: never raises into the caller, since a bookkeeping
    failure must not change whether the bot stays silent.
    """
    try:
        with Session(get_engine()) as session:
            row = session.exec(
                select(AttentionDecision)
                .where(AttentionDecision.channel_id == str(channel_id))
                .where(AttentionDecision.message_id == str(trigger_message_id))
                .where(AttentionDecision.decision == "engage")
                .order_by(AttentionDecision.id.desc())
            ).first()
            if row is None:
                return
            row.withheld_reason = str(reason)
            session.add(row)
            session.commit()
    except Exception:
        logger.exception("attention: failed to set withheld reason")
