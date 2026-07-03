"""Attention-decision logging (ADR 035 phase 3).

Engages are always logged; ignores are sampled by
``ATTENTION_IGNORE_SAMPLE_RATE`` (default 0.1) to bound volume, since most
ambient-channel traffic is an ignore.
"""

from __future__ import annotations

import logging
import os
import random

from sqlmodel import Session

from app.db import get_engine
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
