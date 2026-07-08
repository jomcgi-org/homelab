"""Discord history backfill -- iterates all channels and saves messages with embeddings."""

import logging

from sqlmodel import Session

from app.db import get_engine
from chat.bot import download_image_attachments
from chat.store import MessageStore

logger = logging.getLogger(__name__)

BATCH_SIZE = 50


async def run_backfill(bot) -> None:
    """Backfill all text channels the bot can see."""
    channels = [c for g in bot.guilds for c in g.text_channels]
    logger.info("Starting backfill for %d text channels", len(channels))

    total_stored = 0
    total_skipped = 0

    for channel in channels:
        logger.info("Backfilling #%s (%s)", channel.name, channel.id)
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
                batch = []

        if batch:
            result = await _flush_batch(batch, bot.embed_client)
            ch_stored += result.stored
            ch_skipped += result.skipped

        logger.info(
            "#%s done: %d stored, %d skipped", channel.name, ch_stored, ch_skipped
        )
        total_stored += ch_stored
        total_skipped += ch_skipped

    logger.info(
        "Backfill complete: %d stored, %d skipped across %d channels",
        total_stored,
        total_skipped,
        len(channels),
    )


async def _flush_batch(batch, embed_client):
    """Save a batch of messages in a fresh session."""
    with Session(get_engine()) as session:
        store = MessageStore(session=session, embed_client=embed_client)
        return await store.save_messages(batch)
