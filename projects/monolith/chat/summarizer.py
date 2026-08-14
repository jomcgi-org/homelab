"""Rolling summary generator -- incrementally updates per-user-per-channel summaries."""

import asyncio
import logging
import os
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta, timezone

import httpx
from sqlmodel import Session, select

from chat.models import ChannelSummary, Message, UserChannelSummary
import shared.inference

logger = logging.getLogger(__name__)

# Output token budget. Must leave room for the prompt: the qwen3.6-27b alias has
# a 32768-token context, so reserving the whole window for output leaves 0 tokens
# for input and vLLM rejects every non-empty prompt with a 400. 8192 (matching the
# vision path) is far more than a summary/changelog needs and leaves ~24k for input.
_LLM_MAX_TOKENS = int(os.environ.get("LLM_MAX_TOKENS", "8192"))


async def generate_summaries(
    session: Session,
    llm_call: Callable[[str], Awaitable[str]],
) -> None:
    """Update rolling summaries for all (channel, user) pairs with new messages."""
    pairs = session.exec(
        select(Message.channel_id, Message.user_id, Message.username)
        .where(Message.is_bot == False)  # noqa: E712
        .group_by(Message.channel_id, Message.user_id, Message.username)
    ).all()

    for channel_id, user_id, username in pairs:
        try:
            existing = session.exec(
                select(UserChannelSummary).where(
                    UserChannelSummary.channel_id == channel_id,
                    UserChannelSummary.user_id == user_id,
                )
            ).first()

            high_water = existing.last_message_id if existing else 0

            new_messages = list(
                session.exec(
                    select(Message)
                    .where(
                        Message.channel_id == channel_id,
                        Message.user_id == user_id,
                        Message.is_bot == False,  # noqa: E712
                        Message.id > high_water,
                    )
                    .order_by(Message.created_at.asc())
                ).all()
            )

            if not new_messages:
                continue

            new_max_id = max(m.id for m in new_messages)
            messages_text = "\n".join(
                f"[{m.created_at.strftime('%Y-%m-%d %H:%M')}] {m.content}"
                for m in new_messages
            )

            if existing:
                prompt = (
                    f"Current summary of {username}'s messages:\n{existing.summary}\n\n"
                    f"New messages from {username}:\n{messages_text}\n\n"
                    "The bot already sees the most recent 20 messages as direct context. "
                    "Focus your summary on patterns, topics, and context from OLDER messages "
                    "that would help the bot understand this person better. "
                    "Keep it to 2-4 concise sentences."
                )
            else:
                prompt = (
                    f"Messages from {username}:\n{messages_text}\n\n"
                    "The bot already sees the most recent 20 messages as direct context. "
                    "Focus your summary on patterns, topics, and context from OLDER messages "
                    "that would help the bot understand this person better. "
                    "Write a 2-4 sentence summary of this user's key topics, interests, "
                    "and communication style."
                )

            summary_text = await llm_call(prompt)
            now = datetime.now(timezone.utc)

            if existing:
                existing.summary = summary_text
                existing.username = username
                existing.last_message_id = new_max_id
                existing.updated_at = now
                session.add(existing)
            else:
                session.add(
                    UserChannelSummary(
                        channel_id=channel_id,
                        user_id=user_id,
                        username=username,
                        summary=summary_text,
                        last_message_id=new_max_id,
                    )
                )
            await asyncio.to_thread(session.commit)
        except Exception:
            logger.exception(
                "Failed to generate summary for %s/%s", channel_id, username
            )
            continue

    logger.info("Summary generation complete for %d user-channel pairs", len(pairs))


