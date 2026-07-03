"""Tests for chat router -- backfill and internal progress endpoints."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from chat import goosecracker_progress as gp
from chat.router import internal_router, router


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


@pytest.fixture
def internal_client():
    app = FastAPI()
    app.include_router(internal_router)
    return TestClient(app)


class TestProgressEndpointRestoresNewlines:
    """Regression: the guest streams goose stdout one line per POST with the
    trailing newline stripped, so each chunk is a complete line with no newline.
    The endpoint must restore the newline or marker lines are never parsed into
    stages (they get held as an incomplete fragment forever) and the live
    checklist never renders. This replays the real guest format."""

    def test_line_per_post_markers_parse_into_stages(self, internal_client):
        rid = "test-progress-newline"
        gp.clear(rid)
        for line in ["::stages::1", "::stage::0::running::Answering"]:
            resp = internal_client.post(
                f"/internal/goosecracker/progress/{rid}", json={"chunk": line}
            )
            assert resp.status_code == 204
        snap = gp.get(rid)
        assert snap is not None
        assert len(snap.stages) == 1
        assert snap.stages[0].index == 0
        assert snap.stages[0].state == "running"
        assert snap.stages[0].title == "Answering"
        # A following done marker (also its own newline-less POST) resolves it.
        internal_client.post(
            f"/internal/goosecracker/progress/{rid}",
            json={"chunk": "::stage::0::done::Answering"},
        )
        snap = gp.get(rid)
        assert snap.stages[0].state == "done"
        gp.clear(rid)

    def test_non_marker_line_still_reaches_text(self, internal_client):
        rid = "test-progress-text"
        gp.clear(rid)
        internal_client.post(
            f"/internal/goosecracker/progress/{rid}", json={"chunk": "wrote 12 lines"}
        )
        snap = gp.get(rid)
        assert snap is not None
        assert "wrote 12 lines" in snap.text
        gp.clear(rid)
