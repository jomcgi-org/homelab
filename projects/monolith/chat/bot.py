"""Discord bot -- gateway listener and message handler."""

import asyncio
import hashlib
import json
import logging
import os
import re
import time

import discord
from pydantic_ai import (
    AgentRunResultEvent,
    BinaryContent,
    FunctionToolCallEvent,
    PartDeltaEvent,
    TextPartDelta,
    ThinkingPartDelta,
)

from chat.agent import (
    create_agent,
    create_fact_check_agent,
    format_context_messages,
    today_str,
)
from shared.embedding import EmbeddingClient
from chat.store import MessageStore
from chat.vision import VisionClient
from chat.web_search import search_web
from chat import goosecracker
from chat import goosecracker_progress
from app.db import get_engine

from sqlmodel import Session

logger = logging.getLogger(__name__)

DISCORD_BOT_TOKEN = os.environ.get("DISCORD_BOT_TOKEN", "")
DISCORD_MESSAGE_LIMIT = 2000
THINKING_TRUNCATE_AT = 1985
LLM_MAX_RETRIES = 3
LLM_RETRY_BASE_DELAY = 1.0  # seconds
STREAM_EDIT_INTERVAL = 1.0

# Fact-check (the "Get your facts STR8!" button). The result streams into a
# thread anchored to the response so it stays out of the main channel.
FACT_CHECK_PREFIX = "**Fact check:**\n"
FACT_CHECK_THREAD_NAME = "Fact check"
FACT_CHECK_PLACEHOLDER = "\U0001f50d Fact-checking..."
# How much of the response seeds the mandatory pre-search query (claims usually
# lead, and SearXNG handles a long query fine; this just bounds it).
FACT_CHECK_SEARCH_SEED_CHARS = 500


def _truncate_thinking(thinking: str) -> str:
    """Truncate thinking text if it exceeds Discord's message limit."""
    if len(thinking) <= DISCORD_MESSAGE_LIMIT:
        return thinking
    return thinking[:THINKING_TRUNCATE_AT] + "... (truncated)"


# Live /artifact progress streaming (ADR 024). The guest streams goose's
# stdout to the in-process progress buffer; the bot edits the thread message on
# this cadence so the owner sees activity instead of a multi-minute silent wait.
GOOSECRACKER_STREAM_INTERVAL = 1.5  # seconds between Discord edits (rate-limit safe)
GOOSECRACKER_STREAM_TIMEOUT = 900  # stop streaming after 15 min (run is wedged)
GOOSECRACKER_THINKING_AFTER = 4.0  # no new output for this long -> show "Thinking"

# Pull the human-meaningful beats out of goose's raw stdout (which otherwise
# echoes the entire file it writes). "Created <path> (N lines)" and the
# goose-result "summary:" line are the only bits worth showing the owner.
_GOOSECRACKER_WROTE_RE = re.compile(r"Created\s+\S+\s+\((\d+)\s+lines?\)")
_GOOSECRACKER_SUMMARY_RE = re.compile(r"^\s*summary:\s*(.+?)\s*`*\s*$", re.MULTILINE)


def _render_goosecracker_progress(snap, elapsed: int, kind: str = "artifact") -> str:
    """Render a concise live progress message from a progress snapshot.

    ``snap`` is a chat.goosecracker_progress.Progress or None (nothing yet).
    Goose's raw stdout includes the whole file it writes, which is noise, so this
    shows only a phase line (Thinking while the model reasons quietly, Working
    while output flows, Done on done) plus the meaningful beats extracted from the
    stream: lines written and the goose-result summary. ``kind`` selects the copy:
    "artifact" (the iterable HTML builder) or "agent" (the one-shot coding agent).
    """
    minutes, seconds = divmod(max(elapsed, 0), 60)
    el = f"{minutes}:{seconds:02d}"
    text = snap.text if snap else ""
    done = snap is not None and snap.done
    flowing = bool(text) and (time.monotonic() - snap.updated_at) < (
        GOOSECRACKER_THINKING_AFTER
    )

    if kind == "agent":
        header, done_line, flowing_line = (
            "🤖 **Running agent**",
            f"✅ Done in {el}",
            f"⚙️ Working... ({el})",
        )
    else:
        header, done_line, flowing_line = (
            "🛠 **Building your artifact**",
            f"✅ Built in {el} (reply here to iterate)",
            f"⚙️ Writing the artifact... ({el})",
        )

    lines = [header]
    if done:
        lines.append(done_line)
    elif flowing:
        lines.append(flowing_line)
    else:
        lines.append(f"🧠 Thinking... ({el})")

    wrote = _GOOSECRACKER_WROTE_RE.search(text)
    if wrote:
        lines.append(f"📝 Wrote {wrote.group(1)} lines")
    summary = _GOOSECRACKER_SUMMARY_RE.search(text)
    if summary:
        lines.append(f"📦 {summary.group(1).strip()}")

    return "\n".join(lines)[:DISCORD_MESSAGE_LIMIT]


