"""Ambient interaction-signal analysis core for the directive autopilot
(ADR chat/007, PR 3 of the /improve-ambient program).

This module is the deterministic, sync, session-param core the autopilot job
drives. It mirrors the /improve-ambient skill helper's episode model: an ambient
engage (a ``chat.attention_decision`` row with ``decision='engage'``) enriched
with the reactions and human follow-up that landed shortly after, then scored
into a single fluidity/productivity number. The LLM judgment (``classify_scope``)
is deliberately the ONLY async part and takes its episodes as input, so the
scoring stays deterministic and the SQLite ``create_all`` test fixtures can drive
``score_window`` / ``gather_scope_episodes`` directly with an explicit session.

Reaction attribution matches the skill helper: EXACT on the engage's
``reply_message_id`` when present (reactions land on Bosun's reply), else a
channel + time-after-engage window fallback. Human follow-up is always attributed
by the window (there is no exact "did a human follow up" link).
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta

from sqlmodel import Session, select

from chat.models import AttentionDecision, Message, ReactionEvent

logger = logging.getLogger(__name__)

# Reactions and human follow-up are attributed to an episode by channel plus a
# window after the engage's created_at, mirroring the skill helper's default. An
# ambient reply lands within seconds (or after a slower agent run); humans
# react/reply while the exchange is live. Env-overridable, tunable.
_WINDOW_MINUTES = int(os.environ.get("AMBIENT_WINDOW_MINUTES", "30"))

# Curated starter emoji sets. Stored as the actual unicode characters Discord
# reports for a reaction (str(PartialEmoji)). TUNABLE: widen as real reaction
# data accrues. A trailing variation selector (U+FE0F) is normalised away on
# match, so "heart" reads the same whether Discord sends U+2764 or U+2764 U+FE0F.
# Variation selector Discord may append to a presentation-neutral emoji; stripped
# before matching so U+2764 and U+2764 U+FE0F both read as "heart".
_VARIATION_SELECTOR = "️"

POSITIVE_EMOJI = (
    "\U0001f44d",  # thumbsup
    "❤",  # heart
    "\U0001f389",  # tada
    "\U0001f64c",  # raised_hands
    "\U0001f525",  # fire
    "\U0001f4af",  # 100
    "✅",  # white_check_mark
    "\U0001f44f",  # clap
)
NEGATIVE_EMOJI = (
    "\U0001f44e",  # thumbsdown
    "\U0001f620",  # angry
    "\U0001f621",  # rage
    "\U0001f612",  # unamused
)

_POSITIVE_NORM = {e.replace(_VARIATION_SELECTOR, "") for e in POSITIVE_EMOJI}
_NEGATIVE_NORM = {e.replace(_VARIATION_SELECTOR, "") for e in NEGATIVE_EMOJI}


def _emoji_sign(emoji: str) -> int:
    """+1 for a positive emoji, -1 for a negative one, 0 otherwise. A trailing
    variation selector is stripped before the lookup so presentation variants of
    the same emoji score identically."""
    norm = (emoji or "").replace(_VARIATION_SELECTOR, "")
    if norm in _POSITIVE_NORM:
        return 1
    if norm in _NEGATIVE_NORM:
        return -1
    return 0


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _reaction_valence(session: Session, ad: AttentionDecision) -> int:
    """Net valence of the reactions attributed to one engage episode.

    Each add contributes the emoji's sign (+1/-1/0); each remove contributes the
    negation, so a later removal cancels an earlier signal. EXACT match on the
    engage's ``reply_message_id`` when present, else a channel + window fallback
    (mirrors the skill helper).
    """
    q = select(ReactionEvent).where(ReactionEvent.channel_id == ad.channel_id)
    if ad.reply_message_id:
        q = q.where(ReactionEvent.message_id == ad.reply_message_id)
    else:
        window_end = ad.created_at + timedelta(minutes=_WINDOW_MINUTES)
        q = q.where(ReactionEvent.created_at >= ad.created_at).where(
            ReactionEvent.created_at <= window_end
        )
    valence = 0
    for r in session.exec(q).all():
        sign = _emoji_sign(r.emoji)
        valence += sign if r.action == "add" else -sign
    return valence


def _has_followup(session: Session, ad: AttentionDecision) -> bool:
    """True if any human (non-bot) message other than the trigger landed in the
    same channel within the window after the engage."""
    window_end = ad.created_at + timedelta(minutes=_WINDOW_MINUTES)
    row = session.exec(
        select(Message)
        .where(Message.channel_id == ad.channel_id)
        .where(Message.is_bot == False)  # noqa: E712 - SQL boolean
        .where(Message.created_at > ad.created_at)
        .where(Message.created_at <= window_end)
        .where(Message.discord_message_id != ad.message_id)
        .limit(1)
    ).first()
    return row is not None


def episode_score(session: Session, ad: AttentionDecision) -> dict:
    """Score one engage episode into its components + composite.

    Returns ``{reaction_valence, followup, barged_in, score}``. The composite:

        score = 0.5 * clamp(reaction_valence, -1, 1) + 0.3 * followup
              - 0.2 * barged_in

    where ``followup`` is 1 if a human replied in-window, and ``barged_in`` is 1
    when the bot got no reply AND no positive reaction (it spoke into silence /
    to a thumbs-down). TUNABLE weights.
    """
    valence = _reaction_valence(session, ad)
    followup = 1 if _has_followup(session, ad) else 0
    barged_in = 1 if (followup == 0 and valence <= 0) else 0
    score = 0.5 * _clamp(valence, -1, 1) + 0.3 * followup - 0.2 * barged_in
    return {
        "reaction_valence": valence,
        "followup": followup,
        "barged_in": barged_in,
        "score": score,
    }


def _scope_episodes(
    session: Session,
    scope_kind: str,
    scope_id: str,
    start: datetime,
    end: datetime,
) -> list[AttentionDecision]:
    """Engage attention_decision rows in [start, end] for a scope. channel scope
    filters on ``channel_id``; user scope resolves the trigger author by joining
    ``chat.messages`` on ``discord_message_id = message_id`` and filtering
    ``user_id``."""
    q = (
        select(AttentionDecision)
        .where(AttentionDecision.decision == "engage")
        .where(AttentionDecision.created_at >= start)
        .where(AttentionDecision.created_at <= end)
    )
    if scope_kind == "user":
        q = q.join(
            Message, Message.discord_message_id == AttentionDecision.message_id
        ).where(Message.user_id == scope_id)
    else:
        q = q.where(AttentionDecision.channel_id == scope_id)
    return list(session.exec(q).all())


def score_window(
    session: Session,
    scope_kind: str,
    scope_id: str,
    start: datetime,
    end: datetime,
) -> float | None:
    """Mean episode score over a scope's engage episodes in [start, end], or
    None when there are no episodes (insufficient data: the caller must never
    apply or validate on a None). ``scope_kind`` is 'channel' or 'user'."""
    episodes = _scope_episodes(session, scope_kind, scope_id, start, end)
    if not episodes:
        return None
    total = sum(episode_score(session, ad)["score"] for ad in episodes)
    return total / len(episodes)


def gather_scope_episodes(session: Session, since: datetime) -> dict:
    """Cluster engage episodes since ``since`` by channel and by trigger author,
    each enriched with its reaction/followup signals.

    Returns ``{"channel": {channel_id: [ep, ...]}, "user": {author_id: [ep, ...]}}``.
    Every engage joins its channel cluster; it also joins the author cluster when
    the trigger message resolves to a non-bot author (via ``chat.messages``).
    Each ``ep`` is ``{episode_id, channel_id, author_id, author_name, text,
    reaction_valence, followup, barged_in, score, created_at, reply_message_id}``.
    """
    ads = list(
        session.exec(
            select(AttentionDecision)
            .where(AttentionDecision.decision == "engage")
            .where(AttentionDecision.created_at >= since)
            .order_by(AttentionDecision.created_at)
        ).all()
    )
    by_channel: dict[str, list[dict]] = {}
    by_user: dict[str, list[dict]] = {}
    for ad in ads:
        msg = session.exec(
            select(Message).where(Message.discord_message_id == ad.message_id)
        ).first()
        signals = episode_score(session, ad)
        ep = {
            "episode_id": ad.id,
            "channel_id": ad.channel_id,
            "author_id": msg.user_id if msg is not None else "",
            "author_name": msg.username if msg is not None else "",
            "text": msg.content if msg is not None else "",
            "created_at": ad.created_at,
            "reply_message_id": ad.reply_message_id,
            **signals,
        }
        by_channel.setdefault(ad.channel_id, []).append(ep)
        if msg is not None and not msg.is_bot and msg.user_id:
            by_user.setdefault(msg.user_id, []).append(ep)
    return {"channel": by_channel, "user": by_user}


# --- LLM classification (async; kept out of the sync scoring core) -----------

_CLASSIFY_PROMPT = (
    "You are tuning a Discord bot's behavioural directive for a single "
    "{scope_word}. Below are recent moments where the bot chose to speak, each "
    "with signals about how it landed: a reaction valence (positive means "
    "people reacted well, negative means thumbs-down or annoyance), whether a "
    "human replied afterwards, and whether the bot appears to have spoken into "
    "silence.\n\n"
    "The bot's CURRENT directive for this {scope_word} is:\n"
    "<<<\n{current_directive}\n>>>\n\n"
    "Recent episodes (episode_id | valence | followup | barged_in | text):\n"
    "{episodes}\n\n"
    "Propose a refined tone / attention / interaction-style directive ONLY if "
    "there is a CLEAR, RECURRING, FIXABLE friction across these episodes (for "
    "example: replies too long, wrong tone, speaking when it should stay quiet). "
    "If there is no clear recurring fixable friction, ABSTAIN. Keep any proposal "
    "a BOUNDED REFINEMENT of the current directive (adjust or add a sentence), "
    "never a full rewrite. Describe the change ONLY in terms of tone, attention, "
    "or interaction style. NEVER reference tools, permissions, ACLs, ambient "
    "mode, repos, or access of any kind.\n\n"
    "Cite ONLY episode_ids from the list above as evidence; never invent one.\n\n"
    'Reply with ONLY a JSON object: {{"propose": true|false, "proposed_text": '
    'string, "confidence": number between 0 and 1, "evidence_ids": [number], '
    '"rationale": string}}. When abstaining, set propose=false and confidence=0. '
    "No prose, no markdown."
)


def _format_episodes(episodes: list[dict]) -> str:
    lines = []
    for ep in episodes:
        text = (ep.get("text") or "").replace("\n", " ")[:200]
        lines.append(
            f"{ep['episode_id']} | {ep['reaction_valence']} | "
            f"{ep['followup']} | {ep['barged_in']} | {text}"
        )
    return "\n".join(lines)


def _extract_json(raw: str) -> str:
    s = raw.find("{")
    e = raw.rfind("}")
    return raw[s : e + 1] if s != -1 and e != -1 and e > s else raw


async def classify_scope(
    caller: Callable[[str], Awaitable[str]],
    scope_kind: str,
    scope_id: str,
    episodes: list[dict],
    current_directive: str,
) -> dict:
    """LLM judgment for one scope's episodes. Returns
    ``{proposed_text, confidence, evidence_ids, rationale}``. Fails closed to
    confidence 0 (abstain) when: no episodes, the model declines, an empty or
    missing proposed_text, a cited evidence id that is not one of the input
    episode ids (hallucinated evidence), an out-of-range confidence, or the
    caller/parse raises. Evidence ids are deduped, order preserved, as ints.

    The LLM call is injected (``chat.summarizer.build_llm_caller``) and awaited
    here, deliberately outside the sync scoring core, so the scoring stays
    deterministic under the SQLite fixtures.
    """
    abstain = {
        "proposed_text": "",
        "confidence": 0.0,
        "evidence_ids": [],
        "rationale": "",
    }
    if not episodes:
        return abstain
    try:
        known_ids = {ep["episode_id"] for ep in episodes}
        scope_word = "user" if scope_kind == "user" else "channel"
        prompt = _CLASSIFY_PROMPT.format(
            scope_word=scope_word,
            current_directive=current_directive or "(none set yet)",
            episodes=_format_episodes(episodes),
        )
        raw = await caller(prompt)
        data = json.loads(_extract_json(raw))

        if not data.get("propose"):
            return abstain

        proposed_text = data.get("proposed_text")
        if not isinstance(proposed_text, str) or not proposed_text.strip():
            return abstain

        try:
            confidence = float(data.get("confidence"))
        except (TypeError, ValueError):
            return abstain
        if not 0.0 <= confidence <= 1.0:
            return abstain

        raw_ids = data.get("evidence_ids")
        if not isinstance(raw_ids, list) or not raw_ids:
            return abstain
        evidence_ids: list[int] = []
        for value in raw_ids:
            try:
                evidence_ids.append(int(value))
            except (TypeError, ValueError):
                return abstain
        evidence_ids = list(dict.fromkeys(evidence_ids))
        if not set(evidence_ids).issubset(known_ids):
            return abstain

        rationale = data.get("rationale")
        return {
            "proposed_text": proposed_text.strip(),
            "confidence": confidence,
            "evidence_ids": evidence_ids,
            "rationale": rationale if isinstance(rationale, str) else "",
        }
    except Exception:
        logger.exception(
            "ambient_analysis: classify failed for %s %s", scope_kind, scope_id
        )
        return abstain
