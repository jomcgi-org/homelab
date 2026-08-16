"""Attention gate (ADR 035 phase 3): should the bot engage with a message?

Mentions and replies to the bot always engage (no model call). In channels with
an ambient grant, a classify-only fast-model call scores the message against a
base engagement policy (refined, not gated, by the channel directive) and
engages only above ATTENTION_THRESHOLD. Recently-tagged threads/channels get a
lower threshold so the bot leans into relevant follow-ups. Everywhere else,
ignore. The classifier holds no tools and fails closed (ignore) on any error.

The pre-gate above only ever sees the trigger message, so it cannot catch a
reply that turns out to barge in, double down after a brush-off, or invent
facts. ``should_send`` is a second, disconnected post-generation gate
(improve-ambient): a fresh classify that reads the channel directive, the recent
interaction, and the DRAFTED reply, and vetoes an ambient send that a real
person would not have wanted. It fails OPEN (send) so a classify blip degrades
to today's behaviour rather than silently eating replies, and is scoped to the
ambient chat path only (a live reply someone is waiting on is never gated, and
heavy agent runs are gated pre-generation, not after the microVM has run).
"""

import json
import logging
import os
from dataclasses import dataclass

logger = logging.getLogger(__name__)
ATTENTION_THRESHOLD = float(os.environ.get("ATTENTION_THRESHOLD", "0.5"))
_RECENT_TAG_THRESHOLD = float(os.environ.get("ATTENTION_RECENT_TAG_THRESHOLD", "0.35"))
# The post-generation send-gate is on by default; set AMBIENT_SEND_GATE=0 to
# disable it (and skip its extra classify call) without a redeploy.
SEND_GATE_ENABLED = os.environ.get("AMBIENT_SEND_GATE", "1") != "0"


@dataclass
class AttentionResult:
    engage: bool
    confidence: float
    # True when this engage is an explicit summons (a hard @mention, a reply to
    # Bosun, or a caller-supplied ``directed`` signal), as opposed to a soft
    # ambient interjection the classifier chose to join. The caller uses this to
    # keep an explicit summons on the live (never-suppressed) reply path even in
    # an ambient channel: someone who @-mentioned Bosun is waiting on an answer,
    # so the no_reply tool and the post-generation send-gate must not eat it.
    explicit: bool = False


async def evaluate(
    message,
    directive: str,
    bot_user,
    is_ambient: bool,
    *,
    recently_tagged: bool = False,
    directed: bool = False,
    _caller=None,
) -> AttentionResult:
    """Decide whether to engage. See module docstring.

    ``directive`` is the channel directive text; it refines the base
    engagement policy rather than gating it. ``recently_tagged`` means the bot
    was @mentioned in this channel/thread within a short recent window, which
    lowers the effective engage threshold. ``directed`` is a channel-agnostic
    "this message is aimed at the bot" signal that a non-Discord caller (e.g.
    the WhatsApp gateway: a reply to a bot message or a trigger-name match)
    computes itself and passes in to engage without the Discord mention check.
    Discord callers never pass ``directed``, so their path is unchanged.
    ``_caller`` is an injectable llm-caller for tests; defaults to
    ``build_llm_caller()``.
    """
    # A caller-supplied directedness signal engages immediately, before (and
    # instead of) the Discord-specific mention/reply check. This is the seam
    # non-Discord channels use; it keeps should_respond off the hot path when
    # bot_user is None (WhatsApp has no Discord user object).
    if directed:
        return AttentionResult(True, 1.0, explicit=True)

    from chat.bot import should_respond  # mention/reply detection (lazy to avoid cycle)

    # bot_user is None only for non-Discord channels, which never reach here
    # engaged (they set directed instead); guard the Discord-only check so a
    # WhatsApp adapter never hits should_respond's <@id> mention parsing.
    if bot_user is not None and should_respond(message, bot_user):
        return AttentionResult(True, 1.0, explicit=True)
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
            "You are Bosun, a friendly bot hanging out in this Discord channel. "
            "You are one voice among friends, not a reply guy: join in when there "
            "is a real opening, and otherwise let people talk. Engage (true) if "
            "the message addresses you by name, greets you, asks YOU a question, "
            "is trying to get your attention, or clearly invites your take, or if "
            "it states something checkable, posed as an open question to the room, "
            "that would genuinely benefit from a web search or fact-check (but not "
            "when people are simply discussing their own work, plans, or lives "
            "among themselves). Ignore (false) if it is aimed at another specific "
            "person, or at the other people in the channel rather than you - "
            "including a message that names or @mentions specific other people "
            "(not you) and asks THEM something ('@Brian @Sam drop me your mc "
            "names'), friends sorting out plans among themselves ('you lot around "
            "for a game?', 'wanna play tonight?'), catching up with each other "
            "('how have you been?'), or friends supporting each other through "
            "something personal such as illness, grief, a death, mental health, "
            "money worries, or a relationship - a check-in on how someone or their "
            "family is coping ('how's she dealing with things?', 'how much did you "
            "drop your hours by?') is people looking after each other, not an "
            "opening for you, even when it is phrased as a question. Also ignore a "
            "message that is pure noise or a bare reaction, is "
            "a link, image, or media share posted without a question or a request "
            "for your take, is people thinking out loud to each other, or tells "
            "you to stop, says you talk too much, or otherwise signals you are "
            "not wanted right now. A message being phrased as a question is NOT on "
            "its own a reason to engage when it is plainly directed at the other "
            "people here rather than at you. When a message is not addressed to "
            "you and you are unsure whether it actually wants a reply, stay "
            "quiet.\n"
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


