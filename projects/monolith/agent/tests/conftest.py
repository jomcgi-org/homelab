"""BDD test fixtures for the agent (claude_agent.*) domain.

Agent operations open their own DB sessions via ``get_engine()``, so the
SAVEPOINT-based ``session`` fixture from the shared plugin can't be used —
agent commits happen on separate connections and wouldn't be visible to
a SAVEPOINT-wrapped session. Instead, we point ``DATABASE_URL`` at the real
test Postgres, clear ``get_engine``'s cache, and rely on explicit cleanup.

Both ``claude_agent.agent_locks`` and ``claude_agent.routine_jobs`` are
truncated between tests so this fixture can serve every agent test module.
"""

from __future__ import annotations

import os

import pytest
from sqlmodel import Session, create_engine, text


def _clean(conn) -> None:
    conn.execute(text("DELETE FROM claude_agent.agent_locks"))
    conn.execute(text("DELETE FROM claude_agent.routine_jobs"))
    conn.commit()


@pytest.fixture()
def agent_db(pg):
    """Real Postgres session for the agent tables, with cleanup between tests."""
    raw_url = pg.url.replace("postgresql+psycopg://", "postgresql://", 1)
    os.environ["DATABASE_URL"] = raw_url

    # ``core.db.DATABASE_URL`` is a module-level constant captured at import
    # time, so updating ``os.environ["DATABASE_URL"]`` alone is not enough —
    # ``get_engine()`` would still call ``create_engine`` with the prod
    # default. Patch the module attribute directly and clear the cache so
    # the next ``get_engine()`` call builds an engine pointed at the test
    # Postgres started by the ``pg`` fixture.
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
