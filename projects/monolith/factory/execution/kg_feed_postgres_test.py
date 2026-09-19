"""Postgres regressions for durable Ember knowledge-feed initialization."""

from __future__ import annotations

import asyncio
from threading import Barrier

from sqlalchemy import event, text
from sqlmodel import Session, create_engine

from factory.execution import kg_feed


def test_concurrent_enabled_feed_passes_persist_one_floor(pg, monkeypatch, caplog):
    engine = create_engine(pg.url)
    with Session(engine) as session:
        session.execute(
            text("DELETE FROM knowledge.feed_state WHERE feed_name = :feed_name"),
            {"feed_name": kg_feed.EMBER_SESSIONS_FEED},
        )
        session.commit()

    insert_barrier = Barrier(2)

    def synchronize_first_insert(_conn, _cursor, statement, *_args):
        if "INSERT INTO knowledge.feed_state" in statement:
            insert_barrier.wait(timeout=10)

    event.listen(engine, "before_cursor_execute", synchronize_first_insert)
    monkeypatch.setenv("KG_FEED_ENABLED", "true")
    monkeypatch.delenv("KG_FEED_SINCE", raising=False)
    monkeypatch.setattr(kg_feed, "get_engine", lambda: engine)

    async def run_both():
        return await asyncio.gather(kg_feed.feed_once(), kg_feed.feed_once())

    try:
        results = asyncio.run(run_both())
    finally:
        event.remove(engine, "before_cursor_execute", synchronize_first_insert)

    assert results == [0, 0]
    assert "KG feed pass failed" not in caplog.text
    with Session(engine) as session:
        count, first_enabled_at = session.execute(
            text(
                "SELECT count(*), min(first_enabled_at) "
                "FROM knowledge.feed_state WHERE feed_name = :feed_name"
            ),
            {"feed_name": kg_feed.EMBER_SESSIONS_FEED},
        ).one()
        assert count == 1
        assert first_enabled_at is not None
    engine.dispose()
