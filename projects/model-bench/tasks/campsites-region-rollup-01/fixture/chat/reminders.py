"""Reminder CRUD and due-drain core (ambient-assistant parity, Phase 2).

Every function here is synchronous and session-parameterized: unlike
directives.py (which opens its own session per call), these take an explicit
``session`` argument and never commit it themselves. The SQLite ``create_all``
test fixture drives them directly against an in-memory session, and Task 2.3's
scheduler job wraps deliver_due/next_due in its own session/commit, matching
the rest of the chat write API (see chat.outbox.enqueue_message).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlmodel import Session, select

from chat.models import Reminder
from chat.outbox import enqueue_message

# A user can only have this many reminders pending at once; past this, new
# ones are rejected rather than silently queued forever.
MAX_PENDING_PER_USER = 10
# A reminder cannot be scheduled further out than this many days; a typo'd
# year (or an LLM hallucinating a far-future date) would otherwise wait
# unnoticed for a very long time.
MAX_HORIZON_DAYS = 366


def _aware(dt: datetime) -> datetime:
    """Coerce a naive datetime (as SQLite hands back regardless of what was
    stored) to UTC-aware so it can be compared against datetime.now(utc)."""
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def create_reminder(
    session: Session,
    channel_id: str,
    author_id: str,
    content: str,
    due_at: datetime,
) -> Reminder | str:
    """Insert a pending reminder, or return an error string instead of
    raising if due_at is not in the future, is more than MAX_HORIZON_DAYS
    out, or the author already has MAX_PENDING_PER_USER pending reminders.
    Caller commits."""
    now = datetime.now(timezone.utc)
    if _aware(due_at) <= now:
        return "due_at must be in the future"
    if _aware(due_at) > now + timedelta(days=MAX_HORIZON_DAYS):
        return f"due_at cannot be more than {MAX_HORIZON_DAYS} days out"
    if len(list_pending(session, author_id)) >= MAX_PENDING_PER_USER:
        return f"you already have {MAX_PENDING_PER_USER} pending reminders"

    reminder = Reminder(
        channel_id=channel_id,
        author_id=author_id,
        content=content,
        due_at=due_at,
    )
    session.add(reminder)
    return reminder


def list_pending(session: Session, author_id: str) -> list[Reminder]:
    """The author's pending reminders, earliest due first."""
    return list(
        session.exec(
            select(Reminder)
            .where(Reminder.author_id == author_id)
            .where(Reminder.status == "pending")
            .order_by(Reminder.due_at)
        ).all()
    )


def cancel_reminder(session: Session, author_id: str, reminder_id: int) -> bool:
    """Flip a still-pending reminder owned by author_id to cancelled. Returns
    False (no-op) if the row is missing, already resolved, or owned by
    someone else. Caller commits."""
    reminder = session.get(Reminder, reminder_id)
    if reminder is None:
        return False
    if reminder.author_id != author_id or reminder.status != "pending":
        return False
    reminder.status = "cancelled"
    session.add(reminder)
    return True


def deliver_due(session: Session, now: datetime) -> int:
    """Enqueue a Discord outbox post for every pending reminder whose due_at
    has passed as of ``now``, flip each to delivered, and return the count
    delivered. The due comparison happens in Python (not a SQL WHERE clause)
    because due_at may be stored naive by the SQLite test fixture while
    ``now`` is tz-aware, and the two are not string/numeric comparable at the
    SQL level; the pending row count is small enough that this is cheap.
    Caller commits."""
    now = _aware(now)
    pending = session.exec(select(Reminder).where(Reminder.status == "pending")).all()
    delivered = 0
    for reminder in pending:
        if _aware(reminder.due_at) > now:
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
