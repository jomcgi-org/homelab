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
ATTENTION_THRESHOLD = float(os.environ.get("ATTENTION_THRESHOLD", "0.5"))
_RECENT_TAG_THRESHOLD = float(os.environ.get("ATTENTION_RECENT_TAG_THRESHOLD", "0.35"))


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
            "You are Bosun, a friendly, chatty bot hanging out in this Discord "
            "channel. Lean toward joining the conversation. Engage (true) if the "
            "message addresses you or the group, greets you, asks anything, is "
            "trying to get your attention, could use a reply, or would benefit "
            "from a web search or fact-check. Only ignore (false) if it is clearly "
            "aimed at another specific person (not you), is pure noise or a bare "
            "reaction with nothing to respond to, or is between other people and "
            "not for you. When unsure, engage.\n"
            + (
                "You were recently mentioned in this channel, so lean even harder "
                "toward engaging on the follow-up.\n"
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

    True only for repo work, a standalone interactive webpage/app, or thorough
    multi-source research; False for conversation, general knowledge, a simple
    factual question, OR anything the chat agent's own tools already cover -
    including charting/plotting data (run_python renders a chart that attaches
    to Discord) and computing this channel's activity stats (counts, rankings,
    per-user/per-day breakdowns). The bright line for a chart request: a Python
    image stays chat; only a click-around webpage is agent. Fails closed to
    False so a classify failure degrades to a fast in-monolith reply, never a
    surprise heavy guest run. ``_caller`` is an injectable llm-caller for tests.
    """
    try:
        caller = _caller
        if caller is None:
            from chat.summarizer import build_llm_caller

            caller = build_llm_caller()
        text = (message.content or "")[:500]
        prompt = (
            "You decide whether a chat message needs the heavyweight coding "
            "agent (a sandboxed microVM that reads/writes THIS "
            "repository/codebase and builds standalone interactive webpages) "
            "or can be answered right here in chat. Default to chat: the chat "
            "assistant already has strong tools, so prefer chat whenever they "
            "suffice. Chat tools include:\n"
            "- search and read this channel's message history (semantic + "
            "keyword)\n"
            "- counts, rankings, and per-user or per-day breakdowns of this "
            "channel's own activity (who posted most, messages per day)\n"
            "- run_python: run code to compute results AND draw "
            "charts/graphs/plots with matplotlib; the resulting image "
            "attaches directly here in the chat\n"
            "- render tables, and look things up on the web\n"
            "Summarizing this conversation, catching up on channel history, or "
            'extracting decisions or action items from it is also "chat", '
            'not "agent": the chat agent already has tools for that. Answer '
            '"agent" ONLY if the message truly needs one of: (a) reading, '
            "analyzing, or changing THIS repository/codebase; (b) a "
            "standalone, interactive webpage, dashboard, or app - an "
            "artifact/page you click around in, not a single image; or (c) "
            "thorough multi-source research. KEY RULE: if the ask is to chart, "
            "graph, plot, or visualize DATA (including this channel's own "
            "stats, e.g. 'messages per user per day'), that is \"chat\" - "
            "run_python makes the chart and it attaches here. Do NOT choose "
            '"agent" just because a chart or visualization was requested; '
            'choose "agent" for a chart only if the user explicitly asked for '
            "an interactive or standalone webpage rather than an image. Reply "
            "with ONLY a JSON object: "
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