async def generate_channel_summaries(
    session: Session,
    llm_call: Callable[[str], Awaitable[str]],
) -> None:
    """Update rolling summaries for all channels with new messages."""
    channels = session.exec(
        select(Message.channel_id).group_by(Message.channel_id)
    ).all()

    for (channel_id,) in [(c,) if isinstance(c, str) else c for c in channels]:
        try:
            existing = session.exec(
                select(ChannelSummary).where(
                    ChannelSummary.channel_id == channel_id,
                )
            ).first()

            high_water = existing.last_message_id if existing else 0

            new_messages = list(
                session.exec(
                    select(Message)
                    .where(
                        Message.channel_id == channel_id,
                        Message.id > high_water,
                    )
                    .order_by(Message.created_at.asc())
                ).all()
            )

            if not new_messages:
                continue

            new_max_id = max(m.id for m in new_messages)
            total_count = (existing.message_count if existing else 0) + len(
                new_messages
            )
            messages_text = "\n".join(
                f"[{m.created_at.strftime('%Y-%m-%d %H:%M')}] {m.username}: {m.content}"
                for m in new_messages
            )

            if existing:
                prompt = (
                    f"Current channel summary:\n{existing.summary}\n\n"
                    f"New messages:\n{messages_text}\n\n"
                    "The bot already sees the most recent 20 messages as direct context. "
                    "Focus your summary on the channel's overall topics, culture, and "
                    "recurring themes from OLDER messages. "
                    "Keep it to 2-4 concise sentences."
                )
            else:
                prompt = (
                    f"Messages from a Discord channel:\n{messages_text}\n\n"
                    "The bot already sees the most recent 20 messages as direct context. "
                    "Focus your summary on the channel's overall topics, culture, and "
                    "recurring themes from OLDER messages. "
                    "Write a 2-4 sentence summary of what this channel is about."
                )

            summary_text = await llm_call(prompt)

            now = datetime.now(timezone.utc)
            if existing:
                existing.summary = summary_text
                existing.last_message_id = new_max_id
                existing.message_count = total_count
                existing.updated_at = now
                session.add(existing)
            else:
                session.add(
                    ChannelSummary(
                        channel_id=channel_id,
                        summary=summary_text,
                        last_message_id=new_max_id,
                        message_count=total_count,
                    )
                )
            await asyncio.to_thread(session.commit)
        except Exception:
            logger.exception("Failed to generate channel summary for %s", channel_id)
            continue

    logger.info("Channel summary generation complete for %d channels", len(channels))


async def run_summary_generation(
    session: "Session",
    llm_call: Callable[[str], Awaitable[str]] | None = None,
) -> None:
    """Generate per-user and per-channel chat summaries.

    Module-level entrypoint so the one-shot jobs image (jobs_main
    chat-summary-generation) can run it without the scheduler closure. Builds
    its own LLM caller (Qwen via LLAMA_CPP_URL) when one is not injected."""
    if llm_call is None:
        llm_call = build_llm_caller()
    await generate_summaries(session, llm_call)
    await generate_channel_summaries(session, llm_call)


def on_startup(
    session: "Session",
    *,
    bot: "discord.Client | None" = None,
    llm_call: Callable[[str], Awaitable[str]] | None = None,
) -> None:
    """Register chat jobs with the scheduler."""
    from scheduler.api import register_job

    if llm_call is None:
        llm_call = build_llm_caller()

    async def _summary_handler(session: "Session") -> None:
        await run_summary_generation(session, llm_call)
        return None

    register_job(
        session,
        name="chat.summary_generation",
        interval_secs=86400,
        handler=_summary_handler,
        ttl_secs=1800,
    )

    if bot is not None:
        from chat.changelog import (
            ChangelogConfig,
            load_changelog_configs,
            run_changelog_iteration,
        )
        from shared.embedding import EmbeddingClient

        changelog_configs = load_changelog_configs(
            os.environ.get("CHANGELOG_CONFIGS", "")
        )

        for cfg in changelog_configs:

            def _make_handler(config: ChangelogConfig):
                async def _changelog_handler(session: "Session") -> datetime | None:
                    from chat.store import MessageStore

                    embed_client = EmbeddingClient()

                    async def _store_message(
                        discord_message_id: str,
                        channel_id: str,
                        user_id: str,
                        username: str,
                        content: str,
                    ) -> None:
                        store = MessageStore(session=session, embed_client=embed_client)
                        await store.save_message(
                            discord_message_id=discord_message_id,
                            channel_id=channel_id,
                            user_id=user_id,
                            username=username,
                            content=content,
                            is_bot=True,
                        )

                    await run_changelog_iteration(
                        bot, llm_call, config, store_message=_store_message
                    )
                    # Align to next interval boundary
                    now = datetime.now(timezone.utc)
                    interval = timedelta(hours=config.interval_hours)
                    epoch = now.replace(hour=0, minute=0, second=0, microsecond=0)
                    elapsed = now - epoch
                    periods = int(elapsed / interval) + 1
                    return epoch + interval * periods

                return _changelog_handler

            register_job(
                session,
                name=f"chat.changelog.{cfg.name}",
                interval_secs=cfg.interval_hours * 3600,
                handler=_make_handler(cfg),
                ttl_secs=1200,
            )


