"""Tests for chat.reminders: CRUD guards, ordering, cancellation, and drain."""

from datetime import datetime, timedelta, timezone

import pytest
from sqlmodel import Session, SQLModel, create_engine
from sqlmodel.pool import StaticPool

from chat.models import DiscordOutbox, Reminder
from chat.reminders import (
    MAX_HORIZON_DAYS,
    MAX_PENDING_PER_USER,
    cancel_reminder,
    create_reminder,
    deliver_due,
    list_pending,
    next_due,
)


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


class TestCreateReminder:
    def test_creates_pending_row(self, session):
        due_at = _future(hours=1)
        result = create_reminder(session, "chan-1", "user-1", "stand up", due_at)
        session.commit()

        assert isinstance(result, Reminder)
        assert result.status == "pending"
        assert result.channel_id == "chan-1"
        assert result.author_id == "user-1"
        assert result.content == "stand up"

    def test_rejects_due_at_in_the_past(self, session):
        result = create_reminder(session, "chan-1", "user-1", "late", _future(hours=-1))
        assert result == "due_at must be in the future"

    def test_rejects_due_at_equal_to_now(self, session):
        result = create_reminder(
            session, "chan-1", "user-1", "now", datetime.now(timezone.utc)
        )
        assert isinstance(result, str)

    def test_rejects_due_at_beyond_horizon(self, session):
        result = create_reminder(
            session,
            "chan-1",
            "user-1",
            "far",
            _future(days=MAX_HORIZON_DAYS + 1),
        )
        assert "366" in result or str(MAX_HORIZON_DAYS) in result

    def test_accepts_due_at_within_horizon(self, session):
        result = create_reminder(
            session,
            "chan-1",
            "user-1",
            "far but ok",
            _future(days=MAX_HORIZON_DAYS - 1),
        )
        assert isinstance(result, Reminder)

    def test_rejects_when_author_at_pending_limit(self, session):
        for i in range(MAX_PENDING_PER_USER):
            create_reminder(session, "chan-1", "user-1", f"r{i}", _future(hours=i + 1))
        session.commit()

        result = create_reminder(
            session, "chan-1", "user-1", "one too many", _future(hours=99)
        )
        assert isinstance(result, str)
        assert "10" in result

    def test_pending_limit_is_per_author(self, session):
        for i in range(MAX_PENDING_PER_USER):
            create_reminder(session, "chan-1", "user-1", f"r{i}", _future(hours=i + 1))
        session.commit()

        result = create_reminder(
            session, "chan-1", "user-2", "other user", _future(hours=1)
        )
        assert isinstance(result, Reminder)

    def test_cancelled_reminders_do_not_count_toward_limit(self, session):
        rows = []
        for i in range(MAX_PENDING_PER_USER):
            row = create_reminder(
                session, "chan-1", "user-1", f"r{i}", _future(hours=i + 1)
            )
            rows.append(row)
        session.commit()

        assert cancel_reminder(session, "user-1", rows[0].id)
        session.commit()

        result = create_reminder(
            session, "chan-1", "user-1", "room now", _future(hours=99)
        )
        assert isinstance(result, Reminder)


class TestListPending:
    def test_orders_by_due_at_ascending(self, session):
        create_reminder(session, "chan-1", "user-1", "third", _future(hours=3))
        create_reminder(session, "chan-1", "user-1", "first", _future(hours=1))
        create_reminder(session, "chan-1", "user-1", "second", _future(hours=2))
        session.commit()

        pending = list_pending(session, "user-1")
        assert [r.content for r in pending] == ["first", "second", "third"]

    def test_excludes_other_authors_and_non_pending(self, session):
        create_reminder(session, "chan-1", "user-1", "mine", _future(hours=1))
        create_reminder(session, "chan-1", "user-2", "theirs", _future(hours=1))
        session.commit()

        pending = list_pending(session, "user-1")
        assert len(pending) == 1
        assert pending[0].content == "mine"


class TestCancelReminder:
    def test_cancels_own_pending_reminder(self, session):
        row = create_reminder(
            session, "chan-1", "user-1", "cancel me", _future(hours=1)
        )
        session.commit()

        assert cancel_reminder(session, "user-1", row.id) is True
        session.commit()

        refreshed = session.get(Reminder, row.id)
        assert refreshed.status == "cancelled"

    def test_rejects_wrong_author(self, session):
        row = create_reminder(session, "chan-1", "user-1", "mine", _future(hours=1))
        session.commit()

        assert cancel_reminder(session, "user-2", row.id) is False
        assert session.get(Reminder, row.id).status == "pending"

    def test_rejects_already_resolved_reminder(self, session):
        row = create_reminder(session, "chan-1", "user-1", "mine", _future(hours=1))
        session.commit()
        assert cancel_reminder(session, "user-1", row.id) is True
        session.commit()

        assert cancel_reminder(session, "user-1", row.id) is False

    def test_rejects_missing_reminder(self, session):
        assert cancel_reminder(session, "user-1", 999999) is False


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
        create_reminder(session, "chan-1", "user-1", "later", _future(hours=1))
        session.commit()

        count = deliver_due(session, datetime.now(timezone.utc))
        assert count == 0
        assert session.query(DiscordOutbox).count() == 0


class TestNextDue:
    def test_returns_earliest_pending_due_at(self, session):
        create_reminder(session, "chan-1", "user-1", "later", _future(hours=5))
        create_reminder(session, "chan-1", "user-1", "sooner", _future(hours=1))
        session.commit()

        result = next_due(session)
        assert result is not None

        # sooner is the earliest; compare via the fetched row rather than a
        # fresh _future() call so this isn't flaky against wall-clock drift.
        sooner = list_pending(session, "user-1")[0]
        assert result == sooner.due_at

    def test_ignores_non_pending_rows(self, session):
        row = create_reminder(session, "chan-1", "user-1", "only one", _future(hours=1))
        session.commit()
        cancel_reminder(session, "user-1", row.id)
        session.commit()

        assert next_due(session) is None

    def test_none_when_no_reminders(self, session):
        assert next_due(session) is None
