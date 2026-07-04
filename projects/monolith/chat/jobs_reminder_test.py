"""Tests for chat.jobs._drain_reminders_core: the testable sync core behind
the chat-drain-reminders Argo CronWorkflow one-shot (see chat/jobs.py).

Only the core is unit tested; the thin async handler (drain_reminders_handler)
just opens its own session and delegates via asyncio.to_thread, per
projects/monolith/CLAUDE.md, and is not covered here (repo convention).
"""

from datetime import datetime, timedelta, timezone

import pytest
from sqlmodel import Session, SQLModel, create_engine
from sqlmodel.pool import StaticPool

from chat.jobs import _drain_reminders_core
from chat.models import DiscordOutbox, Reminder
from chat.reminders import create_reminder


@pytest.fixture(name="session")
def session_fixture():
    """In-memory SQLite session with the full chat schema, schema stripped so
    SQLite accepts the DDL -- mirrors chat.reminders_test's session_fixture."""
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    original = {}
    for table in SQLModel.metadata.tables.values():
        if table.schema is not None:
            original[table.name] = table.schema
            table.schema = None
    SQLModel.metadata.create_all(engine)
    with Session(engine) as s:
        yield s
    for table in SQLModel.metadata.tables.values():
        if table.name in original:
            table.schema = original[table.name]


def _future(**kwargs) -> datetime:
    return datetime.now(timezone.utc) + timedelta(**kwargs)


class TestDrainRemindersCore:
    def test_delivers_due_rows_and_enqueues_outbox(self, session):
        due = Reminder(
            channel_id="chan-1",
            author_id="user-1",
            content="stand up",
            due_at=datetime.now(timezone.utc) - timedelta(minutes=1),
        )
        session.add(due)
        session.commit()

        now = datetime.now(timezone.utc)
        result = _drain_reminders_core(session, now)
        session.commit()

        refreshed = session.get(Reminder, due.id)
        assert refreshed.status == "delivered"
        outbox_rows = session.query(DiscordOutbox).all()
        assert len(outbox_rows) == 1
        assert outbox_rows[0].channel_id == "chan-1"
        assert result is None

    def test_returns_earliest_remaining_pending_due_at(self, session):
        due = Reminder(
            channel_id="chan-1",
            author_id="user-1",
            content="do the thing",
            due_at=datetime.now(timezone.utc) - timedelta(minutes=1),
        )
        session.add(due)
        create_reminder(session, "chan-1", "user-1", "later", _future(hours=1))
        session.commit()

        result = _drain_reminders_core(session, datetime.now(timezone.utc))
        session.commit()

        pending = session.query(Reminder).filter(Reminder.status == "pending").all()
        assert len(pending) == 1
        assert result == pending[0].due_at

    def test_returns_none_when_nothing_pending_remains(self, session):
        due = Reminder(
            channel_id="chan-1",
            author_id="user-1",
            content="only one",
            due_at=datetime.now(timezone.utc) - timedelta(minutes=1),
        )
        session.add(due)
        session.commit()

        result = _drain_reminders_core(session, datetime.now(timezone.utc))
        session.commit()

        assert result is None

    def test_nothing_due_leaves_pending_rows_untouched(self, session):
        create_reminder(session, "chan-1", "user-1", "later", _future(hours=1))
        session.commit()

        result = _drain_reminders_core(session, datetime.now(timezone.utc))
        session.commit()

        assert session.query(DiscordOutbox).count() == 0
        pending = session.query(Reminder).filter(Reminder.status == "pending").all()
        assert len(pending) == 1
        assert result == pending[0].due_at