def _agent_reply_context(
    session: Session,
    channel_id: str,
    *,
    recent_limit: int = 15,
    max_users: int = 8,
) -> str:
    """Assemble a compact, channel-scoped context blurb for the agent concierge
    reply.

    Pulls the channel's rolling summary, the rolling per-user summaries of the
    people around, and the last few messages. Every query filters on
    ``channel_id``, so nothing authored outside this channel can enter the
    context: the scope is provenance (where content was written), matching what
    the requesting user can already read here. Returns "" when the channel has no
    stored context yet (the caller then falls back to the deterministic summary).
    """
    parts: list[str] = []

    channel_summary = session.exec(
        select(ChannelSummary).where(ChannelSummary.channel_id == channel_id)
    ).first()
    if channel_summary and channel_summary.summary.strip():
        parts.append(f"About this channel: {channel_summary.summary.strip()}")

    user_summaries = session.exec(
        select(UserChannelSummary)
        .where(UserChannelSummary.channel_id == channel_id)
        .order_by(UserChannelSummary.updated_at.desc())
        .limit(max_users)
    ).all()
    people = "\n".join(
        f"- {u.username}: {u.summary.strip()}"
        for u in user_summaries
        if u.summary.strip()
    )
    if people:
        parts.append("People here:\n" + people)

    recent = list(
        session.exec(
            select(Message)
            .where(Message.channel_id == channel_id)
            .order_by(Message.created_at.desc())
            .limit(recent_limit)
        ).all()
    )
    recent.reverse()  # oldest-first, chronological
    convo = "\n".join(
        f"{m.username}: {m.content.strip()}"
        for m in recent
        if m.content and m.content.strip()
    )
    if convo:
        parts.append("Recent conversation:\n" + convo)

    return "\n\n".join(parts)


def _fetch_agent_reply_context(channel_id: str) -> str:
    """Open a fresh session and build the concierge context (sync; via to_thread)."""
    from core.db import get_engine

    with Session(get_engine()) as session:
        return _agent_reply_context(session, channel_id)


def _build_agent_reply_prompt(summary: str, details: str, context: str) -> str:
    """Prompt the concierge model to deliver an agent run's typed result as
    Bosun's own first-person reply. From the member's side this is one
    conversation with Bosun, so the model speaks as itself and never narrates a
    separate "agent". The URL is NOT part of this prompt: it is appended
    deterministically by the caller so the model can never invent or mangle it."""
    reported = f"Summary: {summary}"
    if details:
        reported += f"\nDetails: {details}"
    ctx = context.strip() or "(no channel context available)"
    return (
        "You are Bosun, the assistant in this Discord channel. You just finished "
        "the work a member asked you for; the result is below. Deliver it as "
        'yourself, in the first person ("I looked at", "I found", "I\'d '
        'suggest"), speaking to the member as "you". From their side this is '
        'one continuous conversation with you, so never mention "the agent", '
        '"the coding agent", or "they" as if something else did the work, and '
        "never write about it in the third person. Do not open with or address "
        "the member by name. Be natural, warm, and specific. Give the complete "
        "answer with nothing padded: a quick task is a sentence or two, a review "
        "or explanation runs as long as it needs to land every real point, with "
        "no filler. Warmth is in how you say it, never in inflating the verdict: "
        "if the result is critical or carries caveats, deliver that criticism at "
        "full strength and never soften it into reassurance, and never add praise "
        "or a rosy summary the result does not contain. "
        "Do not invent links, PR numbers, file names, or any detail "
        "that is not in the result below (any link is posted separately). No "
        "markdown headers or bullet lists, no preamble.\n\n"
        f"What you did and found:\n{reported}\n\n"
        "Channel context, for tone and who is around (do not quote it back or "
        "address anyone by name):\n"
        f"{ctx}"
    )


