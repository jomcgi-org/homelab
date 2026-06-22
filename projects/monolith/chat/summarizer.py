"""Rolling summary generator -- incrementally updates per-user-per-channel summaries."""

import asyncio
import logging
import os
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta, timezone

import httpx
from sqlmodel import Session, select

from chat.models import ChannelSummary, Message, UserChannelSummary

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
                        "chat_template_kwargs": {"enable_thinking": False},
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
