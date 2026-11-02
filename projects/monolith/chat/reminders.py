"""Reminder due-drain core (ambient-assistant parity, Phase 2).

Every function here is synchronous and session-parameterized: unlike
directives.py (which opens its own session per call), these take an explicit
``session`` argument and never commit it themselves. The SQLite ``create_all``
test fixture drives them directly against an in-memory session, and Task 2.3's
scheduler job wraps deliver_due/next_due in its own session/commit, matching
the rest of the chat write API (see chat.outbox.enqueue_message).
"""

from __future__ import annotations

from datetime import datetime

from sqlmodel import Session, select

from core.clock import aware
from chat.models import Reminder
from chat.outbox import enqueue_message


def deliver_due(session: Session, now: datetime) -> int:
    """Enqueue a Discord outbox post for every pending reminder whose due_at
    has passed as of ``now``, flip each to delivered, and return the count
    delivered. The due comparison happens in Python (not a SQL WHERE clause)
    because due_at may be stored naive by the SQLite test fixture while
    ``now`` is tz-aware, and the two are not string/numeric comparable at the
    SQL level; the pending row count is small enough that this is cheap.
    Caller commits."""
    now = aware(now)
    pending = session.exec(select(Reminder).where(Reminder.status == "pending")).all()
    delivered = 0
    for reminder in pending:
        if aware(reminder.due_at) > now:
            continue
        enqueue_message(
            session,
            reminder.channel_id,
            content=f"⏰ <@{reminder.author_id}> reminder: {reminder.content}",
        )
        # reminder comes from this session's own query, so it is already
        # tracked; mutating attributes marks it dirty and commit() flushes
        # the update. No session.add() needed (and adding inside a loop
        # trips session-add-in-loop, which is for freshly constructed rows,
        # not already-tracked ones).
        reminder.status = "delivered"
        reminder.delivered_at = now
        delivered += 1
    return delivered


def next_due(session: Session) -> datetime | None:
    """The earliest due_at among pending reminders, None if there are none."""
    reminder = session.exec(
        select(Reminder).where(Reminder.status == "pending").order_by(Reminder.due_at)
    ).first()
    return reminder.due_at if reminder is not None else None
