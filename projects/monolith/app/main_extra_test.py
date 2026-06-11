"""Extra coverage for app/main.py lifespan -- exception paths in background tasks and bot.close()."""

import asyncio
import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Ensure no valid STATIC_DIR is set (mirrors main_coverage_test.py approach)
os.environ.pop("STATIC_DIR", None)

from app.main import app, lifespan  # noqa: E402


# ---------------------------------------------------------------------------
# Helper: create_task capture that drains coroutines without running them
# ---------------------------------------------------------------------------


def _make_task_capturer():
    """Return a list and a side_effect function that captures created tasks."""
    tasks = []

    def capture(coro, **kwargs):
        if hasattr(coro, "close"):
            coro.close()  # avoid "coroutine was never awaited" warnings
        t = MagicMock()
        tasks.append(t)
        return t

    return tasks, capture


def _lifespan_patches_no_discord():
    """Return patches needed for lifespan without discord token."""
    mock_session = MagicMock()
    mock_session.__enter__ = MagicMock(return_value=mock_session)
    mock_session.__exit__ = MagicMock(return_value=False)
    return [
        patch("app.db.get_engine", return_value=MagicMock()),
        patch("sqlmodel.Session", return_value=mock_session),
        patch("home.on_startup_jobs"),
        patch("shared.scheduler.run_scheduler_loop", new_callable=AsyncMock),
        patch("ships.on_startup_jobs"),
    ]


def _lifespan_patches_with_discord(mock_bot):
    """Return patches needed for lifespan with discord token."""
    mock_session = MagicMock()
    mock_session.__enter__ = MagicMock(return_value=mock_session)
    mock_session.__exit__ = MagicMock(return_value=False)
    return [
        patch("app.db.get_engine", return_value=MagicMock()),
        patch("sqlmodel.Session", return_value=mock_session),
        patch("home.on_startup_jobs"),
        patch("shared.scheduler.run_scheduler_loop", new_callable=AsyncMock),
        patch("chat.summarizer.on_startup"),
        patch("chat.summarizer.build_llm_caller", return_value=MagicMock()),
        patch("chat.bot.create_bot", return_value=mock_bot),
        patch("ships.on_startup_jobs"),
    ]


# ---------------------------------------------------------------------------
# bot.close() raises during lifespan shutdown
# ---------------------------------------------------------------------------


class TestLifespanBotCloseException:
    @pytest.mark.asyncio
    async def test_bot_close_exception_propagates(self):
        """When bot.close() raises during shutdown the exception propagates out of lifespan."""
        tasks, capture = _make_task_capturer()

        mock_bot = MagicMock()
        mock_bot.close = AsyncMock(side_effect=RuntimeError("Discord connection lost"))

        patches = _lifespan_patches_with_discord(mock_bot)
        with (
            patch.dict(os.environ, {"DISCORD_BOT_TOKEN": "fake-token-abc"}),
            patch("asyncio.create_task", side_effect=capture),
            patches[0],
            patches[1],
            patches[2],
            patches[3],
            patches[4],
            patches[5],
            patches[6],
            patches[7],
        ):
            with pytest.raises(RuntimeError, match="Discord connection lost"):
                async with lifespan(app):
                    pass

    @pytest.mark.asyncio
    async def test_bot_close_exception_does_not_prevent_task_creation(self):
        """Even when bot.close() will later raise, all 4 tasks are still created at startup."""
        tasks, capture = _make_task_capturer()

        mock_bot = MagicMock()
        mock_bot.close = AsyncMock(side_effect=RuntimeError("close error"))

        patches = _lifespan_patches_with_discord(mock_bot)
        with (
            patch.dict(os.environ, {"DISCORD_BOT_TOKEN": "fake-token-abc"}),
            patch("asyncio.create_task", side_effect=capture),
            patches[0],
            patches[1],
            patches[2],
            patches[3],
            patches[4],
            patches[5],
            patches[6],
            patches[7],
        ):
            try:
                async with lifespan(app):
                    pass
            except RuntimeError:
                pass  # expected

        # bot + scheduler + ships ingest + sweep = 4 tasks must have been created
        assert len(tasks) == 4


# ---------------------------------------------------------------------------
# run_scheduler_loop raises (background task failure)
# ---------------------------------------------------------------------------


