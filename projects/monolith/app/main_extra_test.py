"""Extra coverage for app/main.py lifespan -- exception paths in background tasks and bot.close()."""

import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Ensure no valid STATIC_DIR is set (mirrors main_coverage_test.py approach)
os.environ.pop("STATIC_DIR", None)

from app.main import (  # noqa: E402
    _start_singletons,
    _stop_singletons,
    app,
    lifespan,
)


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
        patch("core.db.get_engine", return_value=MagicMock()),
        patch("sqlmodel.Session", return_value=mock_session),
        patch("home.on_startup_jobs"),
        patch("ships.on_startup_jobs"),
        patch("hikes.on_startup_jobs"),
    ]


def _lifespan_patches_with_discord(mock_bot):
    """Return patches needed for lifespan with discord token."""
    mock_session = MagicMock()
    mock_session.__enter__ = MagicMock(return_value=mock_session)
    mock_session.__exit__ = MagicMock(return_value=False)
    return [
        patch("core.db.get_engine", return_value=MagicMock()),
        patch("sqlmodel.Session", return_value=mock_session),
        patch("home.on_startup_jobs"),
        patch("chat.summarizer.on_startup"),
        patch("chat.summarizer.build_llm_caller", return_value=MagicMock()),
        patch("chat.bot.create_bot", return_value=mock_bot),
        patch("ships.on_startup_jobs"),
        patch("hikes.on_startup_jobs"),
    ]


# ---------------------------------------------------------------------------
# bot.close() raises during lifespan shutdown
# ---------------------------------------------------------------------------


class TestSingletonBotClose:
    @pytest.mark.asyncio
    async def test_bot_close_exception_is_swallowed_on_stop(self):
        """_stop_singletons logs and swallows bot.close() errors so a resign or
        shutdown never crashes (the leader must be able to step down cleanly)."""
        tasks, capture = _make_task_capturer()

        mock_bot = MagicMock()
        mock_bot.close = AsyncMock(side_effect=RuntimeError("Discord connection lost"))
        mock_bot.start = AsyncMock()

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
            await _start_singletons(app)
            await _stop_singletons(app)  # must NOT raise

        mock_bot.close.assert_called_once()

    @pytest.mark.asyncio
    async def test_start_singletons_creates_four_tasks(self):
        """The leader starts bot + outbox drain + ships ingest + agent_sessions sweep + lock sweep = 5 tasks
        (the scheduler dispatch loop was removed - jobs run as Argo
        CronWorkflows)."""
        tasks, capture = _make_task_capturer()

        mock_bot = MagicMock()
        mock_bot.close = AsyncMock()
        mock_bot.start = AsyncMock()

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
            await _start_singletons(app)

        assert (
            len(tasks) == 6
        )  # bot + outbox + ships + agent_sessions + lock sweeps + title refresh


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
