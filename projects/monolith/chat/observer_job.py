"""Weekly directive-evolution observer (ADR 035 phase 4).

Proposes per-channel behavioural directive updates when a channel shows the SAME
style correction repeated more than once. Runs as the ``chat-observe-directives``
Argo CronWorkflow one-shot (``app/jobs_main.py``, ``chart/values.yaml``
``jobs.cronWorkflows``), never the dead in-process scheduler: the cron schedule
in values.yaml is the sole cadence driver.

Scope and safety (Joe's binding conditions):

- **Ambient-granted channels only.** The observer runs exactly the set of
  channels the attention gate does: those with an ADR 029 ambient grant
  (``feature="ambient"``, ``subject_id=""`` server-wide, ``scope=<channel_id>``).
  Ambient grants are per-channel, so this is a direct enumeration.
- **Sensitivity is deploy-time config**, read from the job env: ``OBSERVER_MIN_EVIDENCE``
  (default 3) is the minimum distinct evidence messages the classifier must cite;
  ``OBSERVER_COOLDOWN_DAYS`` (default 14) suppresses a channel that already saw a
  proposal recently. Cadence is the cron schedule itself.
- **At most one new proposal per channel per run.**

Exchange retrieval (design note): the ``Message`` schema has no reply or mention
edges, so a "bot-directed user message" is approximated as a non-bot message
whose immediately-preceding message in the recency window is a bot message, i.e.
a user reacting to something the bot just said. That is exactly the
style-friction signal (a correction of the bot), needs no bot-id configuration,
and is deterministic. Recency is bounded by a newest-first message-count window.

Concurrency: this follows ``chat.summarizer.conversational_chat_reply`` -- a sync
DB fetch phase (``asyncio.to_thread``, own session), an async LLM classify phase,
then a sync enqueue phase (``asyncio.to_thread``, own session). No ``Session``
ever crosses an ``await`` (semgrep no-sync-session-in-async-def /
no-session-in-to-thread).
"""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timedelta, timezone

from sqlmodel import Session, select

from chat import observer, outbox
from chat.models import ChannelDirective, DiscordFeatureGrant, Message

logger = logging.getLogger("monolith.chat.observer")

_DEFAULT_MIN_EVIDENCE = 3
_DEFAULT_COOLDOWN_DAYS = 14
# Newest-first message-count window scanned per channel for friction. Only
# bot-adjacent user messages become exchanges, so the classifier prompt stays far
# smaller than this cap.
_WINDOW_MESSAGES = 200
# Evidence-snippet budget in the proposal message a human confirms against.
_MAX_EVIDENCE_SNIPPETS = 3
_SNIPPET_CHARS = 120


def _int_env(name: str, default: int) -> int:
    """Read a positive int knob from the job env, falling back to ``default`` on a
    missing, unparseable, or non-positive value: a bad deploy value degrades to
    the default rather than crashing or disabling the run."""
    try:
        value = int(os.environ.get(name, ""))
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


def _granted_channel_ids(session: Session) -> list[str]:
    """Every channel with an ADR 029 ambient grant, across all servers.

    Ambient grants are per-channel (``feature="ambient"``, ``subject_id=""``
    server-wide, ``scope=<channel_id>``), so the observer's scope is exactly the
    attention gate's. Deduped and sorted for a stable iteration order.
    """
    rows = session.exec(
        select(DiscordFeatureGrant).where(DiscordFeatureGrant.feature == "ambient")
    ).all()
    return sorted({r.scope for r in rows if r.subject_id == "" and r.scope})


def _cooldown_active(
    session: Session, channel_id: str, now: datetime, cooldown_days: int
) -> bool:
    """True if the channel saw any directive proposal within the cooldown window.

    A proposal row is any ``ChannelDirective`` with a non-empty
    ``proposal_message_id`` (both the human /agent path and this observer set it;
    a seed or reset does not). Blocking on ANY such row inside the window covers
    both a genuinely-open proposal awaiting 👍/👎 and a just-resolved one: a
    proposal is only applicable for ``directives.PROPOSAL_TTL`` (10 minutes), far
    inside the default 14-day cooldown, so a beyond-cooldown inactive proposal is
    already terminal and correctly stops blocking. This is why the observer needs
    no open-vs-discarded schema flag.
    """
    cutoff = now - timedelta(days=cooldown_days)
    row = session.exec(
        select(ChannelDirective)
        .where(ChannelDirective.channel_id == channel_id)
        .where(ChannelDirective.proposal_message_id != "")
        .where(ChannelDirective.created_at >= cutoff)
        .limit(1)
    ).first()
    return row is not None


