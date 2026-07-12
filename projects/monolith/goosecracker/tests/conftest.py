"""BDD fixtures for the goosecracker run ledger (claude_agent.agent_threads).

The ledger writers open their own DB sessions via ``get_engine()``, so the
SAVEPOINT-based ``session`` fixture cannot be used (their commits happen on
separate connections). Point ``DATABASE_URL`` at the real test Postgres, clear
``get_engine``'s cache, and truncate the table between tests. Mirrors the agent
domain's ``agent_db`` fixture.
"""

from __future__ import annotations

# beartype's claw import hook (installed transitively by py-key-value-aio) has a
# latent circular import in beartype.claw._clawstate that only manifests when the
# hook is first triggered from deep inside a running event loop -- which is
# exactly what happens under pytest 9 / pytest-asyncio 1.x when
# runner._request_replan does its lazy `from chat.api import replan`. Fully load
# _clawstate here, at import time in a clean synchronous context, so claw_state
# is always available when the hook later instruments an import. Then warm
# chat.api itself so the lazy import is a plain cache hit. Both must run before
# any async test.
import beartype.claw._clawstate  # noqa: E402,F401
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
