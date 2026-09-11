"""Discord history backfill -- iterates all channels and saves messages with embeddings."""

import logging
from collections.abc import Callable, Collection
from dataclasses import dataclass

from sqlmodel import Session

from core.db import get_engine
from chat.bot import download_image_attachments
from chat.store import MessageStore

logger = logging.getLogger(__name__)

BATCH_SIZE = 50


@dataclass(frozen=True)
class BackfillProgress:
    """A point-in-time snapshot of backfill progress."""

    channels_total: int
    channels_completed: int = 0
    messages_stored: int = 0
    messages_skipped: int = 0
    current_channel_id: str | None = None


async def run_backfill(
    bot,
    channel_ids: Collection[str] | None = None,
    on_progress: Callable[[BackfillProgress], None] | None = None,
) -> BackfillProgress:
    """Backfill visible text channels, optionally limited to ``channel_ids``."""
    channels = [c for g in bot.guilds for c in g.text_channels]
    if channel_ids is not None:
        requested = {str(channel_id) for channel_id in channel_ids}
        channels = [channel for channel in channels if str(channel.id) in requested]
    logger.info("Starting backfill for %d text channels", len(channels))

    total_stored = 0
    total_skipped = 0
    channels_completed = 0

    def report(current_channel_id: str | None = None) -> BackfillProgress:
        progress = BackfillProgress(
            channels_total=len(channels),
            channels_completed=channels_completed,
            messages_stored=total_stored,
            messages_skipped=total_skipped,
            current_channel_id=current_channel_id,
        )
        if on_progress is not None:
            on_progress(progress)
        return progress

    report()

    for channel in channels:
        logger.info("Backfilling #%s (%s)", channel.name, channel.id)
        report(str(channel.id))
        batch: list[dict] = []
        ch_stored = 0
        ch_skipped = 0

        async for message in channel.history(limit=None, oldest_first=True):
            attachments = await download_image_attachments(
                message.attachments, bot.vision_client, store=None
            )

            msg_dict = {
                "discord_message_id": str(message.id),
                "channel_id": str(channel.id),
                "user_id": str(message.author.id),
                "username": message.author.display_name,
                "content": message.content,
                "is_bot": message.author.bot,
            }
            if attachments:
                msg_dict["attachments"] = attachments

            batch.append(msg_dict)

            if len(batch) >= BATCH_SIZE:
                result = await _flush_batch(batch, bot.embed_client)
                ch_stored += result.stored
                ch_skipped += result.skipped
                total_stored += result.stored
                total_skipped += result.skipped
                batch = []
                report(str(channel.id))

        if batch:
            result = await _flush_batch(batch, bot.embed_client)
            ch_stored += result.stored
            ch_skipped += result.skipped
            total_stored += result.stored
            total_skipped += result.skipped
            report(str(channel.id))

        logger.info(
            "#%s done: %d stored, %d skipped", channel.name, ch_stored, ch_skipped
        )
        channels_completed += 1
        report()

    logger.info(
        "Backfill complete: %d stored, %d skipped across %d channels",
        total_stored,
        total_skipped,
        len(channels),
    )
    return BackfillProgress(
        channels_total=len(channels),
        channels_completed=channels_completed,
        messages_stored=total_stored,
        messages_skipped=total_skipped,
    )


async def _flush_batch(batch, embed_client):
    """Save a batch of messages in a fresh session."""
    with Session(get_engine()) as session:
        store = MessageStore(session=session, embed_client=embed_client)
        return await store.save_messages(batch)
