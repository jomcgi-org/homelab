"""Integration tests for the full monolith FastAPI application.

Boots the complete app via TestClient with all external dependencies mocked:
- Database: in-memory SQLite with StaticPool
- Lifespan: asyncio.create_task patched to avoid real background tasks
- Discord: DISCORD_BOT_TOKEN unset
- STATIC_DIR: unset (no static mount)
- iCal feed: ICAL_FEED_URL unset (poll_calendar is a no-op)
- Knowledge notes: writes to tmp vault root
"""

from __future__ import annotations

import os
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, SQLModel, create_engine
from sqlmodel.pool import StaticPool

# Ensure no static directory or Discord token so module-level behaviour is
# deterministic when app.main is imported.
os.environ.pop("STATIC_DIR", None)
os.environ.pop("DISCORD_BOT_TOKEN", None)
os.environ.pop("ICAL_FEED_URL", None)

from core.db import get_session  # noqa: E402
from app.main import app  # noqa: E402


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(name="session")
def session_fixture():
    """In-memory SQLite session — strips schemas since SQLite doesn't support them."""
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    original_schemas: dict[str, str] = {}
    for table in SQLModel.metadata.tables.values():
        if table.schema is not None:
            original_schemas[table.name] = table.schema
            table.schema = None

    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        yield session

    for table in SQLModel.metadata.tables.values():
        if table.name in original_schemas:
            table.schema = original_schemas[table.name]


def _make_create_task_patcher():
    """Return a side_effect function that closes coroutines instead of scheduling them."""

    def capture_create_task(coro, **kwargs):
        if hasattr(coro, "close"):
            coro.close()
        mock_task = MagicMock()
        return mock_task

    return capture_create_task


@pytest.fixture(name="client")
def client_fixture(session):
    """TestClient with:
    - DB dependency overridden to use in-memory SQLite
    - asyncio.create_task patched to prevent real background tasks
    """

    def get_session_override():
        yield session

    app.dependency_overrides[get_session] = get_session_override

    with patch("asyncio.create_task", side_effect=_make_create_task_patcher()):
        client = TestClient(app, raise_server_exceptions=False)
        yield client

    app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# Health check
# ---------------------------------------------------------------------------


def test_healthz_returns_ok(client):
    """GET /healthz → 200, {"status": "ok"}."""
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


# ---------------------------------------------------------------------------
# Schedule route
# ---------------------------------------------------------------------------


def test_get_schedule_today_returns_list(client):
    """GET /api/home/schedule/today → 200, list (may be empty)."""
    response = client.get("/api/home/schedule/today")
    assert response.status_code == 200
    assert isinstance(response.json(), list)


# ---------------------------------------------------------------------------
# Knowledge notes route
# ---------------------------------------------------------------------------


def test_post_note_returns_201(client, tmp_path, monkeypatch):
    """POST /api/knowledge/notes with content -> 201 and a raw_inputs row (file-less)."""
    monkeypatch.setenv("VAULT_ROOT", str(tmp_path))
    response = client.post(
        "/api/knowledge/notes",
        json={"content": "test note", "title": "Test Note"},
    )
    assert response.status_code == 201
    raw_id = response.json().get("raw_id", "")
    assert len(raw_id) == 64  # sha256 hex content hash


def test_post_note_empty_content_returns_400(client, tmp_path, monkeypatch):
    """POST /api/knowledge/notes with empty content → 400."""
    monkeypatch.setenv("VAULT_ROOT", str(tmp_path))
    response = client.post("/api/knowledge/notes", json={"content": ""})
    assert response.status_code == 400


def test_post_note_whitespace_content_returns_400(client, tmp_path, monkeypatch):
    """POST /api/knowledge/notes with whitespace-only content → 400."""
    monkeypatch.setenv("VAULT_ROOT", str(tmp_path))
    response = client.post("/api/knowledge/notes", json={"content": "   "})
    assert response.status_code == 400


# ---------------------------------------------------------------------------
