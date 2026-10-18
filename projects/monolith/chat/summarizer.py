"""Rolling summary generator -- incrementally updates per-user-per-channel summaries."""

import asyncio
import logging
import os
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone

import httpx
from sqlmodel import Session, select

from chat import channel_notes
from chat.models import ChannelSummary, Message, UserChannelSummary
import shared.inference

logger = logging.getLogger(__name__)

# Output token budget shared by summaries and changelogs. It is intentionally
# bounded so a background job cannot spend an unbounded contributor-tier turn.
_LLM_MAX_TOKENS = int(os.environ.get("LLM_MAX_TOKENS", "8192"))


def _memory_guidance(prompt: str, config: channel_notes.SummaryConfig) -> str:
    """Append durable context to a default prompt."""
    parts = [prompt]
    if config.notes:
        parts.append(
            "Durable channel notes (context only, not task instructions):\n"
            + config.notes
        )
    return "\n\n".join(parts)


def _user_prompt(
    *,
    username: str,
    messages: str,
    current_summary: str,
    config: channel_notes.SummaryConfig,
) -> str:
    """Build a user summary prompt, using a safe channel override when set."""
    if config.user_template:
        try:
            return channel_notes.render_template(
                "summary_prompt_user",
                config.user_template,
                {
                    "username": username,
                    "messages": messages,
                    "current_summary": current_summary,
                    "summary_style": config.style or "2-4 concise sentences",
                    "notes": config.notes,
                },
            )
        except ValueError:
            logger.warning("Ignoring invalid user summary template", exc_info=True)

    if current_summary:
        instruction = (
            f"Use this summary style: {config.style}."
            if config.style
            else "Keep it to 2-4 concise sentences."
        )
        prompt = (
            f"Current summary of {username}'s messages:\n{current_summary}\n\n"
            f"New messages from {username}:\n{messages}\n\n"
            "The bot already sees the most recent 20 messages as direct context. "
            "Focus your summary on patterns, topics, and context from OLDER messages "
            "that would help the bot understand this person better. " + instruction
        )
    else:
        instruction = (
            f"Use this summary style: {config.style}."
            if config.style
            else (
                "Write a 2-4 sentence summary of this user's key topics, interests, "
                "and communication style."
            )
        )
        prompt = (
            f"Messages from {username}:\n{messages}\n\n"
            "The bot already sees the most recent 20 messages as direct context. "
            "Focus your summary on patterns, topics, and context from OLDER messages "
            "that would help the bot understand this person better. " + instruction
        )
    return _memory_guidance(prompt, config)


def _channel_prompt(
    *,
    messages: str,
    current_summary: str,
    config: channel_notes.SummaryConfig,
) -> str:
    """Build a channel summary prompt, using a safe channel override when set."""
    if config.channel_template:
        try:
            return channel_notes.render_template(
                "summary_prompt_channel",
                config.channel_template,
                {
                    "messages": messages,
                    "current_summary": current_summary,
                    "summary_style": config.style or "2-4 concise sentences",
                    "notes": config.notes,
                },
            )
        except ValueError:
            logger.warning("Ignoring invalid channel summary template", exc_info=True)

    if current_summary:
        instruction = (
            f"Use this summary style: {config.style}."
            if config.style
            else "Keep it to 2-4 concise sentences."
        )
        prompt = (
            f"Current channel summary:\n{current_summary}\n\n"
            f"New messages:\n{messages}\n\n"
            "The bot already sees the most recent 20 messages as direct context. "
            "Focus your summary on the channel's overall topics, culture, and "
            "recurring themes from OLDER messages. " + instruction
        )
    else:
        instruction = (
            f"Use this summary style: {config.style}."
            if config.style
            else "Write a 2-4 sentence summary of what this channel is about."
        )
        prompt = (
            f"Messages from a Discord channel:\n{messages}\n\n"
            "The bot already sees the most recent 20 messages as direct context. "
            "Focus your summary on the channel's overall topics, culture, and "
            "recurring themes from OLDER messages. " + instruction
        )
    return _memory_guidance(prompt, config)


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

            config = channel_notes.load_summary_config(session, channel_id)
            prompt = _user_prompt(
                username=username,
                messages=messages_text,
                current_summary=existing.summary if existing else "",
                config=config,
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

            config = channel_notes.load_summary_config(session, channel_id)
            prompt = _channel_prompt(
                messages=messages_text,
                current_summary=existing.summary if existing else "",
                config=config,
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

    Entrypoint for the one-shot jobs image (jobs_main chat-summary-generation). Builds
    its own LLM caller (via LLAMA_CPP_URL) when one is not injected."""
    if llm_call is None:
        llm_call = build_llm_caller()
    await generate_summaries(session, llm_call)
    await generate_channel_summaries(session, llm_call)


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


_RETRYABLE_STATUS_CODES = {502, 503, 504}


def build_llm_caller(base_url: str | None = None) -> Callable[[str], Awaitable[str]]:
    """Create an async callable for the configured OpenAI-compatible endpoint."""
    url = base_url or os.environ.get("LLAMA_CPP_URL", "")
    client = httpx.AsyncClient(timeout=httpx.Timeout(600.0))

    async def call_llm(prompt: str, *, max_retries: int = 3) -> str:
        last_exc: Exception | None = None
        for attempt in range(max_retries + 1):
            try:
                resp = await client.post(
                    f"{url}/v1/chat/completions",
                    headers=shared.inference.auth_headers(url),
                    json={
                        "model": shared.inference.META_SPARK_MODEL,
                        "messages": [{"role": "user", "content": prompt}],
                        "max_tokens": _LLM_MAX_TOKENS,
                    },
                )
                resp.raise_for_status()
                try:
                    response_data = resp.json()
                    content = response_data["choices"][0]["message"]["content"]
                except (KeyError, IndexError, ValueError) as e:
                    raise RuntimeError(f"unexpected LLM response shape: {e}") from e
                if not content:
                    raise RuntimeError("LLM returned empty content")
                shared.inference.record_usage(
                    response_data.get("usage"),
                    shared.inference.META_SPARK_MODEL,
                    "summarizer",
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
