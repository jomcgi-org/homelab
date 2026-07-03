"""Attention gate (ADR 035 phase 3): should the bot engage with a message?

Mentions and replies to the bot always engage (no model call). In channels with
an ambient grant, a classify-only fast-model call scores the message against the
channel directive and engages only above ATTENTION_THRESHOLD. Everywhere else,
ignore. The classifier holds no tools and fails closed (ignore) on any error.
"""

import json
import logging
import os
from dataclasses import dataclass

logger = logging.getLogger(__name__)
ATTENTION_THRESHOLD = float(os.environ.get("ATTENTION_THRESHOLD", "0.8"))


@dataclass
class AttentionResult:
    engage: bool
    confidence: float


async def evaluate(
    message, directive: str, bot_user, is_ambient: bool, *, _caller=None
) -> AttentionResult:
    """Decide whether to engage. See module docstring.

    ``directive`` is the channel directive text (empty until Phase 5 wires it).
    ``_caller`` is an injectable llm-caller for tests; defaults to
    ``build_llm_caller()``.
    """
    from chat.bot import should_respond  # mention/reply detection (lazy to avoid cycle)

    if should_respond(message, bot_user):
        return AttentionResult(True, 1.0)
    if not is_ambient:
        return AttentionResult(False, 0.0)
    # Ambient channel: classify.
    try:
        caller = _caller
        if caller is None:
            from chat.summarizer import build_llm_caller

            caller = build_llm_caller()
        text = (message.content or "")[:500]
        prompt = (
            "You decide whether an assistant should jump into a chat message. "
            "Channel directive (how the assistant should behave here): "
            + (directive or "(none)")
            + "\nMessage: "
            + text
            + '\nReply with ONLY a JSON object: {"engage": true|false, '
            '"confidence": 0.0-1.0}. Engage only if the message clearly wants '
            "the assistant per the directive. No prose, no markdown."
        )
        raw = await caller(prompt)
        data = json.loads(_extract_json(raw))
        engage = bool(data.get("engage", False))
        conf = float(data.get("confidence", 0.0))
        return AttentionResult(engage and conf >= ATTENTION_THRESHOLD, conf)
    except Exception:
        logger.exception("attention: classify failed; failing closed (ignore)")
        return AttentionResult(False, 0.0)


def _extract_json(raw: str) -> str:
    """Pull the first {...} object out of a model reply (tolerates stray text)."""
    s = raw.find("{")
    e = raw.rfind("}")
    return raw[s : e + 1] if s != -1 and e != -1 and e > s else raw