_fact_check_agent = None


def _get_fact_check_agent():
    global _fact_check_agent
    if _fact_check_agent is None:
        _fact_check_agent = create_fact_check_agent()
    return _fact_check_agent


def _get_recent_context(channel_id: str) -> str:
    """Fetch recent channel messages as a formatted context string (sync).

    Called via asyncio.to_thread from the fact_check button callback.
    Returns empty string if the bot is not initialised or the lookup fails.
    """
    if bot is None:
        return ""
    with Session(get_engine()) as session:
        store = MessageStore(session=session, embed_client=bot.embed_client)
        recent = store.get_recent(channel_id, limit=10)
        return format_context_messages(recent, {})


def _clip_fact_check(text: str) -> str:
    """Clip a fact-check body to Discord's single-message limit."""
    if len(text) <= DISCORD_MESSAGE_LIMIT:
        return text
    return text[:THINKING_TRUNCATE_AT] + "... (truncated)"


async def _build_fact_check_prompt(response_text: str, context: str) -> str:
    """Build the fact-check prompt with fresh, searched grounding.

    Prepends today's date and a mandatory live web search seeded from the
    response, so the model judges against current reality instead of its stale
    training memory (it was confidently calling true, post-cutoff facts
    "fabrication"). The search is best-effort: on failure the agent still has
    its own ``web_search`` tool, so we degrade to a no-results prompt rather
    than aborting the fact-check.
    """
    parts = [f"Today is {today_str()}."]
    try:
        live = await search_web(response_text[:FACT_CHECK_SEARCH_SEED_CHARS])
    except Exception:
        logger.exception("Fact-check pre-search failed")
        live = ""
    if live:
        parts.append(
            "Live web search results for the claims (current ground truth, "
            f"trust these over your memory):\n{live}"
        )
    if context:
        parts.append(f"Recent conversation:\n{context}")
    parts.append(f"Fact-check this response:\n\n{response_text}")
    return "\n\n".join(parts)


async def _stream_fact_check(message: discord.Message, agent, prompt: str) -> str:
    """Stream the fact-check agent's output into an already-posted ``message``.

    Edits ``message`` on the ``STREAM_EDIT_INTERVAL`` cadence as text deltas
    arrive (so Discord rate limits are respected, mirroring ``_stream_response``
    and the goosecracker progress stream), then a final edit with the
    authoritative output. Returns the final fact-check text.
    """
    streamed = ""
    final = ""
    last_edit = 0.0
    async for event in agent.run_stream_events(prompt):
        if isinstance(event, AgentRunResultEvent):
            out = event.result.output
            if out and isinstance(out, str):
                final = out
        elif isinstance(event, PartDeltaEvent) and isinstance(
            event.delta, TextPartDelta
        ):
            streamed += event.delta.content_delta
            now = asyncio.get_event_loop().time()
            if (now - last_edit) >= STREAM_EDIT_INTERVAL:
                await message.edit(
                    content=_clip_fact_check(FACT_CHECK_PREFIX + streamed)
                )
                last_edit = now

    body = final or streamed
    await message.edit(content=_clip_fact_check(FACT_CHECK_PREFIX + body))
    return body


