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

Deferred to Phase 4: agent (heavyweight session) dispatch, and enforcement of the
household tier's tool subset at dispatch time (``goosecracker.tiers.tier_allows``
is the mapping the dispatch check will consult). The in-monolith chat reply here
runs with the shared chat agent's full toolset.
"""

from __future__ import annotations

import hmac
import logging
import os

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel, Field
from sqlmodel import Session, select

from app.db import get_engine
from chat import attention, attention_log
from chat.models import Message, WhatsappGroup
from chat.whatsapp_outbox import enqueue_message

logger = logging.getLogger(__name__)

# /internal so it stays off the public HTTPRoute (in-cluster only, reached by the
# gateway across the cluster network), like the goosecracker progress sink.
router = APIRouter(prefix="/internal/whatsapp", tags=["whatsapp"])

# The bot's trigger name: a message containing it (case-insensitive) engages even
# in a non-ambient group, mirroring a Discord mention. Configurable; the persona
# is "Bosun" (see chat.agent.build_system_prompt).
_TRIGGER_NAME = os.environ.get("WHATSAPP_TRIGGER_NAME", "bosun").strip().lower()

# Phase 3 defers agent (heavyweight session) escalation. When an engaged message
# is classified as needing the agent, we reply honestly rather than dispatching.
# The flag exists so Phase 4 can flip the seam on without another rollout.
_AGENT_ENABLED = os.environ.get("WHATSAPP_AGENT_ENABLED", "").lower() in (
    "1",
    "true",
    "yes",
)

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


async def _generate_reply(
    session: Session, embed_client, group_jid: str, sender_name: str, text: str
) -> str:
    """Produce the full conversational reply text via the in-monolith chat agent.

    Reuses ``chat.agent.create_agent`` run non-streamed (``result.output``),
    unlike the Discord path which live-streams. Recent group history is loaded
    for context exactly as the Discord concierge does.
    """
    from chat.agent import ChatDeps, create_agent, format_context_messages
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
    agent = create_agent()
    result = await agent.run(prompt, deps=deps)
    output = result.output
    return output if isinstance(output, str) and output else ""


def _compute_directed(
    session: Session, group_jid: str, text: str, quoted_message_id: str | None
) -> bool:
    """A message is directed at the bot if it replies to a bot message or names
    the trigger word."""
    if _TRIGGER_NAME and _TRIGGER_NAME in (text or "").lower():
        return True
    if quoted_message_id and _is_bot_message(session, group_jid, quoted_message_id):
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

    with Session(get_engine()) as session:
        from chat.store import MessageStore

        embed_client = _embed_client()
        store = MessageStore(session=session, embed_client=embed_client)

        # Dedupe on (group_jid, message_id): the gateway delivers at-least-once.
        if not store.acquire_lock(body.message_id, body.group_jid):
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
        attention_log.log_decision(
            body.group_jid,
            body.message_id,
            "engage" if result.engage else "ignore",
            result.confidence,
        )
        if not result.engage:
            return {"status": "ignored"}

        # Depth split, mirroring Discord. Agent (heavyweight session) work is
        # Phase 4; until then it gets an honest one-line reply behind the flag.
        if await attention.needs_agent(adapter):
            if _AGENT_ENABLED:
                # Phase 4 dispatch seam: goosecracker.dispatch.submit() for a
                # wa:<group_jid> session under the household tier. Until it lands,
                # answer honestly rather than silently dropping.
                logger.info(
                    "whatsapp: agent-shaped message in %s; dispatch deferred to Phase 4",
                    body.group_jid,
                )
            reply_text = _AGENT_DEFERRED_REPLY
        else:
            reply_text = await _generate_reply(
                session, embed_client, body.group_jid, body.sender_name, body.text
            )
            if not reply_text:
                reply_text = (
                    "Sorry, I'm having trouble formulating a response right now. "
                    "Please try again in a moment."
                )

        enqueue_message(
            session,
            body.group_jid,
            content=reply_text,
            quoted_message_id=body.message_id,
        )
        session.commit()

        # Store the bot reply so it is available as context and as a reply target
        # for later directedness (a WhatsApp reply to it engages). It has no
        # WhatsApp message id yet (the gateway stamps sent_message_id on send), so
        # it is stored under a synthetic bot-reply id keyed to the trigger.
        await store.save_message(
            discord_message_id=f"wa-bot:{body.message_id}",
            channel_id=body.group_jid,
            user_id="bot",
            username="Bosun",
            content=reply_text,
            is_bot=True,
        )

    return {"status": "replied"}


class _MessageAdapter:
    """Minimal message shape the attention gate reads (only ``.content``)."""

    def __init__(self, content: str) -> None:
        self.content = content
