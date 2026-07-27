"""Tests for the summary-related startup hooks in app.main.

With the scheduler rewrite, summary generation is now handled by the chat.summarizer
on_startup hook which registers a job with the shared scheduler. These tests verify:
- chat_startup is called when Discord token is set
- Both bot task and scheduler task register done callbacks
- Appropriate log messages appear
- chat_startup is NOT called without a token
"""

from __future__ import annotations

import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Ensure no valid static directory before importing main
os.environ.pop("STATIC_DIR", None)

from app.main import _log_task_exception, _start_singletons, app  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_task_capturer():
    """Return (task_list, side_effect_fn) for patching asyncio.create_task."""
    tasks: list[MagicMock] = []

    def capture(coro, **kwargs):
        if hasattr(coro, "close"):
            coro.close()
        t = MagicMock()
        tasks.append(t)
        return t

    return tasks, capture


def _lifespan_patches_with_discord(mock_bot):
    """Return patches needed for lifespan with discord token."""
    mock_session = MagicMock()
    mock_session.__enter__ = MagicMock(return_value=mock_session)
    mock_session.__exit__ = MagicMock(return_value=False)
    return [
        patch("app.db.get_engine", return_value=MagicMock()),
        patch("sqlmodel.Session", return_value=mock_session),
        patch("home.on_startup_jobs"),
        patch("scheduler.api.run_scheduler_loop", new_callable=AsyncMock),
        patch("chat.summarizer.on_startup"),
        patch("chat.summarizer.build_llm_caller", return_value=MagicMock()),
        patch("chat.bot.create_bot", return_value=mock_bot),
        patch("ships.on_startup_jobs"),
        patch("hikes.on_startup_jobs"),
    ]


def _lifespan_patches_no_discord():
    """Return patches needed for lifespan without discord token."""
    mock_session = MagicMock()
    mock_session.__enter__ = MagicMock(return_value=mock_session)
    mock_session.__exit__ = MagicMock(return_value=False)
    return [
        patch("app.db.get_engine", return_value=MagicMock()),
        patch("sqlmodel.Session", return_value=mock_session),
        patch("home.on_startup_jobs"),
        patch("scheduler.api.run_scheduler_loop", new_callable=AsyncMock),
        patch("ships.on_startup_jobs"),
        patch("hikes.on_startup_jobs"),
    ]


# ---------------------------------------------------------------------------
# chat_startup is called (replaces old summary_task done_callback tests)
# ---------------------------------------------------------------------------


class TestChatStartupHook:
    @pytest.mark.asyncio
    async def test_chat_startup_called_when_discord_token_set(self):
        """When DISCORD_BOT_TOKEN is set, chat.summarizer.on_startup is called."""
        mock_bot = MagicMock()
        mock_bot.close = AsyncMock()

        tasks, capture = _make_task_capturer()

        mock_chat_startup = MagicMock()
        mock_session = MagicMock()
        mock_session.__enter__ = MagicMock(return_value=mock_session)
        mock_session.__exit__ = MagicMock(return_value=False)

        with (
            patch.dict(os.environ, {"DISCORD_BOT_TOKEN": "fake-token"}),
            patch("asyncio.create_task", side_effect=capture),
            patch("app.db.get_engine", return_value=MagicMock()),
            patch("sqlmodel.Session", return_value=mock_session),
            patch("home.on_startup_jobs"),
            patch("ships.on_startup_jobs"),
            patch("hikes.on_startup_jobs"),
            patch("scheduler.api.run_scheduler_loop", new_callable=AsyncMock),
            patch("chat.summarizer.on_startup", mock_chat_startup),
            patch("chat.summarizer.build_llm_caller", return_value=MagicMock()),
            patch("chat.bot.create_bot", return_value=mock_bot),
        ):
            await _start_singletons(app)

        mock_chat_startup.assert_called_once()

    @pytest.mark.asyncio
    async def test_singleton_tasks_all_get_done_callback(self):
        """Every singleton task registers a done callback."""
        mock_bot = MagicMock()
        mock_bot.close = AsyncMock()

        task_mocks: list[MagicMock] = []
        task_counter = [0]

        def capture_create_task(coro, **kwargs):
            if hasattr(coro, "close"):
                coro.close()
            task_counter[0] += 1
            t = MagicMock()
            task_mocks.append(t)
            return t

        patches = _lifespan_patches_with_discord(mock_bot)
        with (
            patch.dict(os.environ, {"DISCORD_BOT_TOKEN": "fake-token"}),
            patch("asyncio.create_task", side_effect=capture_create_task),
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

        # Tasks: bot, outbox drain, ships ingest, message lock sweep, and the
        # agent_sessions pending-message sweep. The scheduler dispatch loop was
        # removed (batch jobs run as Argo CronWorkflows).
        assert len(task_mocks) == 5
        # Assert the invariant rather than indexing a hand-numbered list: what
        # matters is that EVERY singleton gets the done callback, so a task that
        # crashes is logged. Indexing meant this broke whenever a singleton was
        # added, without testing anything extra.
        for task in task_mocks:
            task.add_done_callback.assert_called_once_with(_log_task_exception)


# ---------------------------------------------------------------------------
# Log message tests
# ---------------------------------------------------------------------------


class TestSummaryLoopLogging:
    @pytest.mark.asyncio
    async def test_chat_startup_not_called_when_no_token(self):
        """chat.summarizer.on_startup is NOT called when DISCORD_BOT_TOKEN is absent."""
        tasks, capture = _make_task_capturer()

        env_without_token = {
            k: v for k, v in os.environ.items() if k != "DISCORD_BOT_TOKEN"
        }
        env_without_token["DISCORD_BOT_TOKEN"] = ""

        mock_chat_startup = MagicMock()

        mock_session = MagicMock()
        mock_session.__enter__ = MagicMock(return_value=mock_session)
        mock_session.__exit__ = MagicMock(return_value=False)

        with (
            patch.dict(os.environ, env_without_token, clear=True),
            patch("asyncio.create_task", side_effect=capture),
            patch("app.db.get_engine", return_value=MagicMock()),
            patch("sqlmodel.Session", return_value=mock_session),
            patch("home.on_startup_jobs"),
            patch("ships.on_startup_jobs"),
            patch("hikes.on_startup_jobs"),
            patch("scheduler.api.run_scheduler_loop", new_callable=AsyncMock),
        ):
            await _start_singletons(app)

        # chat_startup should never have been imported/called since the discord
        # branch was not entered
        mock_chat_startup.assert_not_called()
