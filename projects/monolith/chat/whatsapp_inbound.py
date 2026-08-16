"""Inbound WhatsApp endpoint: the bot starts talking (ADR 039, spec sections 2/4).

The Go gateway forwards every allow-listed group message here. This router
mirrors the Discord ``on_message`` flow, minus Discord's live streaming: it
dedupes, stores for context, runs the shared attention gate, and (when engaged)
either enqueues an honest "agent not wired up yet" reply for agent-shaped work
(Phase 3 defers agent escalation) or generates a full conversational reply and
enqueues it as a single ``chat.whatsapp_outbox`` message row. The single-replica
gateway drains that row and sends it (there is no live edit path here).

Reuse seams:
- Dedupe/context/history: ``chat.store.MessageStore`` keyed on the group JID as
  the channel id and the WhatsApp message id as the message id (both are just
  unique strings). No ``provider`` column is added to Message: WhatsApp group
  JIDs (``...@g.us``) never collide with Discord numeric channel ids, so the
  channel_id namespace already separates the two providers.
- Attention: ``chat.attention.evaluate``/``needs_agent``. We supply WhatsApp
  "directedness" ourselves (reply-to-a-bot-message or trigger-name match) via the
  ``directed`` seam and pass ``bot_user=None`` so Discord's mention parsing is
  never touched.
- Reply text: the in-monolith chat agent (``chat.agent.create_agent``) run
  non-streamed; ``result.output`` is the full text.

Agent dispatch is unavailable on WhatsApp. Agent-shaped work receives an honest
unavailable reply. The in-monolith chat reply still runs with the shared chat
agent's full toolset.
"""

from __future__ import annotations

import asyncio
import hmac
import logging
import os

from fastapi import APIRouter, BackgroundTasks, Header, HTTPException
from pydantic import BaseModel, Field
from sqlmodel import Session, select

from core.db import get_engine
from chat import attention, attention_log, whatsapp_capabilities
from chat.models import Message, ReactionEvent, WhatsappGroup, WhatsappOutbox
from chat.whatsapp_outbox import enqueue_media, enqueue_message, enqueue_reaction

logger = logging.getLogger(__name__)

# Cap how many run_code-generated images ride one reply, and the per-image byte
# size (chart PNGs are tens of KB; this guards a runaway sandbox output from
# bloating an outbox row). WhatsApp itself accepts far larger, but the household
# reply never needs it.
_MEDIA_MAX_FILES = 4
_MEDIA_MAX_BYTES = 8 * 1024 * 1024
# Filename-extension -> mime for the image types run_code emits.
_MEDIA_MIME_BY_EXT = {
    "png": "image/png",
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "gif": "image/gif",
    "webp": "image/webp",
}

# /internal so it stays off the public HTTPRoute (in-cluster only, reached by the
# gateway across the cluster network).
router = APIRouter(prefix="/internal/whatsapp", tags=["whatsapp"])

# The bot's trigger name: a message containing it (case-insensitive) engages even
# in a non-ambient group, mirroring a Discord mention. Configurable; the persona
# is "Bosun" (see chat.agent.build_system_prompt).
_TRIGGER_NAME = os.environ.get("WHATSAPP_TRIGGER_NAME", "bosun").strip().lower()

_AGENT_DEFERRED_REPLY = "I can chat, but running full tasks here is not wired up yet."

# Household default directive, used when a group's directive_seed is empty. Shapes
# tone only; it never grants tools or access (that is the tier ACL's job).
_DEFAULT_HOUSEHOLD_DIRECTIVE = (
    "You are a friendly, concise assistant in a small household WhatsApp group. "
    "Help with everyday questions, plans, scheduling, and logging what the group "
    "did. Match the register: a casual message gets a short casual reply. Be "
    "direct and warm, no filler."
)


class InboundMessage(BaseModel):
    """The gateway's forwarded message (spec section 2)."""

    group_jid: str = Field(min_length=1, max_length=128)
    sender_jid: str = Field(min_length=1, max_length=128)
    sender_name: str = Field(default="", max_length=256)
    message_id: str = Field(min_length=1, max_length=128)
    text: str = Field(default="", max_length=8192)
    quoted_message_id: str | None = Field(default=None, max_length=128)
    timestamp: str | None = Field(default=None, max_length=64)


