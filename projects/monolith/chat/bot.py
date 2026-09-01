"""Discord bot -- gateway listener and message handler."""

import asyncio
import hashlib
import io
import json
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

import discord
from pydantic_ai import (
    AgentRunResultEvent,
    BinaryContent,
    FunctionToolCallEvent,
    FunctionToolResultEvent,
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
from chat import acl
from chat import attention
from chat import attention_log
from chat import directives
from chat import safeguards
from chat import orchestrator
from chat import reply_repair_log
from chat import summarizer
from chat.reply_sanitize import repair_leaked_reply
from chat.models import ReactionEvent
from core.db import get_engine

from sqlmodel import Session, select

logger = logging.getLogger(__name__)

# Default model for a Discord-started agent session, and the choices offered
# on /agent. Sourced from agent_sessions so the picker cannot drift from what
# model_family accepts. A model pins the session's adapter family for its
# whole life, so this is chosen once at /agent time and not per turn.
DEFAULT_AGENT_MODEL = "luna"
# The full universe of selectable models, kept as a LITERAL, not imported from
# agent_sessions. A module-level import there would put agent_sessions into the
# Bazel dep graph of every target that imports chat.bot, which is why every
# other agent_sessions import in this file is function-local. Two things keep
# this copy honest instead: the drift test (test_agent_model_choices_match_
# supported_models) in a target that already depends on both, and the filter
# below mirroring agent_sessions.offered_models, which the same test checks
# for behavioural equality across env settings.
_ALL_AGENT_MODEL_CHOICES = ("luna", "terra", "sol", "opus", "sonnet", "fable")


def _filter_agent_models(models, configured: str) -> tuple:
    """Narrow ``models`` to a comma-separated allowlist; empty means all.

    Mirrors agent_sessions.offered_models (same semantics, no import: see
    above). The drift test in chat/bot_on_message_test.py asserts behavioural
    equality between the two across every setting the chart can produce.
    """
    allowed = {part.strip() for part in (configured or "").split(",") if part.strip()}
    if not allowed:
        return tuple(models)
    return tuple(model for model in models if model in allowed)


# Per-env narrowing (issue #4859), read at import like the rest of this
# module's config: AGENT_MODELS is a comma-separated subset; empty or unset
# offers everything. Wired from chart value agents.models so each deployment
# can narrow the shared image's picker without shipping a different image.
AGENT_MODEL_CHOICES = _filter_agent_models(
    _ALL_AGENT_MODEL_CHOICES, os.environ.get("AGENT_MODELS", "")
)
AGENT_REACTION_QUEUED = "⏳"


DISCORD_BOT_TOKEN = os.environ.get("DISCORD_BOT_TOKEN", "")
DISCORD_MESSAGE_LIMIT = 2000
THINKING_TRUNCATE_AT = 1985
LLM_MAX_RETRIES = 3
LLM_RETRY_BASE_DELAY = 1.0  # seconds
STREAM_EDIT_INTERVAL = 1.0

# run_code attachment flush (ADR agents/044). The sandbox handler already
# caps well under this (2 MiB/file, 5 MiB total), so this is a backstop
# against Discord's own base-tier upload limit, not the primary control.
RUN_PYTHON_MAX_ATTACHMENTS = 8
RUN_PYTHON_MAX_ATTACHMENT_BYTES = 8 * 1024 * 1024
# Bounded model-repair passes when a chat reply leaks tool-call scaffolding
# (chat.reply_sanitize). 0 disables repair (scrub only).
AGENT_REPLY_REPAIR_MAX_TURNS = int(os.environ.get("AGENT_REPLY_REPAIR_MAX_TURNS", "2"))
# Shown when the shield cannot recover any clean text but a chart was produced.
_CHART_ONLY_CAPTION = "Here's the chart 👇"


def _build_run_code_attachments(generated_files: list) -> "list[discord.File]":
    """Build discord.File objects from run_code (path, bytes) tuples.

    Capped at RUN_PYTHON_MAX_ATTACHMENTS files and RUN_PYTHON_MAX_ATTACHMENT_BYTES
    per file; anything over either cap is logged and dropped. Shared by the
    direct concierge tool loop and the orchestrator chat-verdict reply so both
    surface a generated chart the same way.
    """
    to_send = generated_files[:RUN_PYTHON_MAX_ATTACHMENTS]
    dropped = [name for name, _ in generated_files[RUN_PYTHON_MAX_ATTACHMENTS:]]
    files: list[discord.File] = []
    for name, data in to_send:
        if len(data) > RUN_PYTHON_MAX_ATTACHMENT_BYTES:
            dropped.append(name)
            continue
        filename = os.path.basename(name) or "output.bin"
        files.append(discord.File(io.BytesIO(data), filename=filename))
    if dropped:
        logger.info(
            "run_code: skipping %d generated file(s) over the attachment "
            "cap/size limit: %s",
            len(dropped),
            ", ".join(dropped),
        )
    return files


async def _deliver_chat_reply(send, text: "str | None", generated_files: list):
    """Post a chat-route reply and any generated files as ONE message.

    Discord's send/reply take ``content`` and ``files`` together, so a generated
    chart no longer arrives as a separate second message after the text. When
    the shield (chat.reply_sanitize) recovered no clean text but a chart exists,
    caption it; when there is neither text nor a file, skip. ``send`` is the
    site's send callable (``interaction.followup.send`` or ``message.reply``).
    Returns the sent message, or None. Best-effort: on failure, falls back to a
    text-only send so the reply is not lost entirely.
    """
    files = _build_run_code_attachments(generated_files) if generated_files else []
    body = (text or "").strip()
    if not body and files:
        body = _CHART_ONLY_CAPTION
    if not body and not files:
        return None
    try:
        if files:
            return await send(content=body or None, files=files)
        return await send(body)
    except Exception:
        logger.exception("run_code: failed to deliver chat reply with attachments")
        if body:
            try:
                return await send(body)
            except Exception:
                logger.exception("run_code: text-only fallback also failed")
        return None


# Fact-check (the "Get your facts STR8!" button). The result streams into a
# thread anchored to the response so it stays out of the main channel.
FACT_CHECK_PREFIX = "**Fact check:**\n"
FACT_CHECK_THREAD_NAME = "Fact check"
FACT_CHECK_PLACEHOLDER = "\U0001f50d Fact-checking..."
# How much of the response seeds the mandatory pre-search query (claims usually
# lead, and SearXNG handles a long query fine; this just bounds it).
FACT_CHECK_SEARCH_SEED_CHARS = 500

# Degenerate "I have nothing to add" outputs the model sometimes emits verbatim
# instead of real text -- typically when the channel directive tells it to stay
# quiet on banter but an ambient engage forced a turn anyway. Posting one reads
# as a broken "(empty)" message and draws negative reactions (/improve-ambient
# episodes 168, 170). Compared case-insensitively against the stripped reply so
# it never reaches Discord. See _stream_response's no-content handling.
_EMPTY_REPLY_MARKERS = frozenset(
    {"(empty)", "(no response)", "(none)", "(no reply)", "(silence)"}
)


def _is_empty_reply(text: str) -> bool:
    """A reply with no real content: blank, whitespace-only, or a bare
    placeholder marker the model emits when it has nothing to say."""
    stripped = text.strip()
    if not stripped or stripped.lower() in _EMPTY_REPLY_MARKERS:
        return True
    # The same failure in prose form: a bracketed meta-note explaining the
    # silence ("[No response required - message contains only casual
    # meme/reaction...]") leaked to the channel and read as a bug
    # (/improve-ambient episode 190).
    lowered = stripped.lower()
    return (
        stripped.startswith("[")
        and stripped.endswith("]")
        and ("no response" in lowered or "no reply" in lowered)
    )


def _truncate_thinking(thinking: str) -> str:
    """Truncate thinking text if it exceeds Discord's message limit."""
    if len(thinking) <= DISCORD_MESSAGE_LIMIT:
        return thinking
    return thinking[:THINKING_TRUNCATE_AT] + "... (truncated)"


def _format_codex_login_message(result: dict) -> str:
    """Explain the broker action needed before a Codex turn can run."""
    message = (
        result.get("message") or "Codex login is required before this session can run."
    )
    verification_url = result.get("verification_url")
    user_code = result.get("user_code")
    if verification_url and user_code:
        return (
            f"🔐 {message}\nApprove in your browser: {verification_url}"
            f"\nUser code: `{user_code}`"
        )
    if result.get("pending"):
        return f"🔐 {message}"
    if result.get("error"):
        return f"🔐 {message}\nBroker error: {result['error']}"
    return f"🔐 {message}"


@dataclass
class AgentFlowOutcome:
    """Result of ``start_agent_flow`` after the orchestrator verdict."""

    thread: "discord.Thread | None" = None
    chat_reply: str | None = None
    generated_files: list = field(default_factory=list)
    pending_proposal: list = field(default_factory=list)


def _render_reply_guidance(verdict: "orchestrator.ChatVerdict") -> str:
    """Render chat-route guidance as context for the local concierge."""
    parts = []
    if verdict.context:
        parts.append(f"Context I found: {verdict.context}")
    if verdict.direction:
        parts.append(f"Suggested direction: {verdict.direction}")
    if verdict.redirect:
        parts.append(f"Possible redirect: {verdict.redirect}")
    return "\n".join(parts)


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
    and the normal response stream), then a final edit with the
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


