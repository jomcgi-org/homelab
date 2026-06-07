"""Discord bot -- gateway listener and message handler."""

import asyncio
import hashlib
import json
import logging
import os

import discord
from pydantic_ai import (
    AgentRunResultEvent,
    BinaryContent,
    FunctionToolCallEvent,
    PartDeltaEvent,
    TextPartDelta,
    ThinkingPartDelta,
)

from chat.agent import create_agent, format_context_messages
from shared.embedding import EmbeddingClient
from chat.store import MessageStore
from chat.vision import VisionClient
from chat.web_search import search_web
from app.db import get_engine

from sqlmodel import Session

logger = logging.getLogger(__name__)

DISCORD_BOT_TOKEN = os.environ.get("DISCORD_BOT_TOKEN", "")
DISCORD_MESSAGE_LIMIT = 2000
THINKING_TRUNCATE_AT = 1985
LLM_MAX_RETRIES = 3
LLM_RETRY_BASE_DELAY = 1.0  # seconds
STREAM_EDIT_INTERVAL = 1.0


def _truncate_thinking(thinking: str) -> str:
    """Truncate thinking text if it exceeds Discord's message limit."""
    if len(thinking) <= DISCORD_MESSAGE_LIMIT:
        return thinking
    return thinking[:THINKING_TRUNCATE_AT] + "... (truncated)"


class ThinkingView(discord.ui.View):
    """Discord View with a 'Show thinking' button that reveals model reasoning."""

    def __init__(self, thinking_text: str):
        super().__init__(timeout=None)
        self.thinking_text = thinking_text

    @discord.ui.button(
        label="Show thinking",
        style=discord.ButtonStyle.secondary,
        custom_id="show_thinking",
    )
    async def show_thinking(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ):
        await interaction.response.send_message(self.thinking_text, ephemeral=True)


def should_respond(message: discord.Message, bot_user: discord.User) -> bool:
    """Determine if the bot should respond to a message."""
    if message.author.bot:
        return False
    if bot_user in message.mentions:
        return True
    if (
        message.reference
        and hasattr(message.reference, "resolved")
        and message.reference.resolved
        and message.reference.resolved.author.id == bot_user.id
    ):
        return True
    return False


async def download_image_attachments(
    attachments: list[discord.Attachment],
    vision_client: VisionClient,
    store: MessageStore | None = None,
) -> list[dict]:
    """Download image attachments and describe them with Qwen 3 vision.

    When a store is provided, checks for an existing blob by content hash
    and reuses its description instead of calling the vision model again.
    """
    results = []
    for att in attachments:
        if not att.content_type or not att.content_type.startswith("image/"):
            continue
        data: bytes | None = None
        try:
            data = await att.read()
            sha = hashlib.sha256(data).hexdigest()
            existing = store.get_blob(sha) if store else None
            if existing:
                description = existing.description
                logger.info("Blob cache hit for %s (%s)", att.filename, sha[:12])
            else:
                description = await vision_client.describe(data, att.content_type)
            results.append(
                {
                    "data": data,
                    "content_type": att.content_type,
                    "filename": att.filename,
                    "description": description,
                }
            )
        except Exception:
            logger.exception("Failed to process attachment %s", att.filename)
            # Still include the attachment so the model knows an image was
            # sent rather than silently pretending it doesn't exist.
            results.append(
                {
                    "data": data,
                    "content_type": att.content_type,
                    "filename": att.filename,
                    "description": "(image could not be processed)",
                }
            )
    return results


def _has_embeddable_content(message: discord.Message) -> bool:
    """Return True if the message has text, image attachments, or Discord embeds."""
    if message.content.strip():
        return True
    if any(
        a.content_type and a.content_type.startswith("image/")
        for a in message.attachments
    ):
        return True
    return any(e.title or e.description for e in message.embeds)


def _extract_embed_text(message: discord.Message) -> str:
    """Extract text from Discord embeds as a single string."""
    parts = []
    for embed in message.embeds:
        if embed.title:
            parts.append(embed.title)
        if embed.description:
            parts.append(embed.description)
    return "\n".join(parts)