class TestLifespanRunSchedulerLoopException:
    @pytest.mark.asyncio
    async def test_lifespan_still_starts_when_run_scheduler_loop_raises(self):
        """Lifespan yields normally even if run_scheduler_loop() raises immediately."""
        started = False

        async def failing_run_scheduler_loop():
            raise RuntimeError("scheduler db error")

        mock_session = MagicMock()
        mock_session.__enter__ = MagicMock(return_value=mock_session)
        mock_session.__exit__ = MagicMock(return_value=False)

        with (
            patch.dict(os.environ, {"DISCORD_BOT_TOKEN": ""}),
            patch("app.db.get_engine", return_value=MagicMock()),
            patch("sqlmodel.Session", return_value=mock_session),
            patch("home.on_startup_jobs"),
            patch("ships.on_startup_jobs"),
            patch(
                "shared.scheduler.run_scheduler_loop",
                new=failing_run_scheduler_loop,
            ),
        ):
            async with lifespan(app):
                await asyncio.sleep(0)
                started = True

        assert started, (
            "lifespan should have yielded despite run_scheduler_loop failure"
        )

    @pytest.mark.asyncio
    async def test_lifespan_shuts_down_cleanly_after_run_scheduler_loop_failure(self):
        """Lifespan completes shutdown even when run_scheduler_loop already raised."""

        async def failing_run_scheduler_loop():
            raise RuntimeError("scheduler db error")

        mock_session = MagicMock()
        mock_session.__enter__ = MagicMock(return_value=mock_session)
        mock_session.__exit__ = MagicMock(return_value=False)

        with (
            patch.dict(os.environ, {"DISCORD_BOT_TOKEN": ""}),
            patch("app.db.get_engine", return_value=MagicMock()),
            patch("sqlmodel.Session", return_value=mock_session),
            patch("home.on_startup_jobs"),
            patch("ships.on_startup_jobs"),
            patch(
                "shared.scheduler.run_scheduler_loop",
                new=failing_run_scheduler_loop,
            ),
        ):
            # Should not raise — background task exceptions are not re-raised
            async with lifespan(app):
                await asyncio.sleep(0)


# ---------------------------------------------------------------------------
# bot.start() raises (background task failure while bot token is set)
# ---------------------------------------------------------------------------


class TestLifespanBotStartException:
    @pytest.mark.asyncio
    async def test_lifespan_still_starts_when_bot_start_raises(self):
        """Lifespan yields normally even if bot.start() raises in its background task."""
        started = False
        tasks, capture = _make_task_capturer()

        mock_bot = MagicMock()
        # close() succeeds so shutdown is clean
        mock_bot.close = AsyncMock()
        # start() raises — but it runs as a background task
        mock_bot.start = AsyncMock(side_effect=RuntimeError("invalid token"))

        patches = _lifespan_patches_with_discord(mock_bot)
        with (
            patch.dict(os.environ, {"DISCORD_BOT_TOKEN": "bad-token"}),
            patch("asyncio.create_task", side_effect=capture),
            patches[0],
            patches[1],
            patches[2],
            patches[3],
            patches[4],
            patches[5],
            patches[6],
            patches[7],
        ):
            async with lifespan(app):
                started = True

        assert started, "lifespan should have yielded even when bot.start raises"

    @pytest.mark.asyncio
    async def test_bot_task_cancelled_even_when_start_raised(self):
        """Bot task is cancelled on shutdown even when bot.start() was going to raise."""
        tasks, capture = _make_task_capturer()

        mock_bot = MagicMock()
        mock_bot.close = AsyncMock()
        mock_bot.start = AsyncMock(side_effect=RuntimeError("invalid token"))

        patches = _lifespan_patches_with_discord(mock_bot)
        with (
            patch.dict(os.environ, {"DISCORD_BOT_TOKEN": "bad-token"}),
            patch("asyncio.create_task", side_effect=capture),
            patches[0],
            patches[1],
            patches[2],
            patches[3],
            patches[4],
            patches[5],
            patches[6],
            patches[7],
        ):
            async with lifespan(app):
                pass

        # All mock tasks should be cancelled
        for task in tasks:
            task.cancel.assert_called_once()