# Overhead of the code fence and header formatting in _format_agent_prompt_echo,
# reserved out of Discord's 2000-char message cap so the echo never overflows.
_PROMPT_ECHO_MAX = 2000


def _format_agent_prompt_echo(user: discord.abc.User, prompt: str) -> str:
    """Render the ``/agent`` prompt as a fenced, attributed echo message.

    The prompt otherwise survives only in the thread title, which Discord
    truncates to ~90 chars, so a long prompt is lost. Any code fence in the
    user-controlled prompt is neutralized (a zero-width space is woven through
    the backticks) so it cannot break out of the block, and the whole message is
    capped to fit one Discord message (2000 chars).
    """
    zwsp = chr(0x200B)  # zero-width space woven through backticks to defuse fences
    safe = prompt.replace("```", f"`{zwsp}`{zwsp}`")
    header = f"Prompt from {user.mention}:\n"
    budget = _PROMPT_ECHO_MAX - len(header) - len("```\n\n```")
    if len(safe) > budget:
        safe = safe[: budget - 1].rstrip() + "…"
    return f"{header}```\n{safe}\n```"


def _record_reaction(data: dict, require_prior_add: bool = False) -> None:
    """Persist one human reaction on a bot message.

    Synchronous (opens its own session); call via ``asyncio.to_thread`` from
    the bot's async handlers, never with the caller's session. Best-effort:
    callers wrap this so a persistence failure never blocks the directive
    confirm/discard flow it runs ahead of.

    When ``require_prior_add`` is set (the remove path), the row is inserted
    only if a matching ``action='add'`` row already exists for the same
    (message_id, reactor_id, emoji). Discord does not send message_author_id on
    REACTION_REMOVE, so we cannot re-check the target was Bosun's message from
    the payload; but an add row was stored ONLY for bot-authored messages, so
    its existence is itself proof the target was Bosun's, and it guarantees a
    remove is never logged without the add it cancels.
    """
    with Session(get_engine()) as session:
        if require_prior_add:
            prior = session.exec(
                select(ReactionEvent).where(
                    ReactionEvent.message_id == data["message_id"],
                    ReactionEvent.reactor_id == data["reactor_id"],
                    ReactionEvent.emoji == data["emoji"],
                    ReactionEvent.action == "add",
                )
            ).first()
            if prior is None:
                return
        session.add(ReactionEvent(**data))
        session.commit()


