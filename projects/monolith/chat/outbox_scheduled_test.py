"""Scheduled outbox delivery explicitly handles unknown Discord outcomes."""

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlmodel import Session, SQLModel, create_engine

from chat.models import DiscordOutbox
from chat.outbox import drain_once, enqueue_message


@pytest.fixture(name="engine")
def engine_fixture(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'outbox.db'}",
        connect_args={"check_same_thread": False},
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


def _enqueue(engine):
    with Session(engine) as session:
        enqueue_message(
            session,
            "123",
            content="scheduled",
            kind="scheduled_task",
            dedupe_key="scheduled:abc",
        )
        session.commit()


@pytest.mark.asyncio
async def test_success_crosses_sending_boundary_then_marks_posted(engine):
    _enqueue(engine)
    channel = MagicMock()
    channel.send = AsyncMock(return_value=MagicMock())
    bot = MagicMock()
    bot.get_channel.return_value = channel

    assert await drain_once(bot, engine) == 1
    with Session(engine) as session:
        row = session.query(DiscordOutbox).one()
        assert row.delivery_state == "posted"
        assert row.send_started_at is not None
        assert row.posted_at is not None


@pytest.mark.asyncio
async def test_failed_scheduled_send_becomes_uncertain_and_is_not_retried(engine):
    _enqueue(engine)
    channel = MagicMock()
    channel.send = AsyncMock(side_effect=ConnectionError("connection reset"))
    bot = MagicMock()
    bot.get_channel.return_value = channel

    assert await drain_once(bot, engine) == 0
    assert await drain_once(bot, engine) == 0
    channel.send.assert_awaited_once()
    with Session(engine) as session:
        row = session.query(DiscordOutbox).one()
        assert row.delivery_state == "uncertain"
        assert row.uncertain_at is not None
        assert "outcome unknown" in row.last_error


@pytest.mark.asyncio
async def test_pre_dispatch_channel_failure_remains_retryable(engine):
    _enqueue(engine)
    bot = MagicMock()
    bot.get_channel.return_value = None
    bot.fetch_channel = AsyncMock(side_effect=ConnectionError("lookup failed"))

    assert await drain_once(bot, engine) == 0
    with Session(engine) as session:
        row = session.query(DiscordOutbox).one()
        assert row.delivery_state == "pending"
        assert row.attempts == 1
        assert row.uncertain_at is None


@pytest.mark.asyncio
async def test_restart_quarantines_interrupted_send_without_posting(engine):
    _enqueue(engine)
    with Session(engine) as session:
        row = session.query(DiscordOutbox).one()
        row.delivery_state = "sending"
        row.send_started_at = datetime.now(timezone.utc)
        session.add(row)
        session.commit()
    bot = MagicMock()
    bot.get_channel.return_value = MagicMock(send=AsyncMock())

    assert await drain_once(bot, engine) == 0
    bot.get_channel.assert_not_called()
    with Session(engine) as session:
        row = session.query(DiscordOutbox).one()
        assert row.delivery_state == "uncertain"
        assert "process restarted" in row.last_error
