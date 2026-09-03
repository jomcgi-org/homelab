"""BDD test fixtures for the knowledge domain."""

import pytest


@pytest.fixture()
def knowledge_mcp_engine(pg):
    """Point MCP-owned sessions at the shared test Postgres instance."""
    from core import db as app_db

    previous_url = app_db.DATABASE_URL
    app_db.get_engine.cache_clear()
    app_db.DATABASE_URL = pg.url
    engine = app_db.get_engine()
    try:
        yield engine
    finally:
        app_db.get_engine.cache_clear()
        engine.dispose()
        app_db.DATABASE_URL = previous_url