class ReactionInbound(BaseModel):
    """A human reaction on one of Bosun's messages, forwarded by the gateway.

    The gateway only forwards reactions whose target was a bot-sent message; an
    empty ``emoji`` is WhatsApp's representation of a removed reaction (a user has
    at most one reaction per message).
    """

    group_jid: str = Field(min_length=1, max_length=128)
    reactor_jid: str = Field(min_length=1, max_length=128)
    target_message_id: str = Field(min_length=1, max_length=128)
    emoji: str = Field(default="", max_length=64)
    timestamp: str | None = Field(default=None, max_length=64)


def _require_bearer(authorization: str | None) -> None:
    """Fail closed unless the Authorization header carries the configured token.

    The token is 1Password-managed and injected as WHATSAPP_INBOUND_TOKEN into
    both this validator (the monolith) and the gateway (the caller). An unset
    token denies every request rather than accepting an empty bearer.
    """
    expected = os.environ.get("WHATSAPP_INBOUND_TOKEN", "")
    if not expected:
        raise HTTPException(status_code=401, detail="inbound token not configured")
    prefix = "Bearer "
    if not authorization or not authorization.startswith(prefix):
        raise HTTPException(status_code=401, detail="missing bearer token")
    presented = authorization[len(prefix) :]
    if not hmac.compare_digest(presented, expected):
        raise HTTPException(status_code=401, detail="invalid bearer token")


def _lookup_enabled_group(group_jid: str) -> WhatsappGroup | None:
    """Return the registry row iff the group exists and is enabled (spec 6)."""
    with Session(get_engine()) as session:
        group = session.get(WhatsappGroup, group_jid)
    if group is None or not group.enabled:
        return None
    return group


def _is_bot_message(session: Session, group_jid: str, message_id: str) -> bool:
    """Whether ``message_id`` in this group is one of the bot's own messages.

    A reply to a bot message is a directedness signal, so we resolve the quoted
    id against stored bot messages (the outbox stamps the id we stored the reply
    under).
    """
    row = session.exec(
        select(Message).where(
            Message.channel_id == group_jid,
            Message.discord_message_id == message_id,
            Message.is_bot == True,  # noqa: E712
        )
    ).first()
    return row is not None


def _is_bot_sent_message(session: Session, group_jid: str, message_id: str) -> bool:
    """Whether ``message_id`` is a message the bot actually sent to this group.

    The gateway stamps the real WhatsApp id it sent a row under on that
    ``chat.whatsapp_outbox`` row's ``sent_message_id``. A real WhatsApp reply to a
    bot message quotes that real id, which never lands in the messages table (bot
    replies are stored there under a synthetic ``wa-bot:`` id), so the outbox is
    the authoritative record of what the bot sent and the only place the quoted id
    can match. Only message/edit rows mint an addressable id worth quoting.
    """
    row = session.exec(
        select(WhatsappOutbox).where(
            WhatsappOutbox.group_jid == group_jid,
            WhatsappOutbox.sent_message_id == message_id,
            WhatsappOutbox.kind.in_(("message", "edit")),  # type: ignore[attr-defined]
        )
    ).first()
    return row is not None


async def _generate_reply(
    session: Session, embed_client, group_jid: str, sender_name: str, text: str
) -> str:
    """Produce the full conversational reply text via the in-monolith chat agent.

    Reuses ``chat.agent.create_agent`` run non-streamed (``result.output``),
    unlike the Discord path which live-streams. Recent group history is loaded
    for context exactly as the Discord concierge does.
    """
    from chat.agent import ChatDeps, create_household_agent, format_context_messages
    from chat.store import MessageStore

    store = MessageStore(session=session, embed_client=embed_client)
    recent = store.get_recent(group_jid, limit=20)
    context = "Recent conversation:\n" + format_context_messages(recent)

    deps = ChatDeps(
        channel_id=group_jid,
        store=store,
        embed_client=embed_client,
        author_id=group_jid,
    )
    prompt = f"{context}\n\nCurrent message from {sender_name or 'someone'}: {text}"
    # Household content is authored by DeepSeek V4 Flash on OpenRouter, not the
    # in-cluster Qwen the Discord concierge uses (ADR 039, amended).
    agent = create_household_agent()
    result = await agent.run(prompt, deps=deps)
    # Deliver any images run_code generated (charts, etc.) as inline media, the
    # same files the Discord path attaches. Best-effort: a media failure must not
    # drop the text reply.
    await _enqueue_generated_media(group_jid, getattr(deps, "generated_files", []))
    output = result.output
    return output if isinstance(output, str) and output else ""


