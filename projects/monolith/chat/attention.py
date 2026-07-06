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


@dataclass
class EngagementDecision:
    """Depth + target for an engaged message: does it need the goose agent, and
    if so which repo (if any) should it hydrate. ``repo`` is always either "" or
    one of the ACL-granted repos passed to classify_engagement, never free-form.
    """

    needs_agent: bool
    repo: str = ""


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


async def classify_engagement(
    message, allowed_repos: list[str] | None = None, *, _caller=None
) -> EngagementDecision:
    """Cheap depth + target classify for an engaged message: does it need the
    goose agent, and if so which granted repo (if any) should it hydrate?

    ``needs_agent`` is True for repo work, artifact/build requests, or thorough
    multi-source research; False for conversation, general knowledge, or a
    simple factual question (a basic web lookup is fine in chat).

    ``repo`` is inferred ONLY from ``allowed_repos`` (the ACL-granted scopes for
    this guild+user), and only when the task clearly targets that specific
    repository; otherwise it is "" (a repo-less run: artifact/build, generic
    investigation, or research). A value the model returns that is not in
    ``allowed_repos`` is coerced to "" -- the model never gets to name a repo
    the caller is not already granted.

    Fails closed to ``needs_agent=False, repo=""`` so a classify failure
    degrades to a fast in-monolith reply, never a surprise heavy or
    wrongly-targeted guest run. ``_caller`` is an injectable llm-caller for
    tests.
    """
    repos = [r for r in (allowed_repos or []) if r]
    try:
        caller = _caller
        if caller is None:
            from chat.summarizer import build_llm_caller

            caller = build_llm_caller()
        text = (message.content or "")[:500]
        repo_line = (
            (
                "If it needs the agent AND clearly targets one of these "
                'repositories, set "repo" to its exact name; otherwise set '
                '"repo" to an empty string. Repositories: ' + ", ".join(repos) + ". "
            )
            if repos
            else 'Always set "repo" to an empty string. '
        )
        prompt = (
            "You decide whether a chat message needs the heavyweight coding "
            "agent or can be answered directly. Answer needs_agent=true ONLY "
            "if it needs to read, analyze, or change a repository/codebase, "
            "build or generate an artifact/page, or do thorough multi-source "
            "research. Answer needs_agent=false for conversation, general "
            "knowledge, or a simple factual question (a basic web lookup is "
            "fine in chat). Summarizing this conversation, catching up on "
            "channel history, or extracting decisions or action items from it "
            "is also needs_agent=false: the chat agent already has tools for "
            "that. " + repo_line + "Reply with ONLY a JSON object: "
            '{"needs_agent": true|false, "repo": "<repo name or empty>"}. '
            "Message: " + text
        )
        raw = await caller(prompt)
        data = json.loads(_extract_json(raw))
        needs = bool(data.get("needs_agent", False))
        repo = data.get("repo", "") or ""
        if not isinstance(repo, str) or repo not in repos:
            repo = ""
        return EngagementDecision(needs_agent=needs, repo=repo)
    except Exception:
        logger.exception("attention: classify_engagement failed; failing closed (chat)")
        return EngagementDecision(needs_agent=False, repo="")


def _extract_json(raw: str) -> str:
    """Pull the first {...} object out of a model reply (tolerates stray text)."""
    s = raw.find("{")
    e = raw.rfind("}")
    return raw[s : e + 1] if s != -1 and e != -1 and e > s else raw
