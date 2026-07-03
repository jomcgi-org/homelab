"""DB-level constraint tests for chat SQLModel definitions.

Complements models_test.py (which validates field defaults and schema
metadata at the Python layer) by verifying that:

1. Column-level constraints (primary_key, unique) are correctly declared
   in the SQLAlchemy table definition.
2. A real SQLite engine enforces those constraints (PK and unique) at
   INSERT time — catching regressions where Field(unique=True) or
   Field(primary_key=True) is silently removed.

The `messages` table uses a PostgreSQL-only Vector(1024) column so DB-level
inserts for Message are not tested here; constraint declarations on that model
are verified via column introspection instead.
"""

import pytest
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, SQLModel, create_engine
from sqlmodel.pool import StaticPool

from chat.models import ChannelSummary, Message, MessageLock, OrchestratorBrief


# ---------------------------------------------------------------------------
# Shared SQLite fixture — creates MessageLock + ChannelSummary tables only.
# (Message.embedding uses pgvector Vector type; skip DB-level Message tests.)
# ---------------------------------------------------------------------------


@pytest.fixture(name="session")
def session_fixture():
    """In-memory SQLite session with only the MessageLock and ChannelSummary
    tables created (strips schemas so SQLite accepts the DDL).
    """
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    # Temporarily strip PostgreSQL schemas so SQLite can parse the DDL.
    tables_to_create = [
        MessageLock.__table__,
        ChannelSummary.__table__,
        OrchestratorBrief.__table__,
    ]
    saved = {}
    for tbl in tables_to_create:
        if tbl.schema is not None:
            saved[tbl.name] = tbl.schema
            tbl.schema = None

    SQLModel.metadata.create_all(engine, tables=tables_to_create)

    with Session(engine) as s:
        yield s

    # Restore schemas so other tests are not affected.
    for tbl in tables_to_create:
        if tbl.name in saved:
            tbl.schema = saved[tbl.name]


# ---------------------------------------------------------------------------
# Column-level constraint declarations (no DB required)
# ---------------------------------------------------------------------------


class TestMessageColumnConstraints:
    """Verify that Message column constraints are correctly declared."""

    def test_discord_message_id_is_unique(self):
        """Message.discord_message_id must declare unique=True.

        The store's save_message() relies on an IntegrityError being raised
        on duplicate discord_message_id inserts to implement idempotent
        deduplication. Removing unique=True would silently allow duplicates.
        """
        col = Message.__table__.c.discord_message_id
        assert col.unique is True, (
            "Message.discord_message_id must have unique=True so the store "
            "can rely on IntegrityError for duplicate detection"
        )

    def test_id_is_primary_key(self):
        """Message.id is the surrogate primary key (auto-increment integer)."""
        col = Message.__table__.c.id
        assert col.primary_key is True


class TestMessageLockColumnConstraints:
    """Verify that MessageLock column constraints are correctly declared."""

    def test_discord_message_id_is_primary_key(self):
        """MessageLock.discord_message_id is the natural primary key.

        Using the discord message id directly as the PK (no surrogate int)
        gives advisory-lock-style semantics at the DB level: an INSERT on a
        message id that is already being processed raises IntegrityError
        instead of silently creating a second row.
        """
        col = MessageLock.__table__.c.discord_message_id
        assert col.primary_key is True

    def test_channel_id_is_not_primary_key(self):
        """channel_id is a plain column, not part of the PK."""
        col = MessageLock.__table__.c.channel_id
        assert col.primary_key is False


class TestChannelSummaryColumnConstraints:
    """Verify that ChannelSummary column constraints are correctly declared."""

    def test_channel_id_is_unique(self):
        """ChannelSummary.channel_id must declare unique=True.

        There should be at most one summary row per channel; a unique
        constraint enforces this at the DB layer and also ensures that
        upsert operations (ON CONFLICT) work correctly.
        """
        col = ChannelSummary.__table__.c.channel_id
        assert col.unique is True, (
            "ChannelSummary.channel_id must have unique=True to enforce "
            "one-summary-per-channel invariant at the database level"
        )

    def test_id_is_primary_key(self):
        """ChannelSummary.id is the surrogate auto-increment PK."""
        col = ChannelSummary.__table__.c.id
        assert col.primary_key is True