def _enqueue_media_sync(group_jid: str, files: list) -> int:
    """Enqueue up to _MEDIA_MAX_FILES run_code images as media outbox rows.

    ``files`` are (filename, bytes) tuples (chat.agent.ChatDeps.generated_files).
    Returns the count enqueued. Sync (opens its own Session); call via to_thread.
    """
    sent = 0
    with Session(get_engine()) as session:
        for filename, data in files[:_MEDIA_MAX_FILES]:
            if not data or len(data) > _MEDIA_MAX_BYTES:
                continue
            ext = str(filename).rsplit(".", 1)[-1].lower() if filename else ""
            mime = _MEDIA_MIME_BY_EXT.get(ext)
            if mime is None:
                # Not an image type we know how to send; skip (never guess a mime).
                continue
            enqueue_media(session, group_jid, data=data, mime=mime)
            sent += 1
        if sent:
            session.commit()
    return sent


async def _enqueue_generated_media(group_jid: str, files: list) -> None:
    """Enqueue run_code-generated images to the group. Best-effort."""
    if not files:
        return
    try:
        n = await asyncio.to_thread(_enqueue_media_sync, group_jid, list(files))
        if n:
            logger.info("whatsapp: enqueued %d media file(s) for %s", n, group_jid)
    except Exception:
        logger.exception(
            "whatsapp: failed to enqueue generated media for %s", group_jid
        )


def _compute_directed(
    session: Session, group_jid: str, text: str, quoted_message_id: str | None
) -> bool:
    """A message is directed at the bot if it replies to a bot message or names
    the trigger word."""
    if _TRIGGER_NAME and _TRIGGER_NAME in (text or "").lower():
        return True
    if quoted_message_id and (
        # The authoritative check: a reply to the real id the bot sent under.
        _is_bot_sent_message(session, group_jid, quoted_message_id)
        # Fallback: a bot message stored in the messages table (synthetic id).
        or _is_bot_message(session, group_jid, quoted_message_id)
    ):
        return True
    return False


def _embed_client():
    """Lazily build the shared embedding client (reads EMBEDDING_URL from env)."""
    from shared.embedding import EmbeddingClient

    global _EMBED_CLIENT
    if _EMBED_CLIENT is None:
        _EMBED_CLIENT = EmbeddingClient()
    return _EMBED_CLIENT


_EMBED_CLIENT = None


@router.post("/inbound")
async def inbound(
    body: InboundMessage,
    background_tasks: BackgroundTasks,
    authorization: str | None = Header(default=None),
) -> dict:
    """Authenticate, dedupe, gate attention, and reply (spec sections 2/4).

    Always returns 200 for accepted-then-dropped outcomes (unknown/disabled
    group, duplicate delivery, attention ignore) so the at-least-once gateway
    does not retry a message the monolith deliberately did not act on. Only auth
    failures are non-2xx.
    """
    _require_bearer(authorization)

    group = _lookup_enabled_group(body.group_jid)
    if group is None:
        # Defense in depth: the gateway already allow-lists, but a disabled or
        # unknown group is dropped without processing.
        return {"status": "dropped"}

    embed_client = _embed_client()

    # Read-side: dedupe, store for context, and gate attention in one session,
    # then close it. The session-keyed agent helpers below open their own
    # sessions (steering, dispatch, checklist), so they must not nest inside an
    # open one (SQLite StaticPool in tests shares a single connection).
    with Session(get_engine()) as session:
        from chat.store import MessageStore

        store = MessageStore(session=session, embed_client=embed_client)

        # Dedupe on (group_jid, message_id): the gateway delivers at-least-once.
        # acquire_lock is a sync DB call; offload it so it does not block the loop.
        if not await asyncio.to_thread(
            store.acquire_lock, body.message_id, body.group_jid
        ):
            return {"status": "duplicate"}
        session.commit()

        # Store the inbound message for context (save_message commits internally).
        await store.save_message(
            discord_message_id=body.message_id,
            channel_id=body.group_jid,
            user_id=body.sender_jid,
            username=body.sender_name or body.sender_jid,
            content=body.text,
            is_bot=False,
        )

        directed = _compute_directed(
            session, body.group_jid, body.text, body.quoted_message_id
        )
        directive = group.directive_seed or _DEFAULT_HOUSEHOLD_DIRECTIVE
        adapter = _MessageAdapter(body.text)
        result = await attention.evaluate(
            adapter,
            directive,
            bot_user=None,
            is_ambient=group.ambient,
            directed=directed,
        )
        engage = result.engage
        confidence = result.confidence
        is_agent = await attention.needs_agent(adapter) if engage else False

    # Log the attention decision off the loop and in its own session scope. It runs
    # after the outer session above has closed (not nested inside it): log_decision
    # opens its own Session, and on SQLite's shared connection a nested open session
    # inside the outer transaction would deadlock. Mirrors the Discord path.
    await asyncio.to_thread(
        attention_log.log_decision,
        body.group_jid,
        body.message_id,
        "engage" if engage else "ignore",
        confidence,
    )
    if not engage:
        return {"status": "ignored"}

    # Household capabilities (spec section 5): a record/schedule/reminder intent,
    # or a follow-up resolving a pending confirmation/clarification, is handled
    # here in the monolith under the household tier, before the generic depth
    # split. handle_capability returns a {status, reply} to enqueue, or None to
    # fall through to the normal chat/agent path.
    handled = await whatsapp_capabilities.handle_capability(body)
    if handled is not None:
        await _enqueue_bot_reply(embed_client, body, handled["reply"])
        return {"status": handled["status"]}

    # Depth split, mirroring Discord. Agent work is unavailable on WhatsApp, so
    # return an honest one-liner instead of dispatching a session.
    if is_agent:
        await _enqueue_bot_reply(embed_client, body, _AGENT_DEFERRED_REPLY)
        return {"status": "replied"}

    # Chat path: acknowledge fast, then author the reply OFF the request path.
    # Generating a reply is a full model round-trip (seconds to minutes); doing it
    # inline would hold the gateway's per-group forward worker open the whole time,
    # serializing the group's other messages behind it. Instead drop an instant
    # reaction so the group sees the bot is working, and hand the reply to a
    # background task (FastAPI sends the 200 first, freeing the forward worker).
    await asyncio.to_thread(_enqueue_ack_reaction, body)
    background_tasks.add_task(_deliver_chat_reply, embed_client, body)
    return {"status": "accepted"}