class ChatBot(discord.Client):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True
        super().__init__(intents=intents)
        self.embed_client = EmbeddingClient()
        self.vision_client = VisionClient()
        self.agent = create_agent()

    async def on_ready(self):
        logger.info("Discord bot connected as %s", self.user)
        # Re-register ThinkingView for recent bot messages so the "Show thinking"
        # button keeps working after a pod restart.
        try:
            with Session(get_engine()) as session:
                store = MessageStore(session=session, embed_client=self.embed_client)
                messages_with_thinking = store.get_messages_with_thinking()
            for msg in messages_with_thinking:
                self.add_view(
                    ThinkingView(msg.thinking),
                    message_id=int(msg.discord_message_id),
                )
            logger.info(
                "Re-registered ThinkingView for %d bot messages",
                len(messages_with_thinking),
            )
        except Exception:
            logger.exception("Failed to re-register ThinkingViews on ready")

    async def on_message(self, message: discord.Message):
        # Skip own messages — bot responses are stored explicitly after sending
        if message.author.id == self.user.id:
            return

        # Acquire lock before any expensive work (embedding, vision, LLM).
        # If another pod already claimed this message, skip it entirely.
        msg_id = str(message.id)
        channel_id = str(message.channel.id)
        try:
            with Session(get_engine()) as session:
                store = MessageStore(session=session, embed_client=self.embed_client)
                if not store.acquire_lock(msg_id, channel_id):
                    logger.debug("Message %s already claimed by another pod", msg_id)
                    return
        except Exception:
            logger.exception("Failed to acquire lock for message %s", msg_id)
            return

        await self._process_message(message)

    async def _process_message(self, message: discord.Message) -> None:
        """Process a message that this pod has locked."""
        msg_id = str(message.id)
        channel_id = str(message.channel.id)
        attachments: list[dict] = []

        if not _has_embeddable_content(message):
            logger.debug(
                "Message %s has no embeddable content, marking completed", msg_id
            )
            with Session(get_engine()) as session:
                store = MessageStore(session=session, embed_client=self.embed_client)
                store.mark_completed(msg_id)
            return

        try:
            with Session(get_engine()) as session:
                store = MessageStore(session=session, embed_client=self.embed_client)
                attachments = await download_image_attachments(
                    message.attachments, self.vision_client, store=store
                )
                content = message.content or _extract_embed_text(message)
                await store.save_message(
                    discord_message_id=msg_id,
                    channel_id=channel_id,
                    user_id=str(message.author.id),
                    username=message.author.display_name,
                    content=content,
                    is_bot=message.author.bot,
                    attachments=attachments if attachments else None,
                )
        except Exception:
            logger.exception("Failed to store message %s", msg_id)
            # Release lock so sweep can retry
            with Session(get_engine()) as session:
                store = MessageStore(session=session, embed_client=self.embed_client)
                store.release_lock(msg_id)
            return

        if not should_respond(message, self.user):
            # Message stored successfully, mark lock done
            with Session(get_engine()) as session:
                store = MessageStore(session=session, embed_client=self.embed_client)
                store.mark_completed(msg_id)
            return

        try:
            async with message.channel.typing():
                sent, response_text, thinking = await self._stream_response(
                    message, attachments
                )
        except Exception:
            logger.exception("Failed to respond to message %s", msg_id)
            try:
                await message.reply(
                    "Sorry, I'm having trouble reaching the language model right now. "
                    "Please try again in a moment."
                )
            except Exception:
                logger.exception("Failed to send error reply for message %s", msg_id)
            with Session(get_engine()) as session:
                store = MessageStore(session=session, embed_client=self.embed_client)
                store.mark_completed(msg_id)
            return

        # Store bot response separately, including thinking for button persistence
        try:
            with Session(get_engine()) as session:
                store = MessageStore(session=session, embed_client=self.embed_client)
                await store.save_message(
                    discord_message_id=str(sent.id),
                    channel_id=channel_id,
                    user_id=str(self.user.id),
                    username=self.user.display_name,
                    content=response_text,
                    is_bot=True,
                    thinking=thinking,
                )
        except Exception:
            logger.exception("Failed to store bot response for message %s", msg_id)

        with Session(get_engine()) as session:
            store = MessageStore(session=session, embed_client=self.embed_client)
            store.mark_completed(msg_id)

    async def _stream_response(
        self,
        message: discord.Message,
        current_attachments: list[dict] | None = None,
    ) -> tuple[discord.Message, str, str | None]:
        """Build context and stream the PydanticAI agent response.

        Sends an initial Discord reply on the first event, then progressively
        edits the message as new content arrives. Returns
        (sent_message, response_text, thinking_text).
        """
        from chat.agent import ChatDeps

        with Session(get_engine()) as session:
            store = MessageStore(session=session, embed_client=self.embed_client)

            # Recent window only — semantic recall is now on-demand via tools
            recent = store.get_recent(str(message.channel.id), limit=20)

            # Run agent with deps so tools can access store + embeddings
            deps = ChatDeps(
                channel_id=str(message.channel.id),
                store=store,
                embed_client=self.embed_client,
            )

            # Load attachments for recent messages
            all_msg_ids = [m.id for m in recent if m.id is not None]
            attachments_by_msg = store.get_attachments(all_msg_ids)

            # Fetch summaries for ambient context
            channel_summary = store.get_channel_summary(str(message.channel.id))
            recent_user_ids = list({m.user_id for m in recent if not m.is_bot})
            user_summaries = store.get_user_summaries_for_users(
                str(message.channel.id), recent_user_ids
            )

            # Build summary context header
            summary_header = ""
            if channel_summary:
                summary_header += f"[Channel context: {channel_summary.summary}]\n\n"
            if user_summaries:
                summary_header += "[People in this conversation:\n"
                for s in user_summaries:
                    summary_header += f" - {s.username}: {s.summary}\n"
                summary_header += "]\n\n"

            context = (
                summary_header
                + "Recent conversation:\n"
                + format_context_messages(recent, attachments_by_msg)
            )

            # Tell the model its own live Discord identity so it recognizes
            # mentions of itself. The numeric user ID isn't known when the
            # agent is constructed (before the gateway connects), so it has to
            # be injected per request here.
            identity_note = (
                f'[Your identity here: you are "{self.user.display_name}" '
                f'(Discord user ID {self.user.id}). Any "<@{self.user.id}>" '
                "mention, or a reply to one of your own messages, in this "
                "conversation is someone talking to or about YOU — not a "
                "third party to look up.]"
            )

            user_prompt = (
                f"{identity_note}\n\n{context}\n\nCurrent message from "
                f"{message.author.display_name}: {message.content}"
            )

            # Include current message images in prompt
            image_parts: list[BinaryContent] = []
            if current_attachments:
                image_context = "\n".join(
                    f"[Attached image '{a['filename']}': {a['description']}]"
                    for a in current_attachments
                )
                user_prompt += f"\n{image_context}"
                for a in current_attachments:
                    if a["data"] is not None:
                        image_parts.append(
                            BinaryContent(data=a["data"], media_type=a["content_type"])
                        )

            # Auto-search when images are attached
            if current_attachments:
                descriptions = " ".join(
                    a["description"]
                    for a in current_attachments
                    if a["description"] != "(image could not be processed)"
                )
                if descriptions:
                    try:
                        search_results = await search_web(descriptions)
                        user_prompt += (
                            f"\n\n[Auto-search results for attached image]\n"
                            f"{search_results}"
                        )
                    except Exception:
                        logger.warning(
                            "Auto-search for image failed, continuing without"
                        )

            agent_prompt: str | list = user_prompt
            if image_parts:
                agent_prompt = [user_prompt, *image_parts]

            # Streaming state
            sent: discord.Message | None = None
            thinking_parts: list[str] = []
            tool_queries: list[str] = []
            response_text = ""
            last_edit_time = 0.0
            had_events = False

            async def _ensure_sent(content: str) -> discord.Message:
                nonlocal sent
                if sent is None:
                    sent = await message.reply(content)
                return sent

            async def _edit_if_due(content: str, force: bool = False) -> None:
                nonlocal last_edit_time
                now = asyncio.get_event_loop().time()
                if force or (now - last_edit_time) >= STREAM_EDIT_INTERVAL:
                    if sent is not None:
                        await sent.edit(content=content)
                        last_edit_time = now

            async for event in self.agent.run_stream_events(agent_prompt, deps=deps):
                had_events = True

                if isinstance(event, AgentRunResultEvent):
                    # Use the authoritative final output if available
                    final_output = event.result.output
                    if final_output and isinstance(final_output, str):
                        response_text = final_output
                elif isinstance(event, PartDeltaEvent):
                    if isinstance(event.delta, ThinkingPartDelta):
                        await _ensure_sent("\U0001f4ad Thinking...")
                        thinking_parts.append(event.delta.content_delta)
                    elif isinstance(event.delta, TextPartDelta):
                        response_text += event.delta.content_delta
                        await _ensure_sent(response_text)
                        await _edit_if_due(response_text)
                elif isinstance(event, FunctionToolCallEvent):
                    args = event.part.args
                    if isinstance(args, str):
                        try:
                            args = json.loads(args)
                        except (json.JSONDecodeError, TypeError):
                            pass
                    if isinstance(args, dict):
                        query = args.get("query", str(args))
                    else:
                        query = str(args)
                    tool_queries.append(query)
                    bullets = "\n".join(f"\u2022 {q}" for q in tool_queries)
                    content = f"\U0001f50d Searching...\n{bullets}"
                    await _ensure_sent(content)
                    await _edit_if_due(content, force=True)

            # Fallback if no events arrived or no text was produced
            if not had_events or not response_text:
                fallback = (
                    "Sorry, I'm having trouble formulating a response. "
                    "Please try again."
                )
                if sent is not None:
                    # Already sent (e.g. thinking indicator) — edit to fallback
                    await sent.edit(content=fallback)
                else:
                    sent = await message.reply(fallback)
                return sent, fallback, None

            # Ensure a reply was sent. AgentRunResultEvent alone never calls
            # _ensure_sent, so sent may still be None here.
            await _ensure_sent(response_text)

            # Final edit with complete response and optional ThinkingView
            thinking_text: str | None = None
            if thinking_parts:
                raw = "".join(thinking_parts).strip()
                if raw:
                    thinking_text = _truncate_thinking(raw)

            if thinking_text:
                await sent.edit(content=response_text, view=ThinkingView(thinking_text))
            else:
                await _edit_if_due(response_text, force=True)

            return sent, response_text, thinking_text

    async def reprocess_message(self, discord_message_id: str, channel_id: str) -> None:
        """Re-fetch a message from Discord and process it. Used by the sweep."""
        channel = self.get_channel(int(channel_id))
        if not channel:
            logger.warning(
                "Cannot reprocess %s: channel %s not found",
                discord_message_id,
                channel_id,
            )
            return
        try:
            message = await channel.fetch_message(int(discord_message_id))
        except discord.NotFound:
            logger.info(
                "Message %s was deleted, marking lock completed", discord_message_id
            )
            with Session(get_engine()) as session:
                store = MessageStore(session=session, embed_client=self.embed_client)
                store.mark_completed(discord_message_id)
            return
        except discord.HTTPException:
            logger.exception(
                "Failed to fetch message %s for reprocessing", discord_message_id
            )
            return
        await self._process_message(message)


#: Module-level reference to the running bot, set by `create_bot()`.
#: Used by in-process callers (e.g. the agent.notify MCP tool) that
#: need to send Discord messages without going through a FastAPI
#: request scope. Tests patch this attribute directly.
bot: ChatBot | None = None


def create_bot() -> ChatBot:
    """Factory function for the Discord bot."""
    global bot
    bot = ChatBot()
    return bot


async def send_message(channel_id: str, content: str, level: str = "info") -> None:
    """Send a Discord message from in-process code (e.g. an MCP tool).

    Used by the agent.notify tool and any other internal caller that
    needs to ping Discord without going through NATS.
    """
    prefix = {"info": "", "warn": "⚠️ ", "error": "\U0001f534 "}.get(level, "")
    if bot is None:
        raise RuntimeError("Discord bot is not running; cannot send message")
    channel = bot.get_channel(int(channel_id))
    if channel is None:
        # bot.get_channel is sync and returns None if cache hasn't seen the channel;
        # fall back to API fetch.
        channel = await bot.fetch_channel(int(channel_id))
    await channel.send(f"{prefix}{content}")
