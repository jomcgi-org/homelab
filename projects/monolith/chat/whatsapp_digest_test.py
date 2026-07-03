"""Tests for chat.whatsapp_digest: rendering, quiet hours, and once-per-day dedupe.

DB-backed via in-memory SQLite (schema stripped). The home calendar snapshot
table is not created here (it is raw-SQL, not SQLModel metadata), so
get_today_events degrades to [] as it does in a not-yet-migrated environment; the
digest still renders reminders and drafts. now is injected so quiet-hours and
send-time gating are deterministic (America/Vancouver is UTC-7 in July).
"""

from datetime import datetime, timedelta, timezone

import pytest
from sqlmodel import Session, SQLModel, create_engine, select
from sqlmodel.pool import StaticPool

from chat import whatsapp_digest
from chat.models import WhatsappGroup, WhatsappOutbox, WhatsappReminder

_GROUP = "12345-67890@g.us"

# 08:30 local (PDT) -> after the 08:00 default send time, outside 22:00-07:00 quiet.
_SEND_NOW = datetime(2026, 7, 3, 15, 30, tzinfo=timezone.utc)
# 23:00 local (PDT) the evening before -> inside quiet hours.
_QUIET_NOW = datetime(2026, 7, 3, 6, 0, tzinfo=timezone.utc)


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


def _seed_group(session, *, digest_config=None, last_digest_at=None):
    session.add(
        WhatsappGroup(
            group_jid=_GROUP,
            enabled=True,
            digest_config=digest_config,
            last_digest_at=last_digest_at,
        )
    )
    session.commit()


def _seed_reminder(session, text="water the plants", *, due_offset_days=-1):
    session.add(
        WhatsappReminder(
            group_jid=_GROUP,
            text=text,
            due_at=_SEND_NOW + timedelta(days=due_offset_days),
        )
    )
    session.commit()


def test_sends_and_marks_reminders_delivered(engine):
    with Session(engine) as session:
        _seed_group(session)
        _seed_reminder(session)

    with Session(engine) as session:
        group = session.get(WhatsappGroup, _GROUP)
        sent = whatsapp_digest._process_group(session, group, _SEND_NOW)
    assert sent is True

    with Session(engine) as session:
        out = session.exec(select(WhatsappOutbox)).one()
        assert out.group_jid == _GROUP and out.kind == "message"
        assert "water the plants" in out.content
        assert "Reminders" in out.content
        rem = session.exec(select(WhatsappReminder)).one()
        assert rem.delivered_at is not None
        assert session.get(WhatsappGroup, _GROUP).last_digest_at is not None


def test_suppressed_during_quiet_hours(engine):
    with Session(engine) as session:
        _seed_group(session)
        _seed_reminder(session)

    with Session(engine) as session:
        group = session.get(WhatsappGroup, _GROUP)
        sent = whatsapp_digest._process_group(session, group, _QUIET_NOW)
    assert sent is False

    with Session(engine) as session:
        assert session.exec(select(WhatsappOutbox)).first() is None
        assert session.exec(select(WhatsappReminder)).one().delivered_at is None


def test_deduped_within_local_day(engine):
    with Session(engine) as session:
        _seed_group(session)
        _seed_reminder(session)

    with Session(engine) as session:
        group = session.get(WhatsappGroup, _GROUP)
        assert whatsapp_digest._process_group(session, group, _SEND_NOW) is True
    # A second run later the same local day is a no-op (already sent).
    with Session(engine) as session:
        group = session.get(WhatsappGroup, _GROUP)
        later = _SEND_NOW + timedelta(hours=2)
        assert whatsapp_digest._process_group(session, group, later) is False
    with Session(engine) as session:
        assert len(session.exec(select(WhatsappOutbox)).all()) == 1


def test_empty_digest_still_sends(engine):
    with Session(engine) as session:
        _seed_group(session)  # no reminders, no drafts

    with Session(engine) as session:
        group = session.get(WhatsappGroup, _GROUP)
        assert whatsapp_digest._process_group(session, group, _SEND_NOW) is True
    with Session(engine) as session:
        out = session.exec(select(WhatsappOutbox)).one()
        assert "Nothing on the calendar" in out.content


def test_handler_iterates_enabled_groups(engine, monkeypatch):
    import asyncio

    with Session(engine) as session:
        _seed_group(session)
        _seed_reminder(session)
        # A disabled group is skipped by the handler's enabled filter.
        session.add(WhatsappGroup(group_jid="other@g.us", enabled=False))
        session.commit()

    # Freeze the handler's clock into the send window via the _utcnow seam.
    monkeypatch.setattr(whatsapp_digest, "_utcnow", lambda: _SEND_NOW)
    with Session(engine) as session:
        asyncio.run(whatsapp_digest.morning_digest_handler(session))

    with Session(engine) as session:
        rows = session.exec(select(WhatsappOutbox)).all()
        assert len(rows) == 1 and rows[0].group_jid == _GROUP