def _enqueue_ack_reaction(body: InboundMessage, remove: bool = False) -> None:
    """Enqueue (or clear) the ⏳ working reaction on the triggering message."""
    with Session(get_engine()) as session:
        enqueue_reaction(
            session,
            body.group_jid,
            body.message_id,
            body.sender_jid,
            "⏳",
            remove=remove,
        )
        session.commit()


async def _deliver_chat_reply(embed_client, body: InboundMessage) -> None:
    """Author the conversational reply and enqueue it, then clear the ⏳ reaction.

    Runs as a background task after the inbound 200, so the model round-trip never
    blocks the gateway. Best-effort: any failure still clears the reaction and, on
    a failed generation, enqueues an honest fallback so the group is never left on
    a bare ⏳.
    """
    try:
        with Session(get_engine()) as session:
            reply_text = await _generate_reply(
                session, embed_client, body.group_jid, body.sender_name, body.text
            )
        if not reply_text:
            reply_text = (
                "Sorry, I'm having trouble formulating a response right now. "
                "Please try again in a moment."
            )
        await _enqueue_bot_reply(embed_client, body, reply_text)
    except Exception:
        logger.exception("whatsapp: chat reply delivery failed for %s", body.group_jid)
    finally:
        try:
            await asyncio.to_thread(_enqueue_ack_reaction, body, True)
        except Exception:
            logger.exception(
                "whatsapp: failed to clear ack reaction for %s", body.group_jid
            )


async def _enqueue_bot_reply(
    embed_client, body: InboundMessage, reply_text: str
) -> None:
    """Enqueue a bot reply to the group and store it for context.

    Opens its own session (the caller no longer holds one). The reply quotes the
    triggering message. The stored copy is a reply target for later directedness
    (a WhatsApp reply to it engages); it has no WhatsApp message id yet (the
    gateway stamps sent_message_id on send), so it is stored under a synthetic
    bot-reply id keyed to the trigger.
    """
    from chat.store import MessageStore

    with Session(get_engine()) as session:
        enqueue_message(
            session,
            body.group_jid,
            content=reply_text,
            quoted_message_id=body.message_id,
        )
        session.commit()
        store = MessageStore(session=session, embed_client=embed_client)
        await store.save_message(
            discord_message_id=f"wa-bot:{body.message_id}",
            channel_id=body.group_jid,
            user_id="bot",
            username="Bosun",
            content=reply_text,
            is_bot=True,
        )