async def conversational_agent_reply(
    channel_id: str,
    summary: str,
    details: str = "",
    *,
    llm_call: Callable[[str], Awaitable[str]] | None = None,
) -> str:
    """Rephrase an agent run's typed summary as a conversational channel reply,
    grounded in channel-scoped context.

    Reuses the shared inference seam (``build_llm_caller`` -> Qwen). Raises on a
    model failure so the caller can fall back to the deterministic summary; the
    caller (the goose runner's ``_delivery_message``) is fail-open around this.
    """
    context = await asyncio.to_thread(_fetch_agent_reply_context, channel_id)
    if llm_call is None:
        llm_call = build_llm_caller()
    prompt = _build_agent_reply_prompt(summary, details, context)
    return await llm_call(prompt)


def _build_chat_reply_prompt(question: str, guidance: str, context: str) -> str:
    """Prompt the concierge model to answer a member's message conversationally.

    This is the ADR 036 chat-route counterpart to ``_build_agent_reply_prompt``:
    no agent ran, so the framing is "answer this question", not "relay what the
    agent did". ``guidance`` is the orchestrator's optional retrieved context and
    suggested direction (may be empty); the member's ``question`` stays ground
    truth and is never overridden by the guidance."""
    ctx = context.strip() or "(no channel context available)"
    parts = [
        "You are Bosun, the assistant in this Discord channel. A member just "
        "messaged you. Answer them directly as yourself, in the first person, "
        'speaking to them as "you". Do not open with or address them by name. '
        "Be natural, warm, and specific. Give the complete answer with nothing "
        "padded: a quick question is a sentence or two, a broader one runs as "
        "long as it needs, with no filler. Be honest over agreeable: if their "
        "idea or work has real problems, lead with them instead of validating it "
        "to be nice, and skip empty openers like 'great question' or 'solid'. "
        "No run happened here, so do not "
        "narrate one or claim you did any work. Do not invent links, PR numbers, "
        "or file names. No markdown headers or bullet lists, no preamble.",
        f"What the member said:\n{question.strip()}",
    ]
    if guidance.strip():
        parts.append(
            "Background to help you answer (retrieved context and an optional "
            "suggested direction; use it to inform your reply, do not quote it "
            "verbatim, and only offer to escalate or take on work if it clearly "
            "fits, without claiming to have started anything):\n"
            f"{guidance.strip()}"
        )
    parts.append(
        f"Channel context, for tone and who is around (do not quote it back):\n{ctx}"
    )
    return "\n\n".join(parts)


async def conversational_chat_reply(
    channel_id: str,
    question: str,
    guidance: str = "",
    *,
    llm_call: Callable[[str], Awaitable[str]] | None = None,
) -> str:
    """Author a conversational reply to a member's message (ADR 036 chat route),
    grounded in channel-scoped context and the orchestrator's reply guidance.

    Unlike ``conversational_agent_reply`` this does not assume an agent ran: the
    prompt answers the question directly. Reuses the shared inference seam
    (``build_llm_caller`` -> Qwen) and raises on model failure so the caller can
    fail open.
    """
    context = await asyncio.to_thread(_fetch_agent_reply_context, channel_id)
    if llm_call is None:
        llm_call = build_llm_caller()
    prompt = _build_chat_reply_prompt(question, guidance, context)
    return await llm_call(prompt)


_RETRYABLE_STATUS_CODES = {502, 503, 504}


def build_llm_caller(base_url: str | None = None) -> Callable[[str], Awaitable[str]]:
    """Create an async callable that sends a prompt to Qwen via llama.cpp."""
    url = base_url or os.environ.get("LLAMA_CPP_URL", "")
    client = httpx.AsyncClient(timeout=httpx.Timeout(600.0))

    async def call_llm(prompt: str, *, max_retries: int = 3) -> str:
        last_exc: Exception | None = None
        for attempt in range(max_retries + 1):
            try:
                resp = await client.post(
                    f"{url}/v1/chat/completions",
                    json={
                        "model": "qwen3.6-27b",
                        "messages": [{"role": "user", "content": prompt}],
                        "max_tokens": _LLM_MAX_TOKENS,
                        # Disable thinking so the budget is spent on the summary,
                        # not <think> reasoning -- a thinking response puts the
                        # reasoning in reasoning_content and returns content:null
                        # behind a 200, which would slip past raise_for_status.
                        **shared.inference.thinking_off(),
                    },
                )
                resp.raise_for_status()
                try:
                    content = resp.json()["choices"][0]["message"]["content"]
                except (KeyError, IndexError, ValueError) as e:
                    raise RuntimeError(f"unexpected LLM response shape: {e}") from e
                if not content:
                    raise RuntimeError(
                        "LLM returned empty content (thinking may have consumed "
                        "the token budget)"
                    )
                return content
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code not in _RETRYABLE_STATUS_CODES:
                    raise
                last_exc = exc
            except httpx.ConnectError as exc:
                last_exc = exc

            if attempt < max_retries:
                delay = 2**attempt  # 1s, 2s, 4s
                logger.warning(
                    "LLM call failed (attempt %d/%d): %s — retrying in %ds",
                    attempt + 1,
                    max_retries + 1,
                    last_exc,
                    delay,
                )
                await asyncio.sleep(delay)

        raise last_exc  # type: ignore[misc]

    return call_llm


