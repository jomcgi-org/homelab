"""Coverage for the background singletons (Discord bot, scheduler, ships, sweep).

These used to start unconditionally in the lifespan; they now live in
app.main._start_singletons / _stop_singletons, invoked only on the elected
leader replica (see app/leadership.py). The lifespan itself just starts the
leader-election task, so the task-count assertions target _start_singletons
directly. A separate test covers the lifespan wiring + tracer shutdown.
"""

import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Ensure no valid STATIC_DIR is set (matches main_test.py approach)
os.environ.pop("STATIC_DIR", None)

from app.main import _start_singletons, _stop_singletons, app, lifespan  # noqa: E402


def _singleton_patches_no_discord():
    """Patches for _start_singletons without a discord token."""
    mock_session = MagicMock()
    mock_session.__enter__ = MagicMock(return_value=mock_session)
    mock_session.__exit__ = MagicMock(return_value=False)
    return [
        patch("core.db.get_engine", return_value=MagicMock()),
        patch("sqlmodel.Session", return_value=mock_session),
        patch("scheduler.api.run_scheduler_loop", new_callable=AsyncMock),
        patch("scheduler.api.purge_stale_jobs"),
    ]


def _singleton_patches_with_discord(mock_bot):
    """Patches for _start_singletons with a discord token."""
    mock_session = MagicMock()
    mock_session.__enter__ = MagicMock(return_value=mock_session)
    mock_session.__exit__ = MagicMock(return_value=False)
    return [
        patch("core.db.get_engine", return_value=MagicMock()),
        patch("sqlmodel.Session", return_value=mock_session),
        patch("scheduler.api.run_scheduler_loop", new_callable=AsyncMock),
        patch("scheduler.api.purge_stale_jobs"),
        patch("chat.summarizer.on_startup"),
        patch("chat.summarizer.build_llm_caller", return_value=MagicMock()),
        patch("chat.bot.create_bot", return_value=mock_bot),
    ]


def _capture():
    created = []

    def capture_create_task(coro, **kwargs):
        if hasattr(coro, "close"):
            coro.close()
        task = MagicMock()
        created.append(task)
        return task

    return created, capture_create_task


class TestSingletons:
    @pytest.mark.asyncio
    async def test_start_singletons_starts_four_tasks_with_token(self):
        """With a token, the leader starts bot + outbox drain + ships + sweep = 4
        tasks (the scheduler dispatch loop was removed)."""
        created, cap = _capture()
        mock_bot = MagicMock()
        mock_bot.close = AsyncMock()
        mock_bot.start = AsyncMock(return_value=None)

        patches = _singleton_patches_with_discord(mock_bot)
        with (
            patch.dict(os.environ, {"DISCORD_BOT_TOKEN": "fake-token-xyz"}),
            patch("asyncio.create_task", side_effect=cap),
            patches[0],
            patches[1],
            patches[2],
            patches[3],
            patches[4],
            patches[5],
            patches[6],
        ):
            await _start_singletons(app)

        assert len(created) == 5  # bot + outbox + ships + agent_sessions + lock sweeps

    @pytest.mark.asyncio
    async def test_start_singletons_one_task_without_token(self):
        """Without a token: ships + agent_sessions sweeps = 2 tasks (no bot, no drain, no sweep; the
        scheduler dispatch loop was removed - jobs run as Argo CronWorkflows)."""
        created, cap = _capture()
        env = {k: v for k, v in os.environ.items() if k != "DISCORD_BOT_TOKEN"}
        env["DISCORD_BOT_TOKEN"] = ""

        patches = _singleton_patches_no_discord()
        with (
            patch.dict(os.environ, env, clear=True),
            patch("asyncio.create_task", side_effect=cap),
            patches[0],
            patches[1],
            patches[2],
            patches[3],
        ):
            await _start_singletons(app)

        assert len(created) == 2  # ships + agent_sessions sweeps

    @pytest.mark.asyncio
    async def test_stop_singletons_closes_and_cancels(self):
        """_stop_singletons closes the bot and cancels every started task."""
        created, cap = _capture()
        mock_bot = MagicMock()
        mock_bot.close = AsyncMock()
        mock_bot.start = AsyncMock(return_value=None)

        patches = _singleton_patches_with_discord(mock_bot)
        with (
            patch.dict(os.environ, {"DISCORD_BOT_TOKEN": "fake-token-xyz"}),
            patch("asyncio.create_task", side_effect=cap),
            patches[0],
            patches[1],
            patches[2],
            patches[3],
            patches[4],
            patches[5],
            patches[6],
        ):
            await _start_singletons(app)
            await _stop_singletons(app)

        mock_bot.close.assert_called_once()
        for task in created:
            task.cancel.assert_called_once()


@pytest.mark.asyncio
async def test_lifespan_starts_elector_and_shuts_down_tracer():
    """The lifespan starts exactly one task (the leader elector) and shuts down
    the tracer on exit; the singletons themselves are leader-gated."""
    created, cap = _capture()
    mock_tracer_provider = MagicMock()

    mock_session = MagicMock()
    mock_session.__enter__ = MagicMock(return_value=mock_session)
    mock_session.__exit__ = MagicMock(return_value=False)

    with (
        patch("asyncio.create_task", side_effect=cap),
        patch("framework.core._OTEL_PROVIDER", mock_tracer_provider),
        patch("home.observability.rollup.prime_snapshots", new_callable=AsyncMock),
        patch("core.db.get_engine", return_value=MagicMock()),
        patch("sqlmodel.Session", return_value=mock_session),
        patch("knowledge.service.on_startup"),
        patch("home.on_startup_jobs"),
        patch("ships.on_startup_jobs"),
        patch("hikes.on_startup_jobs"),
        patch("stars.on_startup_jobs"),
        patch("dr_jobs.on_startup_jobs"),
        patch("worldcup.on_startup_jobs"),
        patch("knowledge.on_startup_jobs"),
        patch("home.observability.rollup.register"),
    ):
        async with lifespan(app):
            mock_tracer_provider.shutdown.assert_not_called()

    # Exactly one task created by the lifespan body: the leader elector.
    assert len(created) == 1
    created[0].cancel.assert_called_once()
    mock_tracer_provider.shutdown.assert_called_once()