@router.post("/reaction")
async def reaction(
    body: ReactionInbound,
    authorization: str | None = Header(default=None),
) -> dict:
    """Persist a human reaction on one of Bosun's WhatsApp messages as an
    /improve-ambient ground-truth signal (mirrors the Discord reaction path).

    The gateway forwards only reactions whose target was a bot-sent message, but
    we re-verify here against the outbox (defense in depth): the reacted id must
    match a message/edit row the bot actually sent to this group. An empty emoji
    is WhatsApp's removed-reaction representation, stored as an ``action='remove'``
    row that cancels the reactor's earlier add.

    Always 200 for accepted-then-dropped outcomes (unknown group, non-bot target,
    a removal with no prior add) so the at-least-once gateway does not retry a
    reaction the monolith deliberately did not persist. Only auth failures are
    non-2xx.
    """
    _require_bearer(authorization)

    group = _lookup_enabled_group(body.group_jid)
    if group is None:
        return {"status": "dropped"}

    persisted = await asyncio.to_thread(
        _record_whatsapp_reaction,
        body.group_jid,
        body.target_message_id,
        body.reactor_jid,
        body.emoji,
    )
    return {"status": "recorded" if persisted else "dropped"}


def _current_reaction(
    session: Session, group_jid: str, target_message_id: str, reactor_jid: str
) -> str | None:
    """The reactor's current active reaction emoji on a message, or None.

    WhatsApp allows one reaction per user per message: reacting again replaces the
    previous emoji, an empty reaction removes it. Replaying the reactor's stored
    add/remove rows in order therefore yields exactly one current emoji (or none),
    which the caller uses to model replace/remove without double-counting under
    the gateway's at-least-once delivery.
    """
    # Order by (created_at, id): a replace records its cancel + add in one commit,
    # so the two rows can share a timestamp; the autoincrement id breaks the tie in
    # insertion order (on both SQLite and Postgres) so the replay never inverts.
    rows = session.exec(
        select(ReactionEvent)
        .where(
            ReactionEvent.channel_id == group_jid,
            ReactionEvent.message_id == target_message_id,
            ReactionEvent.reactor_id == reactor_jid,
        )
        .order_by(ReactionEvent.created_at, ReactionEvent.id)
    ).all()
    active: str | None = None
    for r in rows:
        active = r.emoji if r.action == "add" else None
    return active


def _record_whatsapp_reaction(
    group_jid: str,
    target_message_id: str,
    reactor_jid: str,
    emoji: str,
) -> bool:
    """Persist one WhatsApp reaction on a bot message; return True if a row landed.

    Sync (opens its own Session); call via ``asyncio.to_thread``. Mirrors
    ``chat.bot._record_reaction`` adapted to WhatsApp's single-reaction-per-message
    model:

    - Bot-target check is against the outbox (the reacted id is the REAL WhatsApp
      id the bot sent under, on ``whatsapp_outbox.sent_message_id``), not the
      synthetic ``wa-bot:`` id the messages table stores.
    - An empty emoji is a removal: cancel the reactor's current reaction (a remove
      row carrying that emoji, so ``_reaction_valence`` negates the matching sign).
      A removal with nothing active is a no-op, which also absorbs a replayed
      removal.
    - A non-empty emoji that matches the current reaction is a replay and is
      dropped; one that differs is a replace: cancel the old (remove) then add the
      new, so a user changing 👍 to ❤️ nets to just ❤️.

    Correctness of the replay depends on the gateway delivering a group's
    reactions in order (chat.whatsapp forward.go blocks a group's worker until
    each POST gets a 2xx, so an earlier event never lands after a later one). If
    that per-group ordering is ever relaxed, this replace/replay logic would need
    an explicit sequence number instead.
    """
    with Session(get_engine()) as session:
        # Only reactions on a message the bot actually sent are a signal about
        # Bosun's reply. Reuses the outbox lookup the directedness check uses.
        if not _is_bot_sent_message(session, group_jid, target_message_id):
            return False

        current = _current_reaction(session, group_jid, target_message_id, reactor_jid)
        remove = emoji == ""

        def _add(row_emoji: str, action: str) -> None:
            session.add(
                ReactionEvent(
                    channel_id=group_jid,
                    message_id=target_message_id,
                    target_is_bot=True,
                    emoji=row_emoji,
                    reactor_id=reactor_jid,
                    action=action,
                )
            )

        if remove:
            if current is None:
                return False
            _add(current, "remove")
        elif current == emoji:
            # Same reaction already active: a replayed delivery, nothing to do.
            return False
        else:
            if current is not None:
                # Replace: cancel the prior reaction before recording the new one.
                _add(current, "remove")
            _add(emoji, "add")

        session.commit()
        return True


class _MessageAdapter:
    """Minimal message shape the attention gate reads (only ``.content``)."""

    def __init__(self, content: str) -> None:
        self.content = content