class BotMessageView(discord.ui.View):
    """Discord View with action buttons on bot responses.

    Always shows 'Get your facts STR8!' (fact-check via SearXNG).
    Shows 'Show thinking' only when the model produced reasoning text.
    """

    def __init__(self, response_text: str, thinking_text: str | None = None):
        super().__init__(timeout=None)
        self.response_text = response_text
        self.thinking_text = thinking_text
        if thinking_text is None:
            self.remove_item(self.show_thinking)

    @discord.ui.button(
        label="Show thinking",
        style=discord.ButtonStyle.secondary,
        custom_id="show_thinking",
    )
    async def show_thinking(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ):
        await interaction.response.send_message(self.thinking_text, ephemeral=True)

    @discord.ui.button(
        label="Get your facts STR8!",
        style=discord.ButtonStyle.danger,
        custom_id="fact_check",
    )
    async def fact_check(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ):
        # Durable one-per-message limit. A fact-check opens a thread anchored to
        # this message; Discord keeps ``message.thread`` populated across bot
        # restarts, so its presence means the message was already checked.
        existing = getattr(interaction.message, "thread", None)
        if existing is not None:
            await interaction.response.send_message(
                f"Already fact-checked in {existing.mention}.", ephemeral=True
            )
            return

        await interaction.response.defer()

        # Remove the button so it can't be clicked again. The edited (button-
        # removed) component is stored on the message itself, so it survives a
        # bot restart even though the re-registered persistent view starts with
        # the button present.
        self.remove_item(button)
        try:
            await interaction.message.edit(view=self)
        except discord.HTTPException:
            logger.exception("Fact-check: failed to remove button")

        try:
            channel_id = str(interaction.channel.id)
            context = await asyncio.to_thread(_get_recent_context, channel_id)
            prompt = await _build_fact_check_prompt(self.response_text, context)

            # Keep the fact-check out of the main channel: stream it inside a
            # thread anchored to the response. Discord forbids nested threads,
            # so if we're already inside one, fall back to an inline followup.
            try:
                thread = await interaction.message.create_thread(
                    name=FACT_CHECK_THREAD_NAME
                )
                await interaction.followup.send(
                    f"Fact-checking in {thread.mention}", ephemeral=True
                )
                # Attribute the check publicly so the channel knows who asked.
                # Its own message, not the placeholder, which the stream
                # overwrites with the verdict.
                await thread.send(f"Fact-check requested by {interaction.user.mention}")
                placeholder = await thread.send(FACT_CHECK_PLACEHOLDER)
            except discord.HTTPException:
                placeholder = await interaction.followup.send(FACT_CHECK_PLACEHOLDER)

            await _stream_fact_check(placeholder, _get_fact_check_agent(), prompt)
        except Exception:
            logger.exception("Fact-check failed")
            await interaction.followup.send(
                "Couldn't run the fact-check right now.", ephemeral=True
            )


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


