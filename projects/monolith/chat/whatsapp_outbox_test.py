"""Tests for chat.whatsapp_outbox: enqueue validation and the mirrored CHECK.

The enqueue helpers are pure writers (the Go gateway drains), so the drain logic
is tested on the Go side (whatsapp/outbox_test.go). Here we assert the enqueue
shapes and that the per-kind CHECK constraint, mirrored into __table_args__,
actually rejects malformed rows under SQLite (create_all does not see the
migration-only constraint, so the model must carry it).
"""

from unittest.mock import MagicMock

import pytest
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, SQLModel, create_engine
from sqlmodel.pool import StaticPool

from chat.models import WhatsappOutbox
from chat.whatsapp_outbox import (
    enqueue_edit,
    enqueue_media,
    enqueue_message,
    enqueue_reaction,
)


@pytest.fixture(name="engine")
def engine_fixture():
    """In-memory SQLite engine with the chat schema stripped, so the DB-level
    CHECK constraint (mirrored into the model) is actually exercised."""
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
    yield engine
    for table in SQLModel.metadata.tables.values():
        if table.name in original:
            table.schema = original[table.name]


# --- enqueue shape validation (no DB) --------------------------------------


def test_enqueue_message_adds_row():
    session = MagicMock()
    enqueue_message(session, "g@wa", content="hello")
    row = session.add.call_args.args[0]
    assert row.group_jid == "g@wa"
    assert row.kind == "message"
    assert row.content == "hello"
    assert row.quoted_message_id is None


def test_enqueue_media_adds_row():
    session = MagicMock()
    enqueue_media(session, "g@wa", data=b"\x89PNG...", mime="image/png", caption="hi")
    row = session.add.call_args.args[0]
    assert row.group_jid == "g@wa"
    assert row.kind == "media"
    assert row.media_bytes == b"\x89PNG..."
    assert row.media_mime == "image/png"
    assert row.content == "hi"


def test_enqueue_media_rejects_empty_data():
    with pytest.raises(ValueError):
        enqueue_media(MagicMock(), "g@wa", data=b"", mime="image/png")


def test_enqueue_media_rejects_empty_mime():
    with pytest.raises(ValueError):
        enqueue_media(MagicMock(), "g@wa", data=b"x", mime="")


def test_media_check_accepts_valid_row(engine):
    with Session(engine) as session:
        enqueue_media(session, "g@wa", data=b"x", mime="image/png")
        session.commit()
        assert session.query(WhatsappOutbox).count() == 1


def test_media_check_rejects_missing_bytes(engine):
    # The mirrored CHECK must reject a media row with no bytes (SQLite fixture).
    with Session(engine) as session:
        session.add(
            WhatsappOutbox(group_jid="g@wa", kind="media", media_mime="image/png")
        )
        with pytest.raises(IntegrityError):
            session.commit()


def test_enqueue_message_with_quote():
    session = MagicMock()
    enqueue_message(session, "g@wa", content="re", quoted_message_id="M1")
    row = session.add.call_args.args[0]
    assert row.quoted_message_id == "M1"


def test_enqueue_message_rejects_empty_content():
    session = MagicMock()
    with pytest.raises(ValueError, match="non-empty content"):
        enqueue_message(session, "g@wa", content="")


def test_enqueue_edit_adds_row():
    session = MagicMock()
    enqueue_edit(session, "g@wa", 42, "updated")
    row = session.add.call_args.args[0]
    assert row.kind == "edit"
    assert row.edit_of == 42
    assert row.content == "updated"


def test_enqueue_edit_rejects_empty_content():
    session = MagicMock()
    with pytest.raises(ValueError, match="non-empty content"):
        enqueue_edit(session, "g@wa", 42, "")


def test_enqueue_reaction_adds_row():
    session = MagicMock()
    enqueue_reaction(session, "g@wa", "M9", "author@wa", "\U0001f44d")
    row = session.add.call_args.args[0]
    assert row.kind == "reaction"
    assert row.target_message_id == "M9"
    assert row.target_sender_jid == "author@wa"
    assert row.reaction == "\U0001f44d"
    assert row.reaction_remove is False


def test_enqueue_reaction_remove():
    session = MagicMock()
    enqueue_reaction(session, "g@wa", "M9", "author@wa", "⏳", remove=True)
    row = session.add.call_args.args[0]
    assert row.reaction_remove is True


def test_enqueue_reaction_requires_sender_jid():
    session = MagicMock()
    with pytest.raises(ValueError, match="target_sender_jid"):
        enqueue_reaction(session, "g@wa", "M9", "", "\U0001f44d")


# --- CHECK constraint mirrored into the model (SQLite) ---------------------


def test_valid_message_row_persists(engine):
    with Session(engine) as session:
        enqueue_message(session, "g@wa", content="hi")
        session.commit()
    with Session(engine) as session:
        row = session.query(WhatsappOutbox).one()
    assert row.kind == "message" and row.posted_at is None and row.attempts == 0


def test_valid_reaction_row_persists(engine):
    with Session(engine) as session:
        enqueue_reaction(session, "g@wa", "M9", "author@wa", "\U0001f44d")
        session.commit()
    with Session(engine) as session:
        row = session.query(WhatsappOutbox).one()
    assert row.kind == "reaction" and row.content is None


def test_reaction_missing_sender_jid_rejected_by_check(engine):
    """A reaction row missing target_sender_jid violates the mirrored CHECK.

    Built directly (bypassing the helper's ValueError) to prove the DB-level
    constraint, not just the Python guard, rejects it."""
    with Session(engine) as session:
        session.add(
            WhatsappOutbox(
                group_jid="g@wa",
                kind="reaction",
                target_message_id="M9",
                reaction="\U0001f44d",
            )
        )
        with pytest.raises(IntegrityError):
            session.commit()


def test_message_missing_content_rejected_by_check(engine):
    with Session(engine) as session:
        session.add(WhatsappOutbox(group_jid="g@wa", kind="message"))
        with pytest.raises(IntegrityError):
            session.commit()


def test_edit_missing_edit_of_rejected_by_check(engine):
    with Session(engine) as session:
        session.add(WhatsappOutbox(group_jid="g@wa", kind="edit", content="x"))
        with pytest.raises(IntegrityError):
            session.commit()
