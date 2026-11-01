"""Tests for chat.reminders: due delivery and next-due lookup."""

from datetime import datetime, timedelta, timezone

import pytest
from sqlmodel import Session, SQLModel, create_engine
from sqlmodel.pool import StaticPool

from chat.models import DiscordOutbox, Reminder
from chat.reminders import deliver_due, next_due


@pytest.fixture(name="session")
def session_fixture():
    """In-memory SQLite session with the full chat schema (Reminder AND
    DiscordOutbox, since deliver_due writes both), schema stripped so SQLite
    accepts the DDL -- mirrors chat.outbox_test's engine_fixture."""
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


def _add_reminder(session, content: str, due_at: datetime, **kwargs) -> Reminder:
    reminder = Reminder(
        channel_id="chan-1",
        author_id="user-1",
        content=content,
        due_at=due_at,
        **kwargs,
    )
    session.add(reminder)
    return reminder


class TestDeliverDue:
    def test_delivers_only_due_pending_rows(self, session):
        due = Reminder(
            channel_id="chan-1",
            author_id="user-1",
            content="do the thing",
            due_at=datetime.now(timezone.utc) - timedelta(minutes=1),
        )
        cancelled = Reminder(
            channel_id="chan-1",
            author_id="user-1",
            content="cancelled",
            due_at=datetime.now(timezone.utc) - timedelta(minutes=1),
            status="cancelled",
        )
        future = Reminder(
            channel_id="chan-1",
            author_id="user-1",
            content="not yet",
            due_at=_future(hours=1),
        )
        session.add_all([due, cancelled, future])
        session.commit()

        now = datetime.now(timezone.utc)
        count = deliver_due(session, now)
        session.commit()

        assert count == 1

        due_refreshed = session.get(Reminder, due.id)
        assert due_refreshed.status == "delivered"
        assert due_refreshed.delivered_at is not None

        cancelled_refreshed = session.get(Reminder, cancelled.id)
        assert cancelled_refreshed.status == "cancelled"
        assert cancelled_refreshed.delivered_at is None

        future_refreshed = session.get(Reminder, future.id)
        assert future_refreshed.status == "pending"

        outbox_rows = session.query(DiscordOutbox).all()
        assert len(outbox_rows) == 1
        assert outbox_rows[0].channel_id == "chan-1"
        assert outbox_rows[0].content == "⏰ <@user-1> reminder: do the thing"

    def test_handles_naive_due_at_from_sqlite(self, session):
        """A row fetched back from SQLite has a naive due_at regardless of
        what was stored; deliver_due must still compare it correctly."""
        row = Reminder(
            channel_id="chan-1",
            author_id="user-1",
            content="naive",
            due_at=datetime.now(timezone.utc) - timedelta(minutes=1),
        )
        session.add(row)
        session.commit()

        refetched = session.get(Reminder, row.id)
        assert refetched.due_at.tzinfo is None

        count = deliver_due(session, datetime.now(timezone.utc))
        session.commit()
        assert count == 1

    def test_returns_zero_when_nothing_due(self, session):
        _add_reminder(session, "later", _future(hours=1))
        session.commit()

        count = deliver_due(session, datetime.now(timezone.utc))
        assert count == 0
        assert session.query(DiscordOutbox).count() == 0


class TestNextDue:
    def test_returns_earliest_pending_due_at(self, session):
        _add_reminder(session, "later", _future(hours=5))
        sooner = _add_reminder(session, "sooner", _future(hours=1))
        session.commit()

        result = next_due(session)
        assert result is not None

        # Compare via the fetched row rather than a fresh _future() call so
        # this isn't flaky against wall-clock drift.
        assert result == sooner.due_at

    def test_ignores_non_pending_rows(self, session):
        _add_reminder(session, "only one", _future(hours=1), status="cancelled")
        session.commit()

        assert next_due(session) is None

    def test_none_when_no_reminders(self, session):
        assert next_due(session) is None
