"""Additional coverage for app.main -- lifespan with Discord bot token set."""

import asyncio
import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Ensure no valid STATIC_DIR is set (matches main_test.py approach)
os.environ.pop("STATIC_DIR", None)

from app.main import app, lifespan  # noqa: E402


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


class TestLifespanWithDiscordToken:
    @pytest.mark.asyncio
    async def test_lifespan_starts_bot_when_token_set(self):
        """When DISCORD_BOT_TOKEN is set lifespan creates four tasks (bot, scheduler, ships ingest, sweep)."""
        created_tasks = []

        def capture_create_task(coro, **kwargs):
            if hasattr(coro, "close"):
                coro.close()
            mock_task = MagicMock()
            created_tasks.append(mock_task)
            return mock_task

        mock_bot = MagicMock()
        mock_bot.close = AsyncMock()
        mock_bot.start = AsyncMock(return_value=None)

        patches = _lifespan_patches_with_discord(mock_bot)
        with (
            patch.dict(os.environ, {"DISCORD_BOT_TOKEN": "fake-token-xyz"}),
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
            async with lifespan(app):
                pass

        # bot + scheduler + ships ingest + sweep = 4 tasks
        assert len(created_tasks) == 4

    @pytest.mark.asyncio
    async def test_lifespan_closes_bot_on_shutdown(self):
        """When Discord bot is started lifespan calls bot.close() on shutdown."""
        created_tasks = []

        def capture_create_task(coro, **kwargs):
            if hasattr(coro, "close"):
                coro.close()
            mock_task = MagicMock()
            created_tasks.append(mock_task)
            return mock_task

        mock_bot = MagicMock()
        mock_bot.close = AsyncMock()

        patches = _lifespan_patches_with_discord(mock_bot)
        with (
            patch.dict(os.environ, {"DISCORD_BOT_TOKEN": "fake-token-xyz"}),
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
            async with lifespan(app):
                pass

        mock_bot.close.assert_called_once()

    @pytest.mark.asyncio
    async def test_lifespan_cancels_bot_task_on_shutdown(self):
        """Bot task is cancelled when the lifespan context exits."""
        created_tasks = []

        def capture_create_task(coro, **kwargs):
            if hasattr(coro, "close"):
                coro.close()
            mock_task = MagicMock()
            created_tasks.append(mock_task)
            return mock_task

        mock_bot = MagicMock()
        mock_bot.close = AsyncMock()

        patches = _lifespan_patches_with_discord(mock_bot)
        with (
            patch.dict(os.environ, {"DISCORD_BOT_TOKEN": "fake-token-xyz"}),
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
            async with lifespan(app):
                pass

        # All four tasks (bot, scheduler, ships ingest, sweep) should be cancelled
        for task in created_tasks:
            task.cancel.assert_called_once()

    @pytest.mark.asyncio
    async def test_lifespan_no_bot_task_when_token_empty(self):
        """When DISCORD_BOT_TOKEN is absent lifespan creates exactly 2 tasks (scheduler, ships ingest)."""
        created_tasks = []

        def capture_create_task(coro, **kwargs):
            if hasattr(coro, "close"):
                coro.close()
            mock_task = MagicMock()
            created_tasks.append(mock_task)
            return mock_task

        env_without_token = {
            k: v for k, v in os.environ.items() if k != "DISCORD_BOT_TOKEN"
        }
        env_without_token["DISCORD_BOT_TOKEN"] = ""

        patches = _lifespan_patches_no_discord()
        with (
            patch.dict(os.environ, env_without_token, clear=True),
            patch("asyncio.create_task", side_effect=capture_create_task),
            patches[0],
            patches[1],
            patches[2],
            patches[3],
            patches[4],
        ):
            async with lifespan(app):
                pass

        assert len(created_tasks) == 2


@pytest.mark.asyncio
async def test_lifespan_calls_tracer_provider_shutdown_on_exit():
    """_tracer_provider.shutdown() is called exactly once when the lifespan context exits."""

    def capture_create_task(coro, **kwargs):
        if hasattr(coro, "close"):
            coro.close()
        return MagicMock()

    mock_tracer_provider = MagicMock()

    patches = _lifespan_patches_no_discord()
    with (
        patch("asyncio.create_task", side_effect=capture_create_task),
        patch("app.main._tracer_provider", mock_tracer_provider),
        patches[0],
        patches[1],
        patches[2],
        patches[3],
        patches[4],
    ):
        async with lifespan(app):
            mock_tracer_provider.shutdown.assert_not_called()

    mock_tracer_provider.shutdown.assert_called_once()
