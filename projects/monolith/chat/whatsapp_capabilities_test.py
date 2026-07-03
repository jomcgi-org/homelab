"""Tests for chat.whatsapp_capabilities: the household capability routing.

DB-backed via in-memory SQLite (schema stripped, like chat.whatsapp_outbox_test).
The knowledge capture and the calendar client are patched so the tests stay
hermetic (no S3, no Google): record capture is asserted at the _capture_record
boundary (proving group+author provenance is passed), and calendar create is
asserted at the whatsapp_calendar.create_event boundary.

Covers spec section 5 acceptance:
- record: nothing captured without an affirmative confirmation; captured on yes.
- calendar: create happy path; ambiguous -> one clarify then resolve; fallback to
  a draft when the credential is absent.
- reminders: creation inserts a row; unparseable time -> one clarify then set.
"""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from sqlmodel import Session, SQLModel, create_engine, select
from sqlmodel.pool import StaticPool

from chat import whatsapp_calendar, whatsapp_capabilities
from chat.models import (
    WhatsappCalendarDraft,
    WhatsappPendingAction,
    WhatsappReminder,
)

_GROUP = "12345-67890@g.us"


@pytest.fixture
def engine():
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


@pytest.fixture(autouse=True)
def _patch_engine(engine, monkeypatch):
    monkeypatch.setattr(whatsapp_capabilities, "get_engine", lambda: engine)


def _body(text, *, sender_jid="alice@s.whatsapp.net", sender_name="Alice"):
    return SimpleNamespace(
        group_jid=_GROUP,
        sender_jid=sender_jid,
        sender_name=sender_name,
        text=text,
        message_id="M1",
    )


def _handle(text, **kw):
    return asyncio.run(whatsapp_capabilities.handle_capability(_body(text, **kw)))


# --- record (confirm-then-capture) -----------------------------------------


def test_record_does_not_capture_without_confirmation(engine, monkeypatch):
    capture = Mock()
    monkeypatch.setattr(whatsapp_capabilities, "_capture_record", capture)

    res = _handle("record: we hiked Garibaldi")

    assert res["status"] == "record_confirm"
    capture.assert_not_called()
    with Session(engine) as session:
        pending = session.get(WhatsappPendingAction, _GROUP)
    assert pending is not None and pending.kind == "record"
    assert "Garibaldi" in pending.summary


def test_record_captured_on_yes_with_provenance(engine, monkeypatch):
    capture = Mock()
    monkeypatch.setattr(whatsapp_capabilities, "_capture_record", capture)

    _handle("record: booked the cabin for August")
    res = _handle("yes")

    assert res["status"] == "recorded"
    capture.assert_called_once()
    args, kwargs = capture.call_args
    assert args[0] == "booked the cabin for August"
    assert kwargs["group_jid"] == _GROUP
    assert kwargs["sender_jid"] == "alice@s.whatsapp.net"
    assert kwargs["sender_name"] == "Alice"
    with Session(engine) as session:
        assert session.get(WhatsappPendingAction, _GROUP) is None


def test_record_declined_on_no(engine, monkeypatch):
    capture = Mock()
    monkeypatch.setattr(whatsapp_capabilities, "_capture_record", capture)

    _handle("record: we hiked Garibaldi")
    res = _handle("no")

    assert res["status"] == "record_declined"
    capture.assert_not_called()
    with Session(engine) as session:
        assert session.get(WhatsappPendingAction, _GROUP) is None


# --- calendar ---------------------------------------------------------------


def test_schedule_creates_event(engine, monkeypatch):
    monkeypatch.setattr(whatsapp_calendar, "calendar_configured", lambda: True)
    create = AsyncMock(return_value={"id": "evt1"})
    monkeypatch.setattr(whatsapp_calendar, "create_event", create)

    res = _handle("add dinner with Sam Friday 7pm")

    assert res["status"] == "calendar_created"
    assert "dinner" in res["reply"]
    create.assert_awaited_once()
    kwargs = create.await_args.kwargs
    assert "dinner" in kwargs["title"]
    assert kwargs["start_at"].tzinfo is not None
    with Session(engine) as session:
        assert session.exec(select(WhatsappCalendarDraft)).first() is None


def test_schedule_ambiguous_clarifies_once_then_resolves(engine, monkeypatch):
    monkeypatch.setattr(whatsapp_calendar, "calendar_configured", lambda: True)
    create = AsyncMock(return_value={"id": "evt1"})
    monkeypatch.setattr(whatsapp_calendar, "create_event", create)

    res = _handle("add dinner with Sam")
    assert res["status"] == "calendar_clarify"
    create.assert_not_awaited()
    with Session(engine) as session:
        pending = session.get(WhatsappPendingAction, _GROUP)
    assert pending is not None and pending.kind == "calendar"

    res2 = _handle("Friday 7pm")
    assert res2["status"] == "calendar_created"
    create.assert_awaited_once()
    with Session(engine) as session:
        assert session.get(WhatsappPendingAction, _GROUP) is None


def test_schedule_falls_back_to_draft_when_unconfigured(engine, monkeypatch):
    monkeypatch.setattr(whatsapp_calendar, "calendar_configured", lambda: False)

    res = _handle("add dinner with Sam Friday 7pm")

    assert res["status"] == "calendar_drafted"
    assert "drafted" in res["reply"]
    with Session(engine) as session:
        draft = session.exec(select(WhatsappCalendarDraft)).one()
    assert draft.group_jid == _GROUP
    assert "dinner" in draft.title
    assert draft.start_at is not None


# --- reminders --------------------------------------------------------------


def test_reminder_inserts_row(engine):
    res = _handle("remind us to water the plants tomorrow at 9am")

    assert res["status"] == "reminder_set"
    with Session(engine) as session:
        r = session.exec(select(WhatsappReminder)).one()
    assert r.group_jid == _GROUP
    assert r.text == "water the plants"
    assert r.due_at is not None
    assert r.delivered_at is None


def test_reminder_clarify_then_set(engine):
    res = _handle("remind us to water the plants")
    assert res["status"] == "reminder_clarify"
    with Session(engine) as session:
        pending = session.get(WhatsappPendingAction, _GROUP)
    assert pending is not None and pending.kind == "reminder"
    with Session(engine) as session:
        assert session.exec(select(WhatsappReminder)).first() is None

    res2 = _handle("tomorrow at 9am")
    assert res2["status"] == "reminder_set"
    with Session(engine) as session:
        r = session.exec(select(WhatsappReminder)).one()
        assert r.text == "water the plants"
        assert session.get(WhatsappPendingAction, _GROUP) is None


def test_plain_message_falls_through(engine):
    assert _handle("what's the ferry plan?") is None
