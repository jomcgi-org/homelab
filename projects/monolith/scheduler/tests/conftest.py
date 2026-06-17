"""BDD test fixtures for the shared scheduler domain.

Scheduler handlers claim their own DB sessions via ``get_engine()``, so the
SAVEPOINT-based ``session`` fixture from the shared plugin can't be used —
scheduler commits happen on separate connections and wouldn't be visible to
a SAVEPOINT-wrapped session. Instead, we point ``DATABASE_URL`` at the real
test Postgres, clear ``get_engine``'s cache, and rely on explicit cleanup.
"""

from __future__ import annotations

import os

import pytest
from sqlmodel import Session, create_engine, text


@pytest.fixture()
def scheduler_db(pg):
    """Real Postgres session for the scheduler, with cleanup between tests."""
    raw_url = pg.url.replace("postgresql+psycopg://", "postgresql://", 1)
    os.environ["DATABASE_URL"] = raw_url

    from app.db import get_engine
    from scheduler.api import _registry

    get_engine.cache_clear()
    _registry.clear()

    engine = create_engine(pg.url)
    with engine.connect() as conn:
        conn.execute(text("DELETE FROM scheduler.scheduled_jobs"))
        conn.commit()

    with Session(engine) as session:
        yield session

    with engine.connect() as conn:
        conn.execute(text("DELETE FROM scheduler.scheduled_jobs"))
        conn.commit()
    engine.dispose()
    _registry.clear()
    get_engine.cache_clear()