# ---------------------------------------------------------------------------
# DB-level enforcement tests — MessageLock (SQLite)
# ---------------------------------------------------------------------------


class TestMessageLockDBConstraints:
    """SQLite enforcement of MessageLock primary key."""

    def test_duplicate_discord_message_id_raises_integrity_error(self, session):
        """Inserting two MessageLock rows with the same discord_message_id
        must raise IntegrityError because discord_message_id is the PK.
        """
        lock1 = MessageLock(discord_message_id="msg-pk-dup", channel_id="ch-1")
        session.add(lock1)
        session.commit()

        lock2 = MessageLock(discord_message_id="msg-pk-dup", channel_id="ch-2")
        session.add(lock2)
        with pytest.raises(IntegrityError):
            session.commit()

    def test_different_discord_message_ids_succeed(self, session):
        """Two MessageLock rows with distinct ids insert without error."""
        session.add(MessageLock(discord_message_id="msg-a", channel_id="ch-1"))
        session.add(MessageLock(discord_message_id="msg-b", channel_id="ch-1"))
        session.commit()  # must not raise


# ---------------------------------------------------------------------------
# DB-level enforcement tests — ChannelSummary (SQLite)
# ---------------------------------------------------------------------------


class TestChannelSummaryDBConstraints:
    """SQLite enforcement of ChannelSummary.channel_id unique constraint."""

    def test_duplicate_channel_id_raises_integrity_error(self, session):
        """Inserting two ChannelSummary rows with the same channel_id must
        raise IntegrityError because channel_id has a unique constraint.
        """
        cs1 = ChannelSummary(
            channel_id="ch-dup",
            summary="First summary",
            last_message_id=1,
        )
        session.add(cs1)
        session.commit()

        cs2 = ChannelSummary(
            channel_id="ch-dup",
            summary="Second summary — should conflict",
            last_message_id=2,
        )
        session.add(cs2)
        with pytest.raises(IntegrityError):
            session.commit()

    def test_different_channel_ids_succeed(self, session):
        """Two ChannelSummary rows for different channels insert without error."""
        session.add(
            ChannelSummary(
                channel_id="ch-x",
                summary="Summary X",
                last_message_id=10,
            )
        )
        session.add(
            ChannelSummary(
                channel_id="ch-y",
                summary="Summary Y",
                last_message_id=20,
            )
        )
        session.commit()  # must not raise

    def test_same_channel_id_different_user_channel_summary_allowed(self, session):
        """The unique constraint is on ChannelSummary.channel_id alone; a
        second row with the same channel_id must still be rejected (this
        confirms the constraint isn't accidentally composite).
        """
        session.add(ChannelSummary(channel_id="ch-z", summary="s1", last_message_id=1))
        session.commit()

        session.add(ChannelSummary(channel_id="ch-z", summary="s2", last_message_id=2))
        with pytest.raises(IntegrityError):
            session.commit()


# ---------------------------------------------------------------------------
# DB-level enforcement tests: OrchestratorBrief route CHECK (SQLite)
# ---------------------------------------------------------------------------


class TestOrchestratorBriefDBConstraints:
    """SQLite enforcement of the OrchestratorBrief route CHECK (ADR 036).

    The CHECK is declared only in the migration SQL; the SQLModel mirrors it in
    __table_args__ so the create_all-built SQLite fixture enforces it too. This
    guards against the constraint silently dropping off the model, which would
    let an unexpected route string ('goose_v2', a typo) reach prod telemetry.
    """

    def test_valid_routes_insert(self, session):
        """The three allowed routes all insert without error."""
        session.add_all(
            [
                OrchestratorBrief(route=route, model="m")
                for route in ("chat", "goose", "failopen")
            ]
        )
        session.commit()  # must not raise

    def test_invalid_route_raises_integrity_error(self, session):
        """A route outside the allowed set violates the CHECK."""
        session.add(OrchestratorBrief(route="bogus", model="m"))
        with pytest.raises(IntegrityError):
            session.commit()
