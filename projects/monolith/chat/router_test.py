"""Tests for chat router backfill targeting and status reporting."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from chat.backfill import BackfillProgress
from chat.router import BackfillRequest, backfill, backfill_status, router


@pytest.fixture
def app():
    app = FastAPI()
    app.include_router(router)
    app.state.bot = MagicMock()
    app.state.bot.guilds = [MagicMock()]
    first = MagicMock(id=101)
    second = MagicMock(id=202)
    app.state.bot.guilds[0].text_channels = [first, second]
    app.state.backfill_task = None
    return app


@pytest.fixture
def client(app):
    return TestClient(app)


def _success_result(channels_total: int = 2) -> BackfillProgress:
    return BackfillProgress(
        channels_total=channels_total,
        channels_completed=channels_total,
        messages_stored=3,
        messages_skipped=1,
    )


class TestBackfillEndpoint:
    @pytest.mark.parametrize("payload", [None, {}, {"channel_ids": None}])
    def test_unscoped_request_starts_all_channels(self, client, payload):
        """No body and an empty body preserve the established all-channel call."""
        mock_run = AsyncMock(return_value=_success_result())
        with patch("chat.router.run_backfill", mock_run):
            if payload is None:
                response = client.post("/api/chat/backfill")
            else:
                response = client.post("/api/chat/backfill", json=payload)

        assert response.status_code == 202
        assert response.json() == {"status": "started", "channels": 2}
        assert mock_run.call_args.kwargs["channel_ids"] is None

    def test_scoped_request_starts_only_visible_requested_channels(self, client):
        mock_run = AsyncMock(return_value=_success_result(1))
        with patch("chat.router.run_backfill", mock_run):
            response = client.post("/api/chat/backfill", json={"channel_ids": ["202"]})

        assert response.status_code == 202
        assert response.json() == {"status": "started", "channels": 1}
        assert mock_run.call_args.kwargs["channel_ids"] == ["202"]

    @pytest.mark.parametrize(
        "payload",
        [
            {"channel_ids": []},
            {"channel_ids": ["101", "101"]},
            {"channel_ids": ["not-a-snowflake"]},
            {"channel_ids": ["0"]},
            {"channel_ids": [101]},
            {"unexpected": True},
        ],
    )
    def test_rejects_invalid_requests(self, client, payload):
        response = client.post("/api/chat/backfill", json=payload)

        assert response.status_code == 422

    def test_rejects_unknown_or_inaccessible_channel_ids(self, client):
        response = client.post("/api/chat/backfill", json={"channel_ids": ["303"]})

        assert response.status_code == 422
        assert "303" in response.json()["detail"]

    def test_returns_409_when_already_running(self, client, app):
        running_task = MagicMock()
        running_task.done.return_value = False
        app.state.backfill_task = running_task

        response = client.post("/api/chat/backfill")

        assert response.status_code == 409

    def test_returns_503_when_no_bot(self, client, app):
        app.state.bot = None

        response = client.post("/api/chat/backfill")

        assert response.status_code == 503

    def test_allows_restart_after_previous_completes(self, client, app):
        done_task = MagicMock()
        done_task.done.return_value = True
        app.state.backfill_task = done_task
        with patch(
            "chat.router.run_backfill", AsyncMock(return_value=_success_result())
        ):
            response = client.post("/api/chat/backfill")

        assert response.status_code == 202


class TestBackfillStatus:
    def test_idle_before_first_backfill(self, client):
        response = client.get("/api/chat/backfill/status")

        assert response.status_code == 200
        assert response.json() == {
            "status": "idle",
            "channel_ids": None,
            "channels_total": 0,
            "channels_completed": 0,
            "messages_stored": 0,
            "messages_skipped": 0,
            "current_channel_id": None,
            "started_at": None,
            "finished_at": None,
            "error": None,
        }

    @pytest.mark.asyncio
    async def test_running_progress_duplicate_start_and_success_are_retained(self, app):
        gate = asyncio.Event()

        async def controlled_run(bot, channel_ids=None, on_progress=None):
            on_progress(
                BackfillProgress(
                    channels_total=1,
                    messages_stored=2,
                    messages_skipped=1,
                    current_channel_id="101",
                )
            )
            await gate.wait()
            return BackfillProgress(
                channels_total=1,
                channels_completed=1,
                messages_stored=4,
                messages_skipped=2,
            )

        request = MagicMock()
        request.app = app
        with patch("chat.router.run_backfill", new=controlled_run):
            response = await backfill(request, BackfillRequest(channel_ids=["101"]))
            retained_task = app.state.backfill_task
            await asyncio.sleep(0)

            running = await backfill_status(request)
            assert response == {"status": "started", "channels": 1}
            assert running.status == "running"
            assert running.current_channel_id == "101"
            assert running.messages_stored == 2
            assert running.messages_skipped == 1

            with pytest.raises(HTTPException) as duplicate:
                await backfill(request, BackfillRequest(channel_ids=["101"]))
            assert duplicate.value.status_code == 409

            gate.set()
            await retained_task

        succeeded = await backfill_status(request)
        assert app.state.backfill_task is retained_task
        assert retained_task.done()
        assert succeeded.status == "success"
        assert succeeded.channels_completed == 1
        assert succeeded.messages_stored == 4
        assert succeeded.messages_skipped == 2
        assert succeeded.finished_at is not None

    @pytest.mark.asyncio
    async def test_failure_remains_inspectable(self, app):
        async def failing_run(bot, channel_ids=None, on_progress=None):
            on_progress(BackfillProgress(channels_total=2, channels_completed=1))
            raise RuntimeError("embedding service unavailable")

        request = MagicMock()
        request.app = app
        with patch("chat.router.run_backfill", new=failing_run):
            await backfill(request)
            task = app.state.backfill_task
            with pytest.raises(RuntimeError, match="embedding service unavailable"):
                await task

        failed = await backfill_status(request)
        assert app.state.backfill_task is task
        assert failed.status == "failure"
        assert failed.channels_completed == 1
        assert failed.error == "embedding service unavailable"
        assert failed.finished_at is not None

    @pytest.mark.asyncio
    async def test_cancellation_is_terminal_and_inspectable(self, app):
        started = asyncio.Event()

        async def blocked_run(bot, channel_ids=None, on_progress=None):
            started.set()
            await asyncio.Event().wait()

        request = MagicMock()
        request.app = app
        with patch("chat.router.run_backfill", new=blocked_run):
            await backfill(request)
            task = app.state.backfill_task
            await started.wait()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

        cancelled = await backfill_status(request)
        assert app.state.backfill_task is task
        assert cancelled.status == "cancelled"
        assert cancelled.finished_at is not None