def _channel_exchanges(session: Session, channel_id: str) -> list[dict]:
    """Recent bot-directed user messages in a channel, shaped as
    ``find_style_friction`` exchanges (``{"message_id", "author", "text"}``).

    A bot-directed message is a non-bot message immediately following a bot
    message in the newest-first window (see module docstring). ``message_id`` is
    the Discord id so the classifier's cited evidence ids line up with the ids the
    proposal message quotes back and the drain hook stamps as motivating.
    """
    newest_first = list(
        session.exec(
            select(Message)
            .where(Message.channel_id == channel_id)
            .order_by(Message.created_at.desc())
            .limit(_WINDOW_MESSAGES)
        ).all()
    )
    window = list(reversed(newest_first))  # chronological, oldest first
    exchanges: list[dict] = []
    prev_is_bot = False
    for msg in window:
        if not msg.is_bot and prev_is_bot:
            exchanges.append(
                {
                    "message_id": msg.discord_message_id,
                    "author": msg.username,
                    "text": msg.content,
                }
            )
        prev_is_bot = msg.is_bot
    return exchanges


def _gather_candidates(
    now: datetime, cooldown_days: int
) -> list[tuple[str, list[dict]]]:
    """Sync DB phase: ``(channel_id, exchanges)`` for every ambient-granted
    channel that is off cooldown and has at least one bot-directed exchange.

    Opens its own session (runs under ``asyncio.to_thread``, so it must never
    touch the caller's session).
    """
    from core.db import get_engine

    candidates: list[tuple[str, list[dict]]] = []
    with Session(get_engine()) as session:
        for channel_id in _granted_channel_ids(session):
            if _cooldown_active(session, channel_id, now, cooldown_days):
                continue
            exchanges = _channel_exchanges(session, channel_id)
            if exchanges:
                candidates.append((channel_id, exchanges))
    return candidates


def _truncate(text: str, limit: int) -> str:
    text = text.replace("\n", " ").strip()
    return text if len(text) <= limit else text[: limit - 3].rstrip() + "..."


def _proposal_content(
    directive_change: str, evidence_ids: list[str], exchanges: list[dict]
) -> str:
    """Compose the proposal message: the interactive bot path's summary line plus
    up to ``_MAX_EVIDENCE_SNIPPETS`` truncated evidence quotes, so a human
    confirming has the context that motivated the change."""
    by_id = {e["message_id"]: e for e in exchanges}
    blocks = [
        "Proposed directive for this channel:\n> "
        + directive_change.replace("\n", "\n> ")
    ]
    snippets = []
    for mid in evidence_ids[:_MAX_EVIDENCE_SNIPPETS]:
        e = by_id.get(mid)
        if e is not None:
            snippets.append(f"> {e['author']}: {_truncate(e['text'], _SNIPPET_CHARS)}")
    if snippets:
        blocks.append("Based on repeated feedback like:\n" + "\n".join(snippets))
    blocks.append("React 👍 to apply or 👎 to discard.")
    return "\n\n".join(blocks)


def _enqueue_proposal(channel_id: str, finding: dict, exchanges: list[dict]) -> None:
    """Sync enqueue phase: stage exactly ONE directive-proposal outbox row for the
    channel. Opens its own session (``asyncio.to_thread``). The drain's post-hook
    runs the actual ``propose_update`` once it has the posted message id.
    """
    from core.db import get_engine

    evidence_ids = finding["evidence_message_ids"]
    directive_change = finding["directive_change"]
    payload = {
        "channel_id": channel_id,
        "directive_change": directive_change,
        "evidence_message_ids": evidence_ids,
        "motivating_message_id": evidence_ids[0] if evidence_ids else "",
    }
    content = _proposal_content(directive_change, evidence_ids, exchanges)
    with Session(get_engine()) as session:
        outbox.enqueue_message(
            session,
            channel_id,
            content=content,
            kind="directive_proposal",
            payload=payload,
        )
        session.commit()


async def observe_directives_handler(session: Session) -> None:
    """Observe ambient-granted channels for recurring style friction and enqueue
    at most one directive proposal per channel.

    The ``session`` argument (passed by the one-shot CLI wrapper) is unused: all
    DB I/O runs in worker threads via ``asyncio.to_thread`` with their own
    sessions. The Argo cron schedule drives cadence, so there is no next-run hint
    to return.
    """
    min_evidence = _int_env("OBSERVER_MIN_EVIDENCE", _DEFAULT_MIN_EVIDENCE)
    cooldown_days = _int_env("OBSERVER_COOLDOWN_DAYS", _DEFAULT_COOLDOWN_DAYS)
    now = datetime.now(timezone.utc)

    candidates = await asyncio.to_thread(_gather_candidates, now, cooldown_days)
    if not candidates:
        logger.info("chat.observer: no ambient channels with recent exchanges")
        return

    from chat.summarizer import build_llm_caller

    caller = build_llm_caller()
    proposed = 0
    for channel_id, exchanges in candidates:
        finding = await observer.find_style_friction(
            exchanges, caller, min_evidence=min_evidence
        )
        if finding is None:
            continue
        await asyncio.to_thread(_enqueue_proposal, channel_id, finding, exchanges)
        proposed += 1
    logger.info(
        "chat.observer: observed %d channel(s), proposed %d directive change(s)",
        len(candidates),
        proposed,
    )