def format_subject_note(message: discord.Message, bot_user_id: int) -> str:
    """State who the current message is about, from Discord's structured data.

    Without this, the model has to infer the subject of a "who is this / what
    are your notes on them" question by scanning the rendered conversation
    history, where every past message still carries raw ``<@id>`` tokens. It
    can grab a stale id from an earlier message instead of the person actually
    being addressed. The triggering message's own @-mentions, and the author
    of any message it is a reply to, are the authoritative targets, so we name
    them explicitly with their numeric ids. Returns "" for plain chat with no
    such target (the note is only added when there is something to resolve).
    """
    lines: list[str] = []

    mentioned = [m for m in getattr(message, "mentions", []) if m.id != bot_user_id]
    if mentioned:
        lines.append(
            "  - directly @-mentions: "
            + ", ".join(f"{m.display_name} (Discord user ID {m.id})" for m in mentioned)
        )

    ref = getattr(message, "reference", None)
    resolved = getattr(ref, "resolved", None) if ref is not None else None
    if (
        resolved is not None
        and not isinstance(resolved, discord.DeletedReferencedMessage)
        and getattr(resolved, "author", None) is not None
        and resolved.author.id != bot_user_id
    ):
        author = resolved.author
        lines.append(
            "  - is a reply to a message from: "
            f"{author.display_name} (Discord user ID {author.id})"
        )

    if not lines:
        return ""

    return (
        "\n[Who this message is about. If it asks who someone is, or for your "
        "notes or summary about a person, resolve it to these Discord user ids, "
        "not to any mention that only appears in the conversation history "
        "above:\n" + "\n".join(lines) + "]"
    )


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
        # Slash commands (e.g. /artifact, ADR 024 Task 4) live on a
        # CommandTree; on_message handles plain chat. Registered at construction,
        # synced to the guild in on_ready.
        self.tree = discord.app_commands.CommandTree(self)
        self._register_commands()

    def _register_commands(self) -> None:
        """Register application (slash) commands on the tree."""

        @self.tree.command(
            name="artifact",
            description="Build a self-contained web artifact (owner only)",
        )
        @discord.app_commands.describe(prompt="What to build")
        async def goosecracker_command(
            interaction: discord.Interaction, prompt: str
        ) -> None:
            await self._handle_goosecracker_command(interaction, prompt)

        @self.tree.command(
            name="agent",
            description="Run the coding agent on a repo (owner only)",
        )
        @discord.app_commands.describe(
            prompt="The task for the agent",
            repo="Repo to hydrate from",
        )
        @discord.app_commands.choices(
            repo=[
                discord.app_commands.Choice(name="homelab", value="homelab"),
                discord.app_commands.Choice(name="loom", value="loom"),
            ]
        )
        async def agent_command(
            interaction: discord.Interaction,
            prompt: str,
            repo: discord.app_commands.Choice[str],
        ) -> None:
            await self._handle_agent_command(interaction, prompt, repo.value)

    async def on_ready(self):
        logger.info("Discord bot connected as %s", self.user)
        # Sync slash commands globally so /artifact is available in every
        # server the bot is in (still owner-gated at execution by is_owner, so
        # non-owners just get roasted). A global sync can take up to an hour to
        # propagate on first registration; that is acceptable for a command that
        # changes rarely. We intentionally do not scope to the default server:
        # MONOLITH_AGENT_DISCORD_DEFAULT_SERVER_ID still names the home server
        # for notify, but it no longer limits where commands appear.
        try:
            await self.tree.sync()
            logger.info("Synced application commands (global)")
        except Exception:
            logger.exception("Failed to sync application commands")
        # Re-register BotMessageView for recent bot messages so buttons keep
        # working after a pod restart.
        try:
            with Session(get_engine()) as session:
                store = MessageStore(session=session, embed_client=self.embed_client)
                recent_bot_messages = store.get_recent_bot_messages()
            for msg in recent_bot_messages:
                self.add_view(
                    BotMessageView(msg.content, msg.thinking),
                    message_id=int(msg.discord_message_id),
                )
            logger.info(
                "Re-registered BotMessageView for %d bot messages",
                len(recent_bot_messages),
            )
        except Exception:
            logger.exception("Failed to re-register BotMessageViews on ready")

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

        # A reply inside a goosecracker thread iterates the artifact (Model B)
        # instead of going to the chat agent. Only threads can be goosecracker
        # sessions, so the DB lookup is skipped for ordinary channels.
        if isinstance(message.channel, discord.Thread):
            if await self._maybe_handle_goosecracker_reply(message):
                return

        await self._process_message(message)

    async def _handle_goosecracker_command(
        self, interaction: discord.Interaction, prompt: str
    ) -> None:
        """Owner-gated /artifact: open a thread and dispatch the first run."""
        if not goosecracker.is_owner(interaction.user.id):
            # Defer first: the roast hits the qwen model (with retries), which
            # routinely exceeds Discord's 3s initial-response deadline. Without
            # the defer the interaction 404s and the roast never lands.
            await interaction.response.defer(ephemeral=True)
            roast = await goosecracker.build_roast(prompt)
            await interaction.followup.send(roast, ephemeral=True)
            return

        channel = interaction.channel
        if not isinstance(channel, discord.TextChannel):
            await interaction.response.send_message(
                "Run /artifact from a normal text channel so I can open a thread.",
                ephemeral=True,
            )
            return

        # Dispatch (DB inserts) can take a beat; ack first so the interaction
        # never times out, then create the thread and kick off the run.
        await interaction.response.defer(thinking=True)
        try:
            name = f"goosecracker: {prompt}"[:90]
            thread = await channel.create_thread(
                name=name, type=discord.ChannelType.public_thread
            )
            await asyncio.to_thread(goosecracker.start_session, str(thread.id), prompt)
        except Exception:
            logger.exception("goosecracker: failed to start session")
            await interaction.followup.send(
                "Couldn't start that one. Check the logs.", ephemeral=True
            )
            return

        await interaction.followup.send(f"Building your artifact in {thread.mention}")
        try:
            intro = await thread.send("🛠 On it. Building your artifact now...")
            self._start_goosecracker_stream(str(thread.id), intro)
        except Exception:
            logger.exception("goosecracker: failed to post thread intro")

    async def _handle_agent_command(
        self, interaction: discord.Interaction, prompt: str, repo: str
    ) -> None:
        """Owner-gated /agent: open a thread and dispatch a one-shot agent run."""
        if not goosecracker.is_owner(interaction.user.id):
            await interaction.response.defer(ephemeral=True)
            roast = await goosecracker.build_roast(prompt)
            await interaction.followup.send(roast, ephemeral=True)
            return

        channel = interaction.channel
        if not isinstance(channel, discord.TextChannel):
            await interaction.response.send_message(
                "Run /agent from a normal text channel so I can open a thread.",
                ephemeral=True,
            )
            return

        await interaction.response.defer(thinking=True)
        try:
            name = f"agent: {prompt}"[:90]
            thread = await channel.create_thread(
                name=name, type=discord.ChannelType.public_thread
            )
            await asyncio.to_thread(
                goosecracker.start_agent_session, str(thread.id), repo, prompt
            )
        except Exception:
            logger.exception("goosecracker: failed to start agent session")
            await interaction.followup.send(
                "Couldn't start that one. Check the logs.", ephemeral=True
            )
            return

        await interaction.followup.send(f"Running agent in {thread.mention}")
        try:
            intro = await thread.send("🤖 On it. Running the agent now...")
            self._start_goosecracker_stream(str(thread.id), intro, kind="agent")
        except Exception:
            logger.exception("goosecracker: failed to post agent thread intro")

    def _start_goosecracker_stream(
        self, artifact_id: str, message: discord.Message, kind: str = "artifact"
    ) -> None:
        """Spawn the background task that live-edits ``message`` with run output.

        Fire-and-forget: the task ends itself on the run's done marker or a
        safety timeout. Kept on the instance so it is not garbage-collected
        mid-run, and logs any unhandled error. ``kind`` selects the progress copy
        ("artifact" or "agent").
        """
        task = asyncio.create_task(
            self._stream_goosecracker_progress(artifact_id, message, kind)
        )
        streams = getattr(self, "_goosecracker_streams", None)
        if streams is None:
            streams = set()
            self._goosecracker_streams = streams
        streams.add(task)
        task.add_done_callback(streams.discard)

    async def _stream_goosecracker_progress(
        self, artifact_id: str, message: discord.Message, kind: str = "artifact"
    ) -> None:
        """Edit ``message`` with goose's live build output until done or timeout.

        Polls the in-process progress buffer (fed by the guest's stdout stream)
        and re-renders on a fixed cadence, so Discord edits stay within rate
        limits. The final ``Artifact ready: <url>`` link is posted separately by
        the controller's Done -> outbox path (ADR 024 Task 5).
        """
        gp = goosecracker_progress
        start = time.monotonic()
        last_body = message.content
        while True:
            await asyncio.sleep(GOOSECRACKER_STREAM_INTERVAL)
            snap = gp.get(artifact_id)
            elapsed = int(time.monotonic() - start)
            body = _render_goosecracker_progress(snap, elapsed, kind)
            if body != last_body:
                try:
                    await message.edit(content=body)
                    last_body = body
                except discord.HTTPException:
                    logger.exception(
                        "goosecracker: progress edit failed for %s", artifact_id
                    )
            if snap is not None and snap.done:
                gp.clear(artifact_id)
                return
            if elapsed >= GOOSECRACKER_STREAM_TIMEOUT:
                logger.warning(
                    "goosecracker: progress stream timed out for %s", artifact_id
                )
                gp.clear(artifact_id)
                return

    async def _maybe_handle_goosecracker_reply(self, message: discord.Message) -> bool:
        """Handle a reply inside a goosecracker thread (Model B re-run).

        Returns True when the message was a goosecracker thread message (handled,
        so the caller skips the chat agent), False otherwise.
        """
        thread_id = str(message.channel.id)
        if not await asyncio.to_thread(goosecracker.is_goosecracker_thread, thread_id):
            return False

        msg_id = str(message.id)
        # Ignore other bots in the build thread: don't roast them (avoids
        # bot-to-bot loops) and don't let them reach the chat agent.
        if message.author.bot:
            await asyncio.to_thread(self._complete_lock, msg_id)
            return True
        if not goosecracker.is_owner(message.author.id):
            roast = await goosecracker.build_roast(message.content)
            await message.reply(roast)
            await asyncio.to_thread(self._complete_lock, msg_id)
            return True

        try:
            result = await asyncio.to_thread(
                goosecracker.continue_session, thread_id, message.content
            )
        except Exception:
            logger.exception("goosecracker: failed to continue session")
            await message.reply("Couldn't rebuild that one. Check the logs.")
            await asyncio.to_thread(self._complete_lock, msg_id)
            return True

        if result is None:
            # Lost a race with session teardown; let normal handling take it.
            return False

        progress_msg = await message.reply(
            "🛠 On it. Rebuilding your artifact; the link above will hot-reload..."
        )
        self._start_goosecracker_stream(thread_id, progress_msg)
        await asyncio.to_thread(self._complete_lock, msg_id)
        return True

    def _complete_lock(self, msg_id: str) -> None:
        """Mark a message lock completed (sync; call via asyncio.to_thread)."""
        with Session(get_engine()) as session:
            store = MessageStore(session=session, embed_client=self.embed_client)
            store.mark_completed(msg_id)

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

            # Authoritative subject resolved from Discord's structured mention
            # and reply data, so the model targets the right person rather than
            # a stale "<@id>" scraped from the history above.
            subject_note = format_subject_note(message, self.user.id)

            user_prompt = (
                f"{identity_note}\n\n{context}\n\nCurrent message from "
                f"{message.author.display_name}: {message.content}"
                f"{subject_note}"
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

            await sent.edit(
                content=response_text, view=BotMessageView(response_text, thinking_text)
            )

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