# Household (ADR 039, amended): all generated CONTENT for the WhatsApp channel is
# authored by a strong-but-cheap hosted model (DeepSeek V4 Flash on OpenRouter),
# while the in-cluster Qwen is reserved for the activation classifier. The model
# is pinned (never :auto) so replies stay attributable; the default here is the
# floor, and HOUSEHOLD_LLM_MODEL (set from Helm values) is the source of truth in
# a real deployment. This runs IN the monolith, which already holds
# OPENROUTER_API_KEY for the orchestrator, so it reaches openrouter.ai directly
# (no egress swap; that is only for the sandboxed goose guest).
_HOUSEHOLD_MODEL_DEFAULT = "deepseek/deepseek-v4-flash"
_OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"


def household_model() -> str:
    """The pinned OpenRouter model id for household content generation."""
    return os.environ.get("HOUSEHOLD_LLM_MODEL", "").strip() or _HOUSEHOLD_MODEL_DEFAULT


def build_openrouter_caller(
    model: str | None = None, base_url: str | None = None
) -> Callable[[str], Awaitable[str]]:
    """Async caller that sends a prompt to an OpenRouter-hosted model.

    Mirrors ``build_llm_caller`` (same retry contract, same ``str -> str`` shape)
    but targets OpenRouter with ``OPENROUTER_API_KEY`` and a pinned model. Used
    for household-tier content so the WhatsApp channel answers on DeepSeek V4
    Flash while Discord stays on in-cluster Qwen.
    """
    url = (base_url or _OPENROUTER_BASE_URL).rstrip("/")
    model_id = model or household_model()
    api_key = os.environ.get("OPENROUTER_API_KEY", "")
    client = httpx.AsyncClient(timeout=httpx.Timeout(600.0))
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}

    async def call_llm(prompt: str, *, max_retries: int = 3) -> str:
        last_exc: Exception | None = None
        for attempt in range(max_retries + 1):
            try:
                resp = await client.post(
                    f"{url}/chat/completions",
                    headers=headers,
                    json={
                        "model": model_id,
                        "messages": [{"role": "user", "content": prompt}],
                        "max_tokens": _LLM_MAX_TOKENS,
                    },
                )
                resp.raise_for_status()
                try:
                    content = resp.json()["choices"][0]["message"]["content"]
                except (KeyError, IndexError, ValueError) as e:
                    raise RuntimeError(f"unexpected LLM response shape: {e}") from e
                if not content:
                    raise RuntimeError("OpenRouter returned empty content")
                return content
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code not in _RETRYABLE_STATUS_CODES:
                    raise
                last_exc = exc
            except httpx.ConnectError as exc:
                last_exc = exc

            if attempt < max_retries:
                delay = 2**attempt  # 1s, 2s, 4s
                logger.warning(
                    "OpenRouter call failed (attempt %d/%d): %s — retrying in %ds",
                    attempt + 1,
                    max_retries + 1,
                    last_exc,
                    delay,
                )
                await asyncio.sleep(delay)

        raise last_exc  # type: ignore[misc]

    return call_llm


def concierge_caller_for_tier(
    tier: str,
) -> Callable[[str], Awaitable[str]] | None:
    """The content-generation caller for a tier, or None to use the default.

    The household tier (WhatsApp) authors content on DeepSeek V4 Flash; every
    other tier returns None so the caller keeps its existing default (in-cluster
    Qwen via ``build_llm_caller``). Returning None rather than the Qwen caller
    keeps the Discord path byte-identical (it never builds a caller it would not
    have built before).
    """
    if tier == "household":
        return build_openrouter_caller()
    return None
