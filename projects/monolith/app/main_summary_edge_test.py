"""Edge-case tests for lifespan shutdown in app.main.

Summary generation runs in Argo jobs. These tests cover backfill_task shutdown.
"""

from __future__ import annotations

import os
from unittest.mock import MagicMock, patch

import pytest

# Ensure no valid static directory before importing main
os.environ.pop("STATIC_DIR", None)

from app.main import app, lifespan  # noqa: E402


class _AwaitableTask(MagicMock):
    """Task double: lifespan awaits the cancelled elector task at shutdown."""

    def __await__(self):
        return iter(())


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _lifespan_patches_no_discord():
    """Return patches needed for lifespan without discord token."""
    mock_session = MagicMock()
    mock_session.__enter__ = MagicMock(return_value=mock_session)
    mock_session.__exit__ = MagicMock(return_value=False)
    return [
        patch("core.db.get_engine", return_value=MagicMock()),
        patch("sqlmodel.Session", return_value=mock_session),
    ]


# ---------------------------------------------------------------------------
# Lifespan shutdown: backfill_task.done() returns True → cancel skipped
# ---------------------------------------------------------------------------


class TestLifespanShutdownBackfillDone:
    @pytest.mark.asyncio
    async def test_backfill_task_not_cancelled_when_done(self):
        """If backfill_task.done() returns True, lifespan must NOT call cancel() on it."""
        backfill_mock = MagicMock()
        backfill_mock.done.return_value = True  # task already finished

        def capture_create_task(coro, **kwargs):
            if hasattr(coro, "close"):
                coro.close()
            return _AwaitableTask()

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
        ):
            async with lifespan(app):
                # Inject the already-done backfill task during the lifespan body
                app.state.backfill_task = backfill_mock

        # cancel() must NOT have been called because done() returned True
        backfill_mock.cancel.assert_not_called()

    @pytest.mark.asyncio
    async def test_backfill_task_cancelled_when_not_done(self):
        """If backfill_task.done() returns False, lifespan MUST call cancel()."""
        backfill_mock = _AwaitableTask()
        backfill_mock.done.return_value = False  # task still running

        def capture_create_task(coro, **kwargs):
            if hasattr(coro, "close"):
                coro.close()
            return _AwaitableTask()

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
        ):
            async with lifespan(app):
                app.state.backfill_task = backfill_mock

        backfill_mock.cancel.assert_called_once()
