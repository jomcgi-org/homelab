"""BDD fixtures for the goosecracker run ledger (claude_agent.agent_threads).

The ledger writers open their own DB sessions via ``get_engine()``, so the
SAVEPOINT-based ``session`` fixture cannot be used (their commits happen on
separate connections). Point ``DATABASE_URL`` at the real test Postgres, clear
``get_engine``'s cache, and truncate the table between tests. Mirrors the agent
domain's ``agent_db`` fixture.
"""

from __future__ import annotations

import os

import pytest
from sqlmodel import Session, create_engine, text

# Import chat.api eagerly at collection time. runner._request_replan imports it
# lazily (`from chat.api import replan`) inside an async test; under pytest 9 /
# pytest-asyncio 1.x that first import happens deep in a running event loop,
# where beartype's claw import hook (installed transitively by py-key-value-aio)
# trips a circular import. Importing it here in a clean synchronous context (as
# happened implicitly before the bump) makes the lazy import a cache hit.
import chat.api  # noqa: E402,F401


def _clean(conn) -> None:
    conn.execute(text("DELETE FROM claude_agent.agent_threads"))
    conn.commit()


@pytest.fixture()
def ledger_db(pg):
    """Real Postgres session for the run ledger, with cleanup between tests."""
    raw_url = pg.url.replace("postgresql+psycopg://", "postgresql://", 1)
    os.environ["DATABASE_URL"] = raw_url

    from app import db as app_db

    app_db.DATABASE_URL = pg.url
    app_db.get_engine.cache_clear()

    engine = create_engine(pg.url)
    with engine.connect() as conn:
        _clean(conn)

    with Session(engine) as session:
        yield session

    with engine.connect() as conn:
        _clean(conn)
    engine.dispose()
    app_db.get_engine.cache_clear()