class ChatBot(discord.Client):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True
        super().__init__(intents=intents)
        self.embed_client = EmbeddingClient()
        self.vision_client = VisionClient()
        self.agent = create_agent()
        # Slash commands (e.g. /agent, ADR 024) live on a
        # CommandTree; on_message handles plain chat. Registered at construction,
        # synced to the guild in on_ready.
        self.tree = discord.app_commands.CommandTree(self)
        self._register_commands()
        # Live references to fire-and-forget safeguards intent classifies
        # (asyncio only holds weak refs to tasks; without this set a scoring
        # task could be garbage-collected mid-flight).
        self._safeguards_tasks: set[asyncio.Task] = set()

    def _register_commands(self) -> None:
        """Register application (slash) commands on the tree."""

        @self.tree.command(
            name="agent",
            description="Run the coding agent on a repo (or leave repo empty to build an artifact)",
        )
        @discord.app_commands.describe(
            prompt="The task for the agent",
            repo="Repo to hydrate from; leave empty to just build an artifact",
            model="Model to run (default luna). Pins the session's adapter family.",
        )
        @discord.app_commands.choices(
            model=[
                discord.app_commands.Choice(name=name, value=name)
                for name in AGENT_MODEL_CHOICES
            ]
        )
        async def agent_command(
            interaction: discord.Interaction,
            prompt: str,
            repo: str = "",
            model: str = DEFAULT_AGENT_MODEL,
        ) -> None:
            await self._handle_agent_command(interaction, prompt, repo, model)

        @agent_command.autocomplete("repo")
        async def agent_repo_autocomplete(
            interaction: discord.Interaction, current: str
        ) -> list[discord.app_commands.Choice[str]]:
            """Offer only the repos this server is granted (ADR 029).

            The suggestions are UX only; the real gate is server-side in
            ``_handle_agent_command`` via ``acl.is_granted``, so a user typing a
            non-granted repo is still refused. Discord caps autocomplete at 25.
            """
            scopes = await asyncio.to_thread(
                acl.allowed_scopes,
                interaction.guild_id,
                interaction.user.id,
                "agent",
            )
            needle = (current or "").lower()
            return [
                discord.app_commands.Choice(name=scope, value=scope)
                for scope in sorted(scopes)
                if needle in scope.lower()
            ][:25]

    async def on_ready(self):
        logger.info("Discord bot connected as %s", self.user)
        # Sync slash commands globally so they are available in every server the
        # bot is in (execution is gated by the ADR 029 feature-grant ACL, so a
        # server or user without a grant is refused). A global sync can take up
        # to an hour to
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

    # How far back to look for a recent bot tag when deciding to lean into an
    # ambient reply (ADR 035 engagement policy). A thread the bot was tagged
    # in, or a channel tagged within this window, gets a lower engage
    # threshold.
    _RECENT_TAG_MESSAGES = 10
    _RECENT_TAG_WINDOW = timedelta(minutes=10)

    def _recently_tagged(self, channel_id: str, exclude_message_id: str) -> bool:
        """True if the bot was @mentioned in this channel/thread within the
        last ``_RECENT_TAG_MESSAGES`` messages and ``_RECENT_TAG_WINDOW``.
        Threads are channels in Discord, so this one query covers both "a
        thread I've tagged in" and "a window around a channel tag". Sync;
        call via asyncio.to_thread. Fails closed to False.
        """
        try:
            with Session(get_engine()) as session:
                store = MessageStore(session=session, embed_client=self.embed_client)
                recent = store.get_recent(channel_id, limit=self._RECENT_TAG_MESSAGES)
        except Exception:
            logger.exception("attention: recent-tag lookup failed")
            return False
        needles = (f"<@{self.user.id}>", f"<@!{self.user.id}>")
        now = datetime.now(timezone.utc)
        for m in recent:
            if m.discord_message_id == exclude_message_id:
                continue
            created = (
                m.created_at
                if m.created_at.tzinfo
                else m.created_at.replace(tzinfo=timezone.utc)
            )
            if now - created > self._RECENT_TAG_WINDOW:
                continue
            if m.content and any(n in m.content for n in needles):
                return True
        return False

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

        # A reply inside an agent thread continues that session instead of going
        # to the chat agent. Skip the session lookup for ordinary channels.
        if isinstance(message.channel, discord.Thread):
            if await self._maybe_handle_agent_thread_reply(message):
                return

        # Trust safeguards (ADR chat/003): heuristic signals update the
        # per-user ledger on every message, before any reply, agent run, or
        # message storage. A locked-out author never gets a reply. The brig
        # emoji on their message says "seen, but suppressed": it lands when they
        # address the bot directly, and also when the attention classifier would
        # have engaged them in an ambient channel. That ambient case runs the
        # classify (one LLM call) but nothing downstream, so a gated lurker can
        # tell Bosun would have replied; a locked user whose ambient message the
        # classifier ignores stays silent. observe_message fails open, so a
        # ledger error can never block normal chat.
        guild_id = message.guild.id if message.guild else None
        addressed = should_respond(message, self.user)
        safeguards_payload = {
            "guild_id": str(guild_id) if guild_id else "",
            "channel_id": channel_id,
            "message_id": msg_id,
            "user_id": str(message.author.id),
            "content": message.content or "",
            "addressed": addressed,
            "author_is_bot": bool(getattr(message.author, "bot", False)),
            "bot_user_id": str(self.user.id),
        }
        try:
            verdict = await asyncio.to_thread(
                safeguards.observe_message, safeguards_payload
            )
        except Exception:
            # observe_message already fails open internally; this guards the
            # thread dispatch itself so safeguards can never strand a message.
            logger.exception("safeguards: observe dispatch failed; failing open")
            verdict = safeguards.Verdict(addressed=addressed)
        if verdict.locked_out:
            reacted = False
            # Addressed messages always warrant the brig emoji. For an
            # unaddressed (lurking) message, run ONLY the attention classify in
            # an ambient channel: if it would have engaged, the emoji still
            # lands, but no reply, agent run, or storage follows. This
            # deliberately relaxes the zero-spend-when-locked property to a
            # single classify so a gated user can see they were heard.
            should_mark = addressed
            if not should_mark and await self._resolve_ambient(
                guild_id, channel_id, message
            ):
                directive = await asyncio.to_thread(directives.get_active, channel_id)
                recently_tagged = await asyncio.to_thread(
                    self._recently_tagged, channel_id, msg_id
                )
                try:
                    result = await attention.evaluate(
                        message,
                        directive,
                        self.user,
                        True,
                        recently_tagged=recently_tagged,
                    )
                    should_mark = result.engage
                    # Log the classifier's call (mirrors the normal ambient
                    # path, so ignore rows are sampled the same way). On an
                    # engage, stamp withheld_reason=locked_out: without it a
                    # suppressed engage is invisible to /improve-ambient, which
                    # can then never see how often lockout blocks a reply-worthy
                    # message.
                    directive_version = await asyncio.to_thread(
                        directives.get_active_version, channel_id
                    )
                    await asyncio.to_thread(
                        attention_log.log_decision,
                        channel_id,
                        msg_id,
                        "engage" if result.engage else "ignore",
                        result.confidence,
                        directive_version,
                    )
                    if result.engage:
                        await asyncio.to_thread(
                            attention_log.set_withheld_reason,
                            channel_id,
                            msg_id,
                            attention_log.WITHHELD_LOCKED_OUT,
                        )
                except Exception:
                    logger.exception("safeguards: locked-out ambient classify failed")
            if should_mark:
                try:
                    await message.add_reaction(safeguards.LOCKOUT_EMOJI)
                    reacted = True
                except Exception:
                    logger.exception("safeguards: lockout reaction failed")
            if addressed or verdict.signals or reacted:
                try:
                    await asyncio.to_thread(
                        safeguards.log_enforcement, safeguards_payload, reacted
                    )
                except Exception:
                    logger.exception("safeguards: enforcement log dispatch failed")
            return

        # The LLM intent lane runs fire-and-forget on messages worth the
        # classify (bot-addressed, heuristic-flagged, or ambient-engaged
        # below): it never delays the reply, and its verdict lands on the
        # ledger for the next message.
        intent_scored = False

        def _score_intent_once() -> None:
            nonlocal intent_scored
            if intent_scored:
                return
            intent_scored = True
            task = asyncio.create_task(safeguards.score_intent(safeguards_payload))
            self._safeguards_tasks.add(task)
            task.add_done_callback(self._safeguards_tasks.discard)

        if addressed or verdict.signals:
            _score_intent_once()

        # ADR 035 attention gate (Phase 3 rollout: contained to opted-in
        # channels). Agent-triggering fires ONLY in ambient channels; mentions
        # and replies in non-ambient channels keep today's inline chat reply via
        # _process_message. Phase 4 splits an ambient engage by depth: pure
        # conversation stays in-monolith (see needs_agent below), so
        # server-wide "mentions always engage" no longer needs a heavy guest
        # run to be safe to extend.
        is_ambient = await self._resolve_ambient(guild_id, channel_id, message)
        if is_ambient:
            directive = await asyncio.to_thread(directives.get_active, channel_id)
            recently_tagged = await asyncio.to_thread(
                self._recently_tagged, channel_id, msg_id
            )
            result = await attention.evaluate(
                message, directive, self.user, True, recently_tagged=recently_tagged
            )
            directive_version = await asyncio.to_thread(
                directives.get_active_version, channel_id
            )
            await asyncio.to_thread(
                attention_log.log_decision,
                channel_id,
                msg_id,
                "engage" if result.engage else "ignore",
                result.confidence,
                directive_version,
            )
            if result.engage:
                _score_intent_once()
                # Depth split (in-monolith): pure conversation and basic web
                # lookups are answered by the in-process chat agent (low latency,
                # SearXNG built in); only repo/build/deep-research work escalates
                # to the goose guest. See ADR 035 (amended: chat is in-monolith).
                if await attention.needs_agent(message):
                    # The agent opens its own thread rather than replying in
                    # channel, so this engage's reply_message_id stays null;
                    # record why so /improve-ambient does not read it as a gate
                    # veto.
                    await asyncio.to_thread(
                        attention_log.set_withheld_reason,
                        channel_id,
                        msg_id,
                        attention_log.WITHHELD_AGENT_THREAD,
                    )
                    await self._engage_agent(message)
                else:
                    await self._process_message(
                        message,
                        force_respond=True,
                        explicit=result.explicit,
                        directive=directive,
                    )
                return

        await self._process_message(message)

    async def _resolve_ambient(
        self, guild_id, channel_id: str, message: discord.Message
    ) -> bool:
        """True if this channel has an ambient grant.

        A Discord thread is its own channel with its own id, distinct from its
        parent. Ambient grants are scoped to the parent channel, so a thread
        inherits ambient from its parent: the parent id is checked too when this
        is a thread. Agent/artifact threads never reach the ambient gate (they
        return earlier in on_message), so this only opens ordinary user threads
        under a granted channel.

        Fails closed (non-ambient) on a grants-read error (DB blip), so a
        failure never spuriously engages nor strands the message.
        """
        try:
            ambient = await asyncio.to_thread(acl.ambient_channels, guild_id)
        except Exception:
            logger.exception(
                "attention: ambient lookup failed; treating as non-ambient"
            )
            return False
        if channel_id in ambient:
            return True
        if isinstance(message.channel, discord.Thread):
            parent_id = message.channel.parent_id
            return parent_id is not None and str(parent_id) in ambient
        return False

    async def _persist_reaction_signal(
        self, payload: discord.RawReactionActionEvent, action: str
    ) -> None:
        """Persist a human reaction on one of Bosun's own messages, best-effort.

        A fluidity/productivity signal for /improve-ambient. A persistence
        failure must never block the directive confirm/discard flow that runs
        after the add call in ``on_raw_reaction_add``.

        The add and remove paths establish "the target was Bosun's message"
        differently, because Discord only sends ``message_author_id`` on
        REACTION_ADD (and only for guild adds), never on REACTION_REMOVE:
        - add: gate on ``message_author_id == self.user.id`` from the payload.
        - remove: skip that (the field is absent) and instead require a prior
          matching ``action='add'`` row. An add row exists only for bot-authored
          messages, so its presence proves the target was Bosun's and ensures a
          remove is never logged without the add it cancels.
        """
        require_prior_add = action == "remove"
        if not require_prior_add:
            author_id = getattr(payload, "message_author_id", None)
            if author_id is None or str(author_id) != str(self.user.id):
                return
        try:
            await asyncio.to_thread(
                _record_reaction,
                {
                    "channel_id": str(payload.channel_id),
                    "message_id": str(payload.message_id),
                    "target_is_bot": True,
                    "emoji": str(payload.emoji),
                    "reactor_id": str(payload.user_id),
                    "action": action,
                },
                require_prior_add,
            )
        except Exception:
            logger.exception("reaction_event: persist failed (non-fatal)")

    async def on_raw_reaction_add(
        self, payload: discord.RawReactionActionEvent
    ) -> None:
        """The confirm/discard half of the propose-then-confirm directive flow
        (ADR 035 Phase 5). Ignores the bot's own seed reactions -- only a
        HUMAN 👍/👎 on a proposal message acts. 👍 applies (subject to
        ``apply_proposal``'s freshness check); 👎 discards (no DB mutation
        needed, the proposed row just stays inactive). Either way the seed
        reactions are swapped for a terminal ✅/❌ on the summary message.

        Also persists any human reaction on a Bosun-authored message as a
        ground-truth signal for /improve-ambient (including the proposal 👍/👎
        itself, which is a signal too), before the proposal-specific logic
        below runs."""
        if payload.user_id == self.user.id:
            return

        await self._persist_reaction_signal(payload, "add")

        emoji = str(payload.emoji)
        if emoji not in ("👍", "👎"):
            return
        pid = str(payload.message_id)
        if not await asyncio.to_thread(directives.is_proposal, pid):
            return

        applied = False
        if emoji == "👍":
            applied = await asyncio.to_thread(directives.apply_proposal, pid)

        channel = self.get_channel(payload.channel_id)
        if channel is None:
            return
        try:
            summary = await channel.fetch_message(payload.message_id)
        except discord.HTTPException:
            return
        try:
            await summary.clear_reaction("👍")
            await summary.clear_reaction("👎")
            await summary.add_reaction("✅" if applied else "❌")
        except discord.HTTPException:
            logger.exception("directives: failed to finalize proposal reactions")

    async def on_raw_reaction_remove(
        self, payload: discord.RawReactionActionEvent
    ) -> None:
        """Persist a human reaction removal on a Bosun-authored message.

        A removal cancels an earlier add signal; it never confirms or discards
        a directive proposal (that only happens on add, above)."""
        if payload.user_id == self.user.id:
            return
        await self._persist_reaction_signal(payload, "remove")

    async def _handle_agent_command(
        self,
        interaction: discord.Interaction,
        prompt: str,
        repo: str,
        model: str = DEFAULT_AGENT_MODEL,
    ) -> None:
        """/agent (open per ADR 029): allowed for everyone in a server that is
        opted in, with the repo bound to that server's grants.

        ``repo`` is optional: an empty repo is a repo-less run (no checkout, e.g.
        a "build me a site" artifact). ``is_granted`` with an empty scope only
        requires that the caller hold some agent grant in this server, so a
        repo-less run is permitted wherever /agent is; a non-empty repo must be
        in the server's grants."""
        repo = repo.strip()
        channel = interaction.channel
        if not isinstance(channel, discord.TextChannel):
            await interaction.response.send_message(
                "Run /agent from a normal text channel so I can open a thread.",
                ephemeral=True,
            )
            return

        await interaction.response.defer(thinking=True)
        # route_via_orchestrator=False: running /agent IS the decision that this
        # is agent work, so the chat-vs-agent verdict does not get to overrule it.
        outcome = await self.start_agent_flow(
            channel,
            interaction.user,
            prompt,
            repo,
            route_via_orchestrator=False,
            model=model,
        )
        if outcome.chat_reply is not None:
            # ADR 036: the orchestrator routed this to chat, so reply inline
            # instead of opening a session thread. Text + any chart go in ONE
            # message (see _deliver_chat_reply).
            await _deliver_chat_reply(
                interaction.followup.send,
                outcome.chat_reply,
                outcome.generated_files,
            )
            # No motivating message on the slash path, so pass "".
            await self._flush_directive_proposals(
                outcome.pending_proposal,
                str(channel.id),
                str(interaction.user.id),
                "",
                interaction.followup.send,
            )
            return
        if outcome.thread is None:
            await interaction.followup.send(
                "Couldn't start that one. Check the logs.", ephemeral=True
            )
            return
        await interaction.followup.send(f"Running agent in {outcome.thread.mention}")

    async def start_agent_flow(
        self,
        channel: discord.TextChannel,
        user: discord.abc.User,
        prompt: str,
        repo: str,
        trigger_message: discord.Message | None = None,
        route_via_orchestrator: bool = True,
        model: str = DEFAULT_AGENT_MODEL,
    ) -> AgentFlowOutcome:
        """Shared agent dispatch for the /agent slash command AND mention/ambient
        triggers (ADR 035).

        ``route_via_orchestrator`` decides whether the ADR 036 chat-vs-agent
        verdict runs at all:

        - True (the mention/ambient path): the bot has to judge whether a message
          aimed at it is a task or conversation, so a ``chat`` verdict returns a
          conversational reply and opens no thread.
        - False (the ``/agent`` slash command): the invoker already made that
          judgement by running the command and choosing a repo, so ALWAYS open a
          thread and start a session. Letting a model overrule an explicit
          invocation is how a deliberate ``/agent`` ends up answered with a chat
          summary and no session. Skipping the call also removes a paid
          OpenRouter round trip from every ``/agent``.

        Does not send origin-channel acknowledgements. Returns an
        :class:`AgentFlowOutcome`.
        """
        if not acl.is_owner(user.id):
            return AgentFlowOutcome(
                chat_reply="Only the configured owner can start or continue agent sessions."
            )
        guild_id = str(channel.guild.id) if channel.guild else ""
        channel_id = str(channel.id)

        if route_via_orchestrator:
            verdict = await self._orchestrator_verdict(
                guild_id, channel_id, user, prompt, repo
            )

            if isinstance(verdict, orchestrator.ChatVerdict):
                reply, files, proposals = await self._orchestrator_chat_reply(
                    channel_id, prompt, verdict, str(user.id)
                )
                return AgentFlowOutcome(
                    chat_reply=reply,
                    generated_files=files,
                    pending_proposal=proposals,
                )
        else:
            verdict = None

        # Agent sessions use the selected repo directly. The orchestrator's
        # conversational verdict above remains unchanged.
        effective_repo = repo

        name = f"agent: {prompt}"[:90]
        try:
            if trigger_message is not None:
                thread = await trigger_message.create_thread(name=name)
            else:
                thread = await channel.create_thread(
                    name=name, type=discord.ChannelType.public_thread
                )
            # Keep this import lazy because agent_sessions.api imports mcp,
            # which reaches chat.api and then chat.bot during startup.
            from agent_sessions.api import start_session_for_thread

            start_result = await start_session_for_thread(
                str(thread.id),
                prompt,
                effective_repo or None,
                model=model,
            )
            if isinstance(start_result, dict) and start_result.get("login_required"):
                await thread.send(_format_codex_login_message(start_result))
                return AgentFlowOutcome(thread=thread)
            # ADR 036: the orchestrator wrote its telemetry row BEFORE this
            # thread existed (it decides whether to open one), so the row's
            # thread_id is null. Backfill it now that the thread id is known, so
            # the routing verdict joins to the agent session it
            # produced (both goose and failopen verdicts carry the row id;
            # ungranted/disabled fail-opens carry None and are skipped).
            brief_id = getattr(verdict, "brief_id", None)
            if brief_id is not None:
                await asyncio.to_thread(
                    orchestrator.link_thread, brief_id, str(thread.id)
                )
        except Exception:
            logger.exception("agent_sessions: failed to start agent session")
            return AgentFlowOutcome()

        try:
            # Echo the full prompt into the thread: the thread title truncates it
            # to ~90 chars, so a long prompt is otherwise lost. Its own message,
            # not the intro (which the progress stream live-edits and would clobber).
            await thread.send(_format_agent_prompt_echo(user, prompt))
        except Exception:
            logger.exception("agent_sessions: failed to post agent prompt echo")
        try:
            await thread.send("🤖 Planning...")
        except Exception:
            logger.exception("agent_sessions: failed to post agent thread intro")
        return AgentFlowOutcome(thread=thread)

    async def _orchestrator_verdict(
        self,
        guild_id: str,
        channel_id: str,
        user: discord.abc.User,
        prompt: str,
        repo: str,
    ) -> "orchestrator.Verdict":
        """Run the ADR 036 orchestrator for one escalation, failing open on any
        error. Short-circuits to FailOpen when the tier is disabled so the
        failopen path does no extra gathering (byte-for-byte today's behaviour).
        """
        if not orchestrator.enabled():
            return orchestrator.FailOpen("orchestrator disabled")
        try:
            allowed = await asyncio.to_thread(
                acl.allowed_scopes, guild_id, user.id, "agent"
            )
            directive = orchestrator.Directive(
                version=await asyncio.to_thread(
                    directives.get_active_version, channel_id
                ),
                text=await asyncio.to_thread(directives.get_active, channel_id),
            )
            channel_context = await asyncio.to_thread(
                summarizer._fetch_agent_reply_context, channel_id
            )
            similar_messages = await self._orchestrator_similar_messages(
                channel_id, prompt
            )
            ctx = orchestrator.RequestContext(
                request=prompt,
                guild_id=guild_id,
                channel_id=channel_id,
                invoker_scope=repo,
                allowed_scopes=frozenset(allowed),
                channel_context=channel_context,
                similar_messages=similar_messages,
                directive=directive,
            )
            return await orchestrator.compile(ctx)
        except Exception:
            logger.exception("orchestrator: verdict failed; failing open")
            return orchestrator.FailOpen("orchestrator raised")

    async def _orchestrator_similar_messages(
        self, channel_id: str, prompt: str
    ) -> list[str]:
        """Contextually similar past messages from THIS channel, via pgvector
        similarity, for orchestrator grounding (ADR 036).

        Channel-scoped, preserving the chat provenance guarantee: nothing
        authored outside the channel enters the context. Returns [] on any
        failure so a retrieval miss degrades the brief's grounding without
        failing the whole orchestrator open.
        """
        try:
            query_embedding = await self.embed_client.embed(prompt)

            def _search() -> list[str]:
                with Session(get_engine()) as session:
                    store = MessageStore(
                        session=session, embed_client=self.embed_client
                    )
                    results = store.search_similar(
                        channel_id=channel_id,
                        query_embedding=query_embedding,
                        limit=5,
                    )
                if not results:
                    return []
                block = format_context_messages(results)
                return [line for line in block.split("\n") if line.strip()]

            return await asyncio.to_thread(_search)
        except Exception:
            logger.warning(
                "orchestrator: similar-message retrieval failed", exc_info=True
            )
            return []

    async def _orchestrator_chat_reply(
        self,
        channel_id: str,
        prompt: str,
        verdict: "orchestrator.ChatVerdict",
        user_id: str,
    ) -> "tuple[str | None, list, list]":
        """Author a chat-verdict reply through the tool-enabled concierge agent.

        A chat verdict means "no microVM session", not "no tools": this runs the
        same PydanticAI agent a direct mention does, so run_code (exact math,
        a matplotlib chart) and the other concierge tools work here too. The
        orchestrator's reply guidance is injected as a system prompt (via
        ChatDeps.orchestrator_guidance) to keep the reply grounded; the raw
        prompt stays ground truth. ``user_id`` is the requester's Discord id,
        threaded in so per-user tools (set_my_style, reminders) and directive
        attribution work, matching the direct-mention path. Returns
        (reply_text, generated_files, pending_proposal); the caller flushes the
        files as attachments and posts the proposals for confirm. Returns
        (None, [], []) on model failure so the caller fails open.
        """
        from chat.agent import ChatDeps

        guidance = _render_reply_guidance(verdict)
        try:
            # The session must stay open for the whole agent run so store-backed
            # tools (search_history, get_user_summary) work; the plain-data lists
            # (generated bytes, proposals) are copied out before it closes.
            with Session(get_engine()) as session:
                deps = ChatDeps(
                    channel_id=channel_id,
                    store=MessageStore(session=session, embed_client=self.embed_client),
                    embed_client=self.embed_client,
                    author_id=user_id,
                    orchestrator_guidance=guidance,
                )
                result = await self.agent.run(prompt, deps=deps)
                files = list(deps.generated_files)
                proposals = list(deps.pending_proposal)
            # Shield the reply from leaked tool-call scaffolding (a small model
            # emitting a run_code call as plain text) and from stray markdown
            # image tags. Always scrub; when a leak is detected, run the bounded
            # model-repair loop and log the occurrence for later eval. Outside
            # the session block: the repair uses its own inference seam, not the
            # store-backed tools. Best-effort: a shield failure falls back to the
            # raw output so a reply still ships.
            try:
                shielded = await repair_leaked_reply(
                    result.output or "",
                    llm_call=summarizer.build_llm_caller(),
                    max_turns=AGENT_REPLY_REPAIR_MAX_TURNS,
                )
                if shielded.leaked:
                    await asyncio.to_thread(
                        reply_repair_log.log_repair, channel_id, user_id, shielded
                    )
                reply_text = shielded.final
            except Exception:
                logger.exception("orchestrator: reply shield failed")
                reply_text = result.output
            return reply_text, files, proposals
        except Exception:
            logger.exception("orchestrator: chat reply generation failed")
            return None, [], []

    async def _flush_directive_proposals(
        self,
        proposals: list,
        channel_id: str,
        author_id: str,
        motivating_message_id: str,
        post,
    ) -> None:
        """Post a directive-change proposal for a human 👍/👎 confirm (ADR 035
        Phase 5): post the summary, stage the inactive row, and seed reactions.

        Shared by the direct-mention path (_stream_response) and the
        orchestrator chat-verdict callers. ``post`` is an async callable taking
        the summary text and returning a message-like object (message.reply or
        interaction.followup.send); ``motivating_message_id`` is the triggering
        message id, or "" where there is none (a slash command).
        """
        for prop in proposals:
            try:
                summary = await post(
                    "Proposed directive for this channel:\n> "
                    + prop["directive"].replace("\n", "\n> ")
                    + "\n\nReact 👍 to apply or 👎 to discard."
                )
                ok, _ = await asyncio.to_thread(
                    directives.propose_update,
                    channel_id,
                    prop["directive"],
                    author_id,
                    motivating_message_id,
                    str(summary.id),
                )
                if ok:
                    await summary.add_reaction("👍")
                    await summary.add_reaction("👎")
                else:
                    await summary.edit(
                        content="That change was rejected (it tried to alter "
                        "tools, permissions, or access)."
                    )
            except Exception:
                logger.exception("directives: failed to post proposal summary")

    async def _maybe_handle_agent_thread_reply(self, message: discord.Message) -> bool:
        """Queue an owner reply for the agent session bound to its thread.

        Looks up the session bound to the message thread, gates continuation on
        the configured owner, and queues a turn. Returns True when the message
        was handled and the caller should skip the chat agent, and False when
        this thread has no bound agent session.
        """
        # Import lazily because agent_sessions.api imports mcp, which reaches
        # chat.api and then chat.bot during startup.
        import agent_sessions.api

        thread_id = str(message.channel.id)
        session_id = await asyncio.to_thread(
            agent_sessions.api.session_id_for_thread, thread_id
        )
        if session_id is None:
            return False

        msg_id = str(message.id)
        # Ignore other bots in the build thread: don't roast them (avoids
        # bot-to-bot loops) and don't let them reach the chat agent.
        if message.author.bot:
            await asyncio.to_thread(self._complete_lock, msg_id)
            return True
        if not acl.is_owner(message.author.id):
            await message.reply(
                "Only the configured owner can continue agent sessions."
            )
            await asyncio.to_thread(self._complete_lock, msg_id)
            return True

        try:
            result = await agent_sessions.api.send_to_thread_session(
                thread_id, message.content
            )
        except Exception:
            logger.exception("agent_sessions: failed to continue session")
            await message.reply("Couldn't send that to the session. Check the logs.")
            await asyncio.to_thread(self._complete_lock, msg_id)
            return True

        if result is None:
            # Lost a race with session teardown; let normal handling take it.
            return False

        if result.get("login_required"):
            await message.channel.send(_format_codex_login_message(result))
            await asyncio.to_thread(self._complete_lock, msg_id)
            return True

        try:
            await message.add_reaction("⏳")
        except discord.HTTPException:
            logger.exception("agent_sessions: failed to react queued on %s", msg_id)
        await asyncio.to_thread(self._complete_lock, msg_id)
        return True

    async def _engage_agent(self, message: discord.Message) -> AgentFlowOutcome | None:
        """A mention/reply/ambient engage: run the /agent flow (repo-less) off
        this message, applying the owner-only agent gate. On a non-owner, an
        EXPLICIT trigger (mention/reply) gets a short refusal; an ambient engage
        stays silent (no spam in a channel nobody asked the bot into). Marks the
        message lock completed either way.

        The gate is duplicated here and in ``start_agent_flow`` on purpose. The
        one in the shared flow is the security chokepoint, covering the slash
        command too; this one exists so an ambient engage can be refused
        SILENTLY, which the shared flow cannot express (it returns a chat_reply
        its callers post).
        """
        user = message.author
        if not acl.is_owner(user.id):
            outcome = AgentFlowOutcome(
                chat_reply="Only the configured owner can start or continue agent sessions."
            )
            if should_respond(message, self.user):
                try:
                    await message.reply(outcome.chat_reply)
                except discord.HTTPException:
                    logger.exception("agent: failed to send owner-only refusal")
            await asyncio.to_thread(self._complete_lock, str(message.id))
            return outcome

        channel = message.channel
        if not isinstance(channel, discord.TextChannel):
            # Ambient/mention only makes sense in a text channel we can thread from.
            await asyncio.to_thread(self._complete_lock, str(message.id))
            return

        # Persist the triggering message to the channel's history. The agent path
        # (unlike _process_message) otherwise never saves it, so a channel whose
        # only activity is agent triggers reads back empty and the run's injected
        # context (ADR 040, built from the parent channel's recent messages) has
        # nothing to show. Best-effort: a save failure must not block the run.
        try:
            with Session(get_engine()) as store_session:
                store = MessageStore(
                    session=store_session, embed_client=self.embed_client
                )
                await store.save_message(
                    discord_message_id=str(message.id),
                    channel_id=str(channel.id),
                    user_id=str(message.author.id),
                    username=message.author.display_name,
                    content=message.content or _extract_embed_text(message),
                    is_bot=message.author.bot,
                )
        except Exception:
            logger.exception("agent: failed to persist trigger message %s", message.id)

        # Ack-first (Task 8): post the ⏳ queue reaction BEFORE start_agent_flow,
        # which runs the ADR 036 orchestrator.compile (route + submit_plan, up to
        # 60s). This hides that latency behind a visible ack so an engaged user
        # sees immediate feedback; the runner then flips ⏳ -> 👀 -> ✅/❌ as the
        # turn runs. Best-effort: a reaction failure must not block the run. (The
        # /agent slash path is already ack-first via interaction.response.defer.)
        try:
            await message.add_reaction(AGENT_REACTION_QUEUED)
        except discord.HTTPException:
            logger.exception("agent: failed to react queued on %s", message.id)

        outcome = await self.start_agent_flow(
            channel, user, message.content, "", trigger_message=message
        )
        if outcome.chat_reply is not None:
            # ADR 036: routed to chat, so reply to the triggering message rather
            # than opening a session thread. Text + any chart go in ONE message
            # (see _deliver_chat_reply).
            try:
                sent = await _deliver_chat_reply(
                    message.reply, outcome.chat_reply, outcome.generated_files
                )
                await self._flush_directive_proposals(
                    outcome.pending_proposal,
                    str(channel.id),
                    str(user.id),
                    str(message.id),
                    message.reply,
                )
                # Link this in-channel reply to the ambient engage decision so a
                # reaction on it joins to the engage exactly (ADR 035 /
                # improve-ambient). The thread-opening path has no single
                # in-channel reply, so it stays null; likewise when the shield
                # delivered nothing (sent is None). Best-effort.
                try:
                    if sent is not None:
                        await asyncio.to_thread(
                            attention_log.set_reply_message,
                            str(channel.id),
                            str(message.id),
                            str(sent.id),
                        )
                except Exception:
                    logger.exception(
                        "attention: failed to record agent reply id for %s",
                        message.id,
                    )
            except discord.HTTPException:
                logger.exception("orchestrator: failed to send chat reply")
        elif outcome.thread is None:
            try:
                await message.reply("Couldn't start that one. Check the logs.")
            except discord.HTTPException:
                logger.exception("agent: failed to send start-failure reply")
        await asyncio.to_thread(self._complete_lock, str(message.id))
        return outcome

    def _complete_lock(self, msg_id: str) -> None:
        """Mark a message lock completed (sync; call via asyncio.to_thread)."""
        with Session(get_engine()) as session:
            store = MessageStore(session=session, embed_client=self.embed_client)
            store.mark_completed(msg_id)

    async def _process_message(
        self,
        message: discord.Message,
        force_respond: bool = False,
        explicit: bool = False,
        directive: str | None = None,
    ) -> None:
        """Process a message that this pod has locked.

        ``force_respond`` skips the ``should_respond`` gate so an ambient
        engage that the depth classify routed to chat (ADR 035 Phase 4) still
        gets a reply even though it's not a mention/reply. ``explicit`` marks an
        ambient engage that is actually a hard summons (a direct @mention or a
        reply to Bosun): it keeps the reply on the live, never-suppressed path
        even though ``force_respond`` routed it here, so the no_reply tool and
        the post-generation send-gate cannot silently eat an answer someone is
        waiting on. (A soft ambient interjection leaves ``explicit`` False and
        stays suppressible.) ``directive`` is the channel directive already
        fetched by ``on_message`` for the pre-gate, threaded through to the
        post-generation send-gate so it is not read from the DB a second time;
        only the ambient (force_respond) path passes it.
        """
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

        if not force_respond and not should_respond(message, self.user):
            # Message stored successfully, mark lock done
            with Session(get_engine()) as session:
                store = MessageStore(session=session, embed_client=self.embed_client)
                store.mark_completed(msg_id)
            return

        try:
            async with message.channel.typing():
                sent, response_text, thinking = await self._stream_response(
                    message,
                    attachments,
                    with_buttons=not force_respond,
                    # An explicit summons is always live (never suppressed),
                    # even on the ambient force_respond path: the mention set
                    # engage=1.0, so a person is waiting on an answer.
                    live=(not force_respond) or explicit,
                    directive=directive,
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

        if sent is None:
            # Ambient reply suppressed: the model had nothing worth saying, so
            # _stream_response stayed silent rather than posting a placeholder.
            # Nothing was posted, so there is no bot message to store or link
            # back to the engage decision; just close out the trigger message.
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

        # Link this reply back to the ambient engage decision (ADR 035 /
        # improve-ambient), so a reaction on the reply joins to the engage
        # exactly rather than by a time window. Only the ambient path
        # (force_respond) has an engage row to attach to; a normal mention/reply
        # has none, so guard on force_respond. Best-effort: never block the
        # reply on a bookkeeping failure.
        if force_respond:
            try:
                await asyncio.to_thread(
                    attention_log.set_reply_message,
                    channel_id,
                    str(message.id),
                    str(sent.id),
                )
            except Exception:
                logger.exception(
                    "attention: failed to record reply id for message %s", msg_id
                )

        with Session(get_engine()) as session:
            store = MessageStore(session=session, embed_client=self.embed_client)
            store.mark_completed(msg_id)

    async def _stream_response(
        self,
        message: discord.Message,
        current_attachments: list[dict] | None = None,
        *,
        with_buttons: bool = True,
        live: bool = True,
        directive: str | None = None,
    ) -> tuple[discord.Message | None, str, str | None]:
        """Build context and stream the PydanticAI agent response.

        When ``live`` is True (a mention/reply the user is waiting on), sends an
        initial Discord reply on the first event, then progressively edits the
        message as thinking/search/text arrives. When ``live`` is False (an
        ambient interjection nobody explicitly asked for), suppresses the
        placeholder and the progressive edits and posts one complete message at
        the end, so the bot reads as chiming in rather than visibly "typing" a
        message it edits in place (the channel's native typing indicator still
        signals composition). Returns (sent_message, response_text,
        thinking_text).

        ``with_buttons`` controls whether the "Show thinking" / fact-check
        action row is attached. Ambient (unprompted) replies pass ``False`` so
        they read as an organic message rather than a bot card (ADR 035).
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
                author_id=str(message.author.id),
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
            # Live search checklist. search_status is keyed by query string so a
            # model-unavailable retry (which replays the same tool call under a
            # fresh id) collapses to one row; id_to_query maps every seen
            # tool_call_id back to its query so a result event can flip the
            # marker from pending to done.
            search_status: dict[str, bool] = {}  # query -> done
            id_to_query: dict[str, str] = {}  # tool_call_id -> query
            response_text = ""
            last_edit_time = 0.0
            had_events = False

            def _search_content() -> str:
                lines = []
                for q, done in search_status.items():
                    marker = "✅" if done else "⏳"
                    lines.append(f"{marker} {q}")
                return "\U0001f50d Searching...\n" + "\n".join(lines)

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
                        if live:
                            await _ensure_sent("\U0001f4ad Thinking...")
                        thinking_parts.append(event.delta.content_delta)
                    elif isinstance(event.delta, TextPartDelta):
                        response_text += event.delta.content_delta
                        if live:
                            await _ensure_sent(response_text)
                            await _edit_if_due(response_text)
                elif isinstance(event, FunctionToolCallEvent):
                    args = event.part.args
                    if isinstance(args, str):
                        try:
                            args = json.loads(args)
                        except (json.JSONDecodeError, TypeError):
                            pass
                    if event.part.tool_name == "run_code" and isinstance(args, dict):
                        # A code blob makes a useless checklist row; show the
                        # first line of the snippet instead of the generic
                        # query extraction below.
                        code = args.get("code", "") or ""
                        first_line = code.splitlines()[0] if code else ""
                        language = args.get("language", "python") or "python"
                        query = f"run {language}: {first_line[:80]}"
                    elif isinstance(args, dict):
                        query = args.get("query", str(args))
                    else:
                        query = str(args)
                    call_id = event.part.tool_call_id
                    if call_id:
                        id_to_query[call_id] = query
                    # Dedupe: a model-unavailable retry replays the turn and
                    # re-emits the same tool call. Showing it once keeps the
                    # checklist honest instead of spamming repeat rows; a later
                    # result event for either id flips the shared row to done.
                    if query not in search_status:
                        search_status[query] = False
                        if live:
                            content = _search_content()
                            await _ensure_sent(content)
                            await _edit_if_due(content, force=True)
                elif isinstance(event, FunctionToolResultEvent):
                    # A search returned: flip its row from \u23f3 to \u2705.
                    done_query = id_to_query.get(event.tool_call_id)
                    if done_query is not None and not search_status.get(
                        done_query, True
                    ):
                        search_status[done_query] = True
                        if live:
                            await _edit_if_due(_search_content(), force=True)

            # The model explicitly declined via the no_reply tool. Honored only
            # on the ambient path where nothing has been posted yet: any text it
            # emitted alongside the call is presumed meta-leakage and discarded.
            # A live reply someone is actively waiting on ignores the flag and
            # falls through to the normal handling below.
            if deps.no_reply_requested and not live and sent is None:
                logger.info(
                    "no_reply tool: suppressing ambient reply in channel %s (%s)",
                    message.channel.id,
                    deps.no_reply_reason or "no reason given",
                )
                await asyncio.to_thread(
                    attention_log.set_withheld_reason,
                    str(message.channel.id),
                    str(message.id),
                    attention_log.WITHHELD_NO_REPLY,
                )
                return None, "", None

            # No real content: no events arrived, or the reply is blank /
            # whitespace / a bare placeholder the model emits when it has nothing
            # to add. For an ambient interjection (not live) nothing has been
            # posted yet, so the right outcome is silence: return no message
            # rather than leaking a literal "(empty)" or an apology nobody asked
            # for (/improve-ambient episodes 168, 170). For a live reply someone
            # is actively waiting on, keep the visible apology instead of going
            # dark.
            if not had_events or _is_empty_reply(response_text):
                if not live and sent is None:
                    await asyncio.to_thread(
                        attention_log.set_withheld_reason,
                        str(message.channel.id),
                        str(message.id),
                        attention_log.WITHHELD_EMPTY_REPLY,
                    )
                    return None, "", None
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

            # Post-generation send-gate (improve-ambient): a second, disconnected
            # classify reads the channel directive + recent interaction + the
            # drafted reply and vetoes an ambient send that would barge in, pile
            # on after a brush-off, or invent facts - failures the pre-gate cannot
            # see because it only had the trigger. Ambient only (nothing posted
            # yet, so it can be silently held); a live reply someone is waiting on
            # is never gated. The directive is threaded in from on_message (which
            # already fetched it for the pre-gate), so there is no second DB read
            # here. Fails open. Skipped entirely when disabled.
            if attention.SEND_GATE_ENABLED and not live and sent is None:
                if not await attention.should_send(
                    directive or "", context, message.content or "", response_text
                ):
                    logger.info(
                        "send-gate: suppressing ambient reply in channel %s",
                        message.channel.id,
                    )
                    await asyncio.to_thread(
                        attention_log.set_withheld_reason,
                        str(message.channel.id),
                        str(message.id),
                        attention_log.WITHHELD_SEND_GATE,
                    )
                    return None, "", None

            # Ensure a reply was sent. AgentRunResultEvent alone never calls
            # _ensure_sent, so sent may still be None here.
            await _ensure_sent(response_text)

            # Final edit with complete response and optional ThinkingView
            thinking_text: str | None = None
            if thinking_parts:
                raw = "".join(thinking_parts).strip()
                if raw:
                    thinking_text = _truncate_thinking(raw)

            if with_buttons:
                await sent.edit(
                    content=response_text,
                    view=BotMessageView(response_text, thinking_text),
                )
            elif live:
                # Streamed reply without buttons: settle the final delta in place.
                await sent.edit(content=response_text)
            # Ambient (not live): the finalizing _ensure_sent above already posted
            # the complete text as one message, so there is nothing left to edit.

            # A directive change proposed this run needs a human 👍/👎 confirm
            # before it applies (ADR 035 Phase 5): post the summary, seed the
            # reactions, and link the row to the summary message id. Guard runs
            # again inside propose_update as a defense-in-depth backstop.
            await self._flush_directive_proposals(
                deps.pending_proposal,
                str(message.channel.id),
                str(message.author.id),
                str(message.id),
                message.reply,
            )

            # run_code may have generated files (e.g. a chart) this run. The
            # tool can't post to Discord itself, so flush them here as a
            # follow-up message to the same channel/thread the reply went to
            # (mirrors the pending_proposal flush above). Guarded end-to-end
            # so an attachment failure never eats the text reply already sent.
            if deps.generated_files:
                try:
                    discord_files = _build_run_code_attachments(deps.generated_files)
                    if discord_files:
                        await message.reply(files=discord_files)
                except Exception:
                    logger.exception(
                        "run_code: failed to flush generated file attachments"
                    )
                finally:
                    deps.generated_files.clear()

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
