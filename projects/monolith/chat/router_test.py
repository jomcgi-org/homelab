"""Tests for chat router -- backfill and internal progress endpoints."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from chat.router import router


@pytest.fixture
def app():
    app = FastAPI()
    app.include_router(router)
    app.state.bot = MagicMock()
    app.state.bot.guilds = [MagicMock()]
    app.state.bot.guilds[0].text_channels = [MagicMock(), MagicMock()]
    app.state.backfill_task = None
    return app


@pytest.fixture
def client(app):
    return TestClient(app)


class TestBackfillEndpoint:
    def test_returns_202_and_starts_backfill(self, client, app):
        """POST /api/chat/backfill returns 202 and channel count."""
        with patch("chat.router.run_backfill", new_callable=AsyncMock):
            resp = client.post("/api/chat/backfill")
        assert resp.status_code == 202
        body = resp.json()
        assert body["status"] == "started"
        assert body["channels"] == 2

    def test_returns_409_when_already_running(self, client, app):
        """POST /api/chat/backfill returns 409 if backfill is in progress."""
        running_task = MagicMock()
        running_task.done.return_value = False
        app.state.backfill_task = running_task
        resp = client.post("/api/chat/backfill")
        assert resp.status_code == 409

    def test_returns_503_when_no_bot(self, client, app):
        """POST /api/chat/backfill returns 503 if Discord bot is not running."""
        app.state.bot = None
        resp = client.post("/api/chat/backfill")
        assert resp.status_code == 503

    def test_allows_restart_after_previous_completes(self, client, app):
        """POST /api/chat/backfill allows restart when previous task is done."""
        done_task = MagicMock()
        done_task.done.return_value = True
        app.state.backfill_task = done_task
        with patch("chat.router.run_backfill", new_callable=AsyncMock):
            resp = client.post("/api/chat/backfill")
        assert resp.status_code == 202