async def should_send(
    directive: str,
    conversation: str,
    trigger: str,
    reply: str,
    *,
    _caller=None,
) -> bool:
    """Post-generation send-gate for an ambient chat reply. See module docstring.

    A second, disconnected classify: given the channel ``directive``, the recent
    ``conversation``, the ``trigger`` message being replied to, and Bosun's
    ``reply`` as already drafted, decide whether sending it improves the channel
    or Bosun should stay silent. This is an independent critic reading the
    finished artifact, not the drafter's own second thought, so it catches
    reply-quality misfires the pre-gate (which only sees the trigger) cannot:
    barging into a conversation nobody asked Bosun to join, piling on after a
    brush-off, or inventing facts/names/links.

    Fails OPEN (returns True) on any error or when disabled, so a classify blip
    degrades to today's behaviour rather than silently swallowing a reply.
    ``_caller`` is an injectable llm-caller for tests.
    """
    if not SEND_GATE_ENABLED:
        return True
    try:
        caller = _caller
        if caller is None:
            from chat.summarizer import build_llm_caller

            caller = build_llm_caller()
        prompt = (
            "You are the send-gate for Bosun, a bot in a Discord channel among "
            "friends. Bosun has DRAFTED a reply to the latest message. Your only "
            "job is to decide whether sending it right now improves the "
            "conversation, or whether Bosun should stay silent. Be willing to "
            "veto. Answer send=false if the reply: barges into a conversation the "
            "humans are having with each other and did not ask Bosun to join; "
            "inserts Bosun into a personal or emotional exchange between people "
            "(someone's illness, grief, a death, mental health, money worries, or "
            "a relationship) that Bosun was not asked to join, even when the "
            "drafted reply is kind, supportive, or on topic; "
            "piles on after someone signalled they don't want it (a brush-off, "
            "'stop', 'you talk too much', hostility); states specific facts, "
            "events, names, links, or numbers Bosun cannot actually know or that "
            "read as invented; is off Bosun's voice, padded, or long for what was "
            "asked; or adds nothing a person would miss. Answer send=true only if "
            "a real person in this channel would be glad Bosun sent it. When "
            "unsure, prefer send=false.\n"
            "EXCEPTION, explicit invitation: if the latest message directly "
            "addresses Bosun by name or @mention with a request or question, the "
            "human has asked Bosun to reply, so the barge-in, brush-off-context, "
            "and when-unsure vetoes do NOT apply. Answer send=true unless the "
            "drafted reply is harmful or hateful, or that same person told Bosun "
            "to stop earlier in this exchange. A long computed result the person "
            "explicitly asked for (requested digits, data, a table, a list) is "
            "wanted content, not 'invented numbers', and must not be vetoed on "
            "that ground.\n"
            + (
                "Channel guidance (weigh this heavily): " + directive + "\n"
                if directive
                else ""
            )
            + "Recent conversation:\n"
            + (conversation or "")[:2000]
            + "\nLatest message Bosun is replying to: "
            + (trigger or "")[:500]
            + "\nBosun's drafted reply: "
            + (reply or "")[:1000]
            + '\nReply with ONLY a JSON object: {"send": true|false}. '
            "No prose, no markdown."
        )
        raw = await caller(prompt)
        data = json.loads(_extract_json(raw))
        return bool(data.get("send", True))
    except Exception:
        logger.exception("attention: send-gate failed; failing open (send)")
        return True


async def needs_agent(message, *, _caller=None) -> bool:
    """Cheap depth classify: does this engaged message need the goose agent?

    True only for repo work, a standalone interactive webpage/app, or thorough
    multi-source research; False for conversation, general knowledge, a simple
    factual question, OR anything the chat agent's own tools already cover -
    including charting/plotting data (run_code renders a chart that attaches
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
            "- run_code: run code (python by default) to compute results AND "
            "draw "
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
            "run_code makes the chart and it attaches here. Do NOT choose "
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
