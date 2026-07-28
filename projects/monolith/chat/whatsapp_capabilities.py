"""Household capability routing for the WhatsApp inbound path (ADR 039 spec
section 5): record-to-knowledge (confirm-then-capture), calendar scheduling
(clarify-once, with a draft fallback), and reminders. These execute in the
monolith under the ``household`` tier; the gateway never calls tools.

The inbound handler calls :func:`handle_capability` for every engaged, non-steered
message, before the generic depth/chat split. It returns a ``{"status", "reply"}``
dict when it handled the message (the caller enqueues the reply), or ``None`` to
fall through to the normal chat/agent path.

Conversational state is a single row per group in ``chat.whatsapp_pending_action``
(one pending action per group): a ``record`` awaiting an affirmative confirmation
(the knowledge-capture consent boundary, spec 5a and ADR 039 security), or a
``calendar``/``reminder`` awaiting one clarifying answer (clarify-once). The next
engaged message resolves or abandons it.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from sqlmodel import Session

from core.db import get_engine
from chat import whatsapp_calendar
from chat.models import (
    WhatsappCalendarDraft,
    WhatsappPendingAction,
    WhatsappReminder,
)
from chat.whatsapp_intents import classify_intent
from chat.whatsapp_timeparse import parse_datetime

logger = logging.getLogger(__name__)

# Local time zone the household reasons in (relative words, reminder due times,
# digest quiet hours). Not a secret; a plain value so a tz change is a values edit.
_TZ_NAME = os.environ.get("WHATSAPP_TZ", "America/Vancouver")

_AFFIRMATIVE = {
    "yes",
    "yeah",
    "yep",
    "yup",
    "sure",
    "ok",
    "okay",
    "confirm",
    "confirmed",
    "y",
    "save",
    "save it",
    "record it",
    "do it",
    "go ahead",
    "sounds good",
    "please do",
    "\U0001f44d",  # thumbs up
}
_NEGATIVE = {
    "no",
    "nope",
    "nah",
    "cancel",
    "skip",
    "stop",
    "n",
    "don't",
    "dont",
    "no thanks",
    "leave it",
}


def _tz() -> ZoneInfo:
    return ZoneInfo(_TZ_NAME)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _fmt(when: datetime) -> str:
    """Human-readable local rendering of a UTC/aware datetime (24h, tz-local)."""
    return when.astimezone(_tz()).strftime("%a %d %b, %H:%M")


def _is_affirmative(text: str) -> bool:
    t = text.strip().lower().rstrip("!.")
    return t in _AFFIRMATIVE or (bool(t) and t.split()[0] in _AFFIRMATIVE)


def _is_negative(text: str) -> bool:
    t = text.strip().lower().rstrip("!.")
    return t in _NEGATIVE or (bool(t) and t.split()[0] in _NEGATIVE)


# --- text cleanup helpers ---------------------------------------------------

_TIME_TOKENS = re.compile(
    r"\b\d{1,2}:\d{2}\s*(am|pm)?\b|\b\d{1,2}\s*(am|pm)\b|\bat\s+\d{1,2}\b"
    r"|\b(mon|tue|tues|wed|thu|thur|thurs|fri|sat|sun"
    r"|monday|tuesday|wednesday|thursday|friday|saturday|sunday"
    r"|today|tomorrow|tonight|noon|midnight|midday|morning|afternoon|evening"
    r"|next|this)\b",
    re.IGNORECASE,
)


def _strip_time_words(text: str) -> str:
    """Remove date/time tokens (and a dangling connective) from a phrase."""
    t = _TIME_TOKENS.sub("", text)
    t = re.sub(r"\b(on|at|for)\b\s*$", "", t.strip(), flags=re.IGNORECASE)
    t = re.sub(r"\s+", " ", t).strip(" ,.-")
    return t


def _event_title(text: str) -> str:
    """Best-effort event title: drop the leading scheduling verb and time words."""
    t = _strip_time_words(text)
    t = re.sub(
        r"^(add|schedule|book|put|plan|set up|create|new)\s+",
        "",
        t,
        flags=re.IGNORECASE,
    )
    return t.strip(" ,.-") or "event"


def _attendees(text: str) -> str | None:
    m = re.search(
        r"\bwith\s+([A-Za-z][A-Za-z' ]*?)"
        r"(?=\s+(?:on|at|next|this|tomorrow|tonight|today"
        r"|mon|tue|wed|thu|fri|sat|sun|\d)|$)",
        text,
        re.IGNORECASE,
    )
    return m.group(1).strip() if m else None


def _reminder_body(text: str) -> str:
    """The thing to be reminded of, with the reminder verb and time words removed."""
    t = re.sub(
        r"^(remind\s+(us|me|everyone)?\s*(to|about|that)?"
        r"|reminder\s*(to|:)?|don'?t\s+forget\s+(to)?|remember\s+to)\s+",
        "",
        text.strip(),
        flags=re.IGNORECASE,
    )
    t = _strip_time_words(t)
    return t.strip(" ,.-") or text.strip()


# --- pending-state DB helpers (sync; call via asyncio.to_thread) ------------


def _get_pending(group_jid: str) -> WhatsappPendingAction | None:
    with Session(get_engine()) as session:
        return session.get(WhatsappPendingAction, group_jid)


def _set_pending(
    group_jid: str,
    kind: str,
    *,
    summary: str | None = None,
    payload: dict | None = None,
    created_by: str | None = None,
) -> None:
    with Session(get_engine()) as session:
        row = session.get(WhatsappPendingAction, group_jid)
        if row is None:
            row = WhatsappPendingAction(group_jid=group_jid, kind=kind)
        row.kind = kind
        row.summary = summary
        row.payload = payload
        row.created_by = created_by
        row.created_at = _now()
        session.add(row)
        session.commit()


def _clear_pending(group_jid: str) -> None:
    with Session(get_engine()) as session:
        row = session.get(WhatsappPendingAction, group_jid)
        if row is not None:
            session.delete(row)
            session.commit()


# --- capability writes (sync; call via asyncio.to_thread) -------------------


def _capture_record(
    summary: str, *, group_jid: str, sender_jid: str, sender_name: str
) -> None:
    """Write a knowledge raw for a confirmed record, attributed to group+author.

    Imports ``ingest_raw`` lazily so the capabilities module stays importable
    without the knowledge/S3 stack at load time. Provenance carries the group JID
    and the author (JID + push name) so a captured note is traceable to who said
    it in which group.
    """
    from knowledge.api import ingest_raw

    with Session(get_engine()) as session:
        ingest_raw(
            session,
            content=summary,
            source="whatsapp",
            original_url=f"whatsapp:{group_jid}",
            extra={
                "group_jid": group_jid,
                "sender_jid": sender_jid,
                "sender_name": sender_name,
            },
        )


def _insert_reminder(
    group_jid: str, text: str, due_at: datetime, created_by: str
) -> None:
    with Session(get_engine()) as session:
        session.add(
            WhatsappReminder(
                group_jid=group_jid,
                text=text,
                due_at=due_at.astimezone(timezone.utc),
                created_by=created_by,
            )
        )
        session.commit()


def _insert_calendar_draft(
    group_jid: str,
    title: str,
    start_at: datetime,
    attendees: str | None,
    created_by: str,
) -> None:
    with Session(get_engine()) as session:
        session.add(
            WhatsappCalendarDraft(
                group_jid=group_jid,
                title=title,
                start_at=start_at.astimezone(timezone.utc),
                attendees=attendees,
                created_by=created_by,
            )
        )
        session.commit()


# --- orchestrator -----------------------------------------------------------


async def handle_capability(body) -> dict | None:
    """Route an engaged household message to a capability, or return None.

    ``body`` is the inbound ``InboundMessage``. Resolves a pending action first
    (a confirmation or a clarification), else classifies the message's intent and
    dispatches record/schedule/reminder. Returns ``{"status", "reply"}`` when
    handled; ``None`` to fall through to the normal chat/agent path.
    """
    pending = await asyncio.to_thread(_get_pending, body.group_jid)
    if pending is not None:
        return await _resolve_pending(pending, body)

    intent = classify_intent(body.text)
    if intent == "record":
        return await _start_record(body)
    if intent == "reminder":
        return await _handle_reminder(body, body.text)
    if intent == "schedule":
        return await _handle_schedule(body, body.text)
    return None


# --- record (confirm-then-capture) ------------------------------------------


def _record_summary(text: str) -> str:
    """Strip a leading record verb, leaving the thing to store."""
    t = re.sub(
        r"^(record(:|\s+that|\s+this)?|log(:|\s+that)?|note\s+(that|down)"
        r"|make\s+a\s+note\s+(of|that)?|for\s+the\s+record|remember\s+that"
        r"|save\s+this)\s*",
        "",
        text.strip(),
        flags=re.IGNORECASE,
    )
    return t.strip(" ,.-:") or text.strip()


async def _start_record(body) -> dict:
    summary = _record_summary(body.text)
    await asyncio.to_thread(
        _set_pending,
        body.group_jid,
        "record",
        summary=summary,
        created_by=body.sender_jid,
    )
    return {
        "status": "record_confirm",
        "reply": f"Record this? “{summary}”\n\nReply yes to save it, or no to skip.",
    }


async def _resolve_record(pending: WhatsappPendingAction, body) -> dict | None:
    await asyncio.to_thread(_clear_pending, body.group_jid)
    summary = pending.summary or ""
    if _is_affirmative(body.text):
        await asyncio.to_thread(
            _capture_record,
            summary,
            group_jid=body.group_jid,
            sender_jid=pending.created_by or body.sender_jid,
            sender_name=body.sender_name,
        )
        return {"status": "recorded", "reply": "Saved to the knowledge base."}
    if _is_negative(body.text):
        return {"status": "record_declined", "reply": "Okay, I won't record that."}
    # Ambiguous reply: abandon the capture (nothing is stored without an explicit
    # yes) and let the message be handled as a normal message.
    return None


# --- calendar (clarify-once, draft fallback) --------------------------------


async def _handle_schedule(body, text: str) -> dict:
    when = parse_datetime(text, now=_now(), tz=_tz())
    if when is None or not when.had_time:
        title = _event_title(text)
        await asyncio.to_thread(
            _set_pending,
            body.group_jid,
            "calendar",
            payload={"text": text},
            created_by=body.sender_jid,
        )
        return {
            "status": "calendar_clarify",
            "reply": f"When should I schedule “{title}”? Give me a day and time.",
        }
    return await _create_or_draft(body, text, when.when)


async def _resolve_calendar(pending: WhatsappPendingAction, body) -> dict:
    await asyncio.to_thread(_clear_pending, body.group_jid)
    original = (pending.payload or {}).get("text", "")
    combined = f"{original} {body.text}".strip()
    when = parse_datetime(combined, now=_now(), tz=_tz())
    if when is None or not when.had_time:
        return {
            "status": "calendar_abandoned",
            "reply": "I still couldn't work out the time, so I've left it. "
            "Ask again with a day and time when you're ready.",
        }
    return await _create_or_draft(body, combined, when.when)


async def _create_or_draft(body, text: str, start_at: datetime) -> dict:
    title = _event_title(text)
    attendees = _attendees(text)
    suffix = f" (with {attendees})" if attendees else ""
    when_str = _fmt(start_at)

    if whatsapp_calendar.calendar_configured():
        try:
            await whatsapp_calendar.create_event(
                title=title, start_at=start_at, attendees=attendees
            )
            return {
                "status": "calendar_created",
                "reply": f"Added “{title}” to the calendar for {when_str}{suffix}.",
            }
        except Exception:
            logger.exception("whatsapp calendar: create failed, drafting instead")

    await asyncio.to_thread(
        _insert_calendar_draft,
        body.group_jid,
        title,
        start_at,
        attendees,
        body.sender_jid,
    )
    return {
        "status": "calendar_drafted",
        "reply": f"I couldn't reach the calendar just now, so I've drafted "
        f"“{title}” for {when_str}{suffix}. It'll be in the morning "
        "digest for you to confirm.",
    }


# --- reminders --------------------------------------------------------------


async def _handle_reminder(body, text: str) -> dict:
    when = parse_datetime(text, now=_now(), tz=_tz())
    reminder_text = _reminder_body(text)
    if when is None:
        await asyncio.to_thread(
            _set_pending,
            body.group_jid,
            "reminder",
            payload={"text": reminder_text},
            created_by=body.sender_jid,
        )
        return {
            "status": "reminder_clarify",
            "reply": f"When should I remind you about “{reminder_text}”?",
        }
    await asyncio.to_thread(
        _insert_reminder, body.group_jid, reminder_text, when.when, body.sender_jid
    )
    return {
        "status": "reminder_set",
        "reply": f"Reminder set for {_fmt(when.when)}: {reminder_text}",
    }


async def _resolve_reminder(pending: WhatsappPendingAction, body) -> dict:
    await asyncio.to_thread(_clear_pending, body.group_jid)
    reminder_text = (pending.payload or {}).get("text", "").strip()
    when = parse_datetime(body.text, now=_now(), tz=_tz())
    if when is None:
        when = parse_datetime(f"{reminder_text} {body.text}", now=_now(), tz=_tz())
    if when is None:
        return {
            "status": "reminder_abandoned",
            "reply": "I still couldn't work out when, so I've left that reminder. "
            "Ask again with a day or time.",
        }
    await asyncio.to_thread(
        _insert_reminder,
        body.group_jid,
        reminder_text or body.text.strip(),
        when.when,
        pending.created_by or body.sender_jid,
    )
    return {
        "status": "reminder_set",
        "reply": f"Reminder set for {_fmt(when.when)}: {reminder_text or body.text.strip()}",
    }


# --- pending dispatch -------------------------------------------------------


async def _resolve_pending(pending: WhatsappPendingAction, body) -> dict | None:
    if pending.kind == "record":
        return await _resolve_record(pending, body)
    if pending.kind == "calendar":
        return await _resolve_calendar(pending, body)
    if pending.kind == "reminder":
        return await _resolve_reminder(pending, body)
    # Unknown kind (should never happen given the CHECK): clear and fall through.
    await asyncio.to_thread(_clear_pending, body.group_jid)
    return None
