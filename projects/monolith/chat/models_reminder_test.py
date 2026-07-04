"""DB-level tests for the chat.Reminder SQLModel (ambient-assistant Phase 2).

Follows the models_db_constraints_test.py idiom: a real SQLite engine builds
the table via create_all (schema stripped so SQLite accepts the DDL) and
exercises both the column defaults and the status CHECK, which is declared
only in the migration SQL and mirrored in __table_args__ specifically so this
fixture can enforce it too.
"""

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, SQLModel, create_engine
from sqlmodel.pool import StaticPool

from chat.models import Reminder


@pytest.fixture(name="session")
def session_fixture():
    """In-memory SQLite session with only the Reminder table created."""
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    table = Reminder.__table__
    saved_schema = table.schema
    table.schema = None
    SQLModel.metadata.create_all(engine, tables=[table])

    with Session(engine) as s:
        yield s

    table.schema = saved_schema


class TestReminderRoundTrip:
    """A Reminder row round-trips through SQLite with the expected defaults."""

    def test_insert_and_fetch(self, session):
        due_at = datetime.now(timezone.utc) + timedelta(hours=1)
        session.add(
            Reminder(
                channel_id="ch-1",
                author_id="user-1",
                content="stand up",
                due_at=due_at,
            )
        )
        session.commit()

        row = session.query(Reminder).one()
        assert row.channel_id == "ch-1"
        assert row.author_id == "user-1"
        assert row.content == "stand up"
        # SQLite returns naive datetimes regardless of what was stored; assert
        # the type round-trips, not the tzinfo.
        assert isinstance(row.due_at, datetime)
        assert isinstance(row.created_at, datetime)
        assert row.delivered_at is None

    def test_status_defaults_to_pending(self, session):
        session.add(
            Reminder(
                channel_id="ch-1",
                author_id="user-1",
                content="stand up",
                due_at=datetime.now(timezone.utc),
            )
        )
        session.commit()

        row = session.query(Reminder).one()
        assert row.status == "pending"

    def test_delivered_at_is_nullable_and_settable(self, session):
        reminder = Reminder(
            channel_id="ch-1",
            author_id="user-1",
            content="stand up",
            due_at=datetime.now(timezone.utc),
        )
        session.add(reminder)
        session.commit()
        assert reminder.delivered_at is None

        delivered_at = datetime.now(timezone.utc)
        reminder.status = "delivered"
        reminder.delivered_at = delivered_at
        session.add(reminder)
        session.commit()

        row = session.query(Reminder).one()
        assert row.status == "delivered"
        assert isinstance(row.delivered_at, datetime)


class TestReminderStatusCheckConstraint:
    """SQLite enforcement of the Reminder.status CHECK (mirrors the migration)."""

    def test_valid_statuses_insert(self, session):
        session.add_all(
            [
                Reminder(
                    channel_id="ch-1",
                    author_id="user-1",
                    content="stand up",
                    due_at=datetime.now(timezone.utc),
                    status=status,
                )
                for status in ("pending", "delivered", "cancelled")
            ]
        )
        session.commit()  # must not raise

    def test_invalid_status_raises_integrity_error(self, session):
        session.add(
            Reminder(
                channel_id="ch-1",
                author_id="user-1",
                content="stand up",
                due_at=datetime.now(timezone.utc),
                status="bogus",
            )
        )
        with pytest.raises(IntegrityError):
            session.commit()
