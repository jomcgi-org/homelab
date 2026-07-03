"""Attention gate (ADR 035 phase 3): should the bot engage with a message?

Mentions and replies to the bot always engage (no model call). In channels with
an ambient grant, a classify-only fast-model call scores the message against a
base engagement policy (refined, not gated, by the channel directive) and
engages only above ATTENTION_THRESHOLD. Recently-tagged threads/channels get a
lower threshold so the bot leans into relevant follow-ups. Everywhere else,
ignore. The classifier holds no tools and fails closed (ignore) on any error.
"""

import json
import logging
import os
from dataclasses import dataclass

logger = logging.getLogger(__name__)
ATTENTION_THRESHOLD = float(os.environ.get("ATTENTION_THRESHOLD", "0.8"))
_RECENT_TAG_THRESHOLD = float(os.environ.get("ATTENTION_RECENT_TAG_THRESHOLD", "0.5"))


@dataclass
class AttentionResult:
    engage: bool
    confidence: float


async def evaluate(
    message,
    directive: str,
    bot_user,
    is_ambient: bool,
    *,
    recently_tagged: bool = False,
    _caller=None,
) -> AttentionResult:
    """Decide whether to engage. See module docstring.

    ``directive`` is the channel directive text; it refines the base
    engagement policy rather than gating it. ``recently_tagged`` means the bot
    was @mentioned in this channel/thread within a short recent window, which
    lowers the effective engage threshold. ``_caller`` is an injectable
    llm-caller for tests; defaults to ``build_llm_caller()``.
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
            "You decide whether the assistant (a Discord bot named Bosun) should "
            "jump into this message unprompted. Engage (true) when the message is "
            "an open question or request that EITHER addresses the bot or the group "
            "and wants a response, OR is about a code repository or codebase, OR "
            "would benefit from a web search or fact-check (a factual claim, a "
            "current event, anything checkable). Lean ignore (false) for idle "
            "chatter, statements not seeking help, or messages clearly between "
            "other people.\n"
            + (
                "The bot was recently mentioned in this channel, so lean toward "
                "engaging on a relevant follow-up.\n"
                if recently_tagged
                else ""
            )
            + (
                "Channel guidance (adjust the above if it applies): " + directive + "\n"
                if directive
                else ""
            )
            + "Message: "
            + text
            + '\nReply with ONLY a JSON object: {"engage": true|false, '
            '"confidence": 0.0-1.0}. No prose, no markdown.'
        )
        raw = await caller(prompt)
        data = json.loads(_extract_json(raw))
        engage = bool(data.get("engage", False))
        conf = float(data.get("confidence", 0.0))
        threshold = _RECENT_TAG_THRESHOLD if recently_tagged else ATTENTION_THRESHOLD
        return AttentionResult(engage and conf >= threshold, conf)
    except Exception:
        logger.exception("attention: classify failed; failing closed (ignore)")
        return AttentionResult(False, 0.0)


async def needs_agent(message, *, _caller=None) -> bool:
    """Cheap depth classify: does this engaged message need the goose agent?

    True for repo work, artifact/build requests, or thorough multi-source
    research; False for conversation, general knowledge, or a simple factual
    question (a basic web lookup is fine in chat). Fails closed to False so a
    classify failure degrades to a fast in-monolith reply, never a surprise
    heavy guest run. ``_caller`` is an injectable llm-caller for tests.
    """
    try:
        caller = _caller
        if caller is None:
            from chat.summarizer import build_llm_caller

            caller = build_llm_caller()
        text = (message.content or "")[:500]
        prompt = (
            "You decide whether a chat message needs the heavyweight coding "
            'agent or can be answered directly. Answer "agent" ONLY if it '
            "needs to read, analyze, or change THIS repository/codebase, "
            "build or generate an artifact/page, or do thorough multi-source "
            'research. Answer "chat" for conversation, general knowledge, '
            "or a simple factual question (a basic web lookup is fine in "
            "chat). Reply with ONLY a JSON object: "
            '{"needs_agent": true|false}. Message: ' + text
        )
        raw = await caller(prompt)
        data = json.loads(_extract_json(raw))
        return bool(data.get("needs_agent", False))
    except Exception:
        logger.exception(
            "attention: needs_agent classify failed; failing closed (chat)"
        )
        return False


def _extract_json(raw: str) -> str:
    """Pull the first {...} object out of a model reply (tolerates stray text)."""
    s = raw.find("{")
    e = raw.rfind("}")
    return raw[s : e + 1] if s != -1 and e != -1 and e > s else raw
