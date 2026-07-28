"""BDD fixtures for the goosecracker run ledger (claude_agent.agent_threads).

The ledger writers open their own DB sessions via ``get_engine()``, so the
SAVEPOINT-based ``session`` fixture cannot be used (their commits happen on
separate connections). Point ``DATABASE_URL`` at the real test Postgres, clear
``get_engine``'s cache, and truncate the table between tests. Mirrors the agent
domain's ``agent_db`` fixture.
"""

from __future__ import annotations

# Warm chat.api at collection time (clean synchronous context) so the async
# run_one_turn tests, which `import chat.api` and patch its attributes, don't
# trigger its import graph -- including beartype's claw hook -- from inside a
# running event loop under pytest-asyncio 1.x.
import chat.api  # noqa: E402,F401

import os  # noqa: E402

import pytest  # noqa: E402
from sqlmodel import Session, create_engine, text  # noqa: E402


def _clean(conn) -> None:
    conn.execute(text("DELETE FROM claude_agent.agent_threads"))
    conn.commit()


@pytest.fixture()
def ledger_db(pg):
    """Real Postgres session for the run ledger, with cleanup between tests."""
    raw_url = pg.url.replace("postgresql+psycopg://", "postgresql://", 1)
    os.environ["DATABASE_URL"] = raw_url

    from core import db as app_db

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
