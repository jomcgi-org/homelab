"""Tests for lifespan singleton failure callbacks.

Summary generation runs separately through the one-shot Argo job.
"""

from __future__ import annotations

import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

os.environ.pop("STATIC_DIR", None)

from app.main import app  # noqa: E402
from app.modules_private import ALL_MODULES  # noqa: E402
from framework import log_task_exception, start_leader_singletons  # noqa: E402


@pytest.mark.asyncio
async def test_singleton_tasks_all_get_done_callback():
    """Every singleton task registers a done callback."""
    mock_bot = MagicMock()
    mock_bot.close = AsyncMock()
    task_mocks: list[MagicMock] = []

    def capture_create_task(coro, **kwargs):
        if hasattr(coro, "close"):
            coro.close()
        task = MagicMock()
        task_mocks.append(task)
        return task

    with (
        patch.dict(os.environ, {"DISCORD_BOT_TOKEN": "fake-token"}),
        patch("asyncio.create_task", side_effect=capture_create_task),
        patch("core.db.get_engine", return_value=MagicMock()),
        patch("chat.acl.bootstrap_defaults"),
        patch("chat.bot.create_bot", return_value=mock_bot),
    ):
        await start_leader_singletons(app, ALL_MODULES)

    # Bot, outbox, scheduled tasks, ships, lock sweep and seven service loops.
    assert len(task_mocks) == 12
    for task in task_mocks:
        task.add_done_callback.assert_called_once_with(log_task_exception)
