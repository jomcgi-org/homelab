"""The scheduled-task loop follows chat leader startup and shutdown."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from chat.leader import leader_start


@pytest.mark.asyncio
async def test_leader_tracks_scheduler_for_framework_shutdown():
    app = SimpleNamespace(state=SimpleNamespace(singleton_tasks=[]))
    bot = MagicMock()
    bot.start = AsyncMock()
    bot.is_ready.return_value = True
    scheduler = AsyncMock()
    outbox_drain = AsyncMock()

    with (
        patch.dict("os.environ", {"DISCORD_BOT_TOKEN": "test-token"}),
        patch("chat.acl.bootstrap_defaults"),
        patch("chat.bot.create_bot", return_value=bot),
        patch("chat.summarizer.on_startup"),
        patch("chat.summarizer.build_llm_caller", return_value=AsyncMock()),
        patch("chat.leader.wait_for_sidecar", new_callable=AsyncMock),
        patch("core.db.get_engine", return_value=MagicMock()),
        patch("chat.outbox.run_outbox_drain", outbox_drain),
        patch("chat.scheduled_tasks.run_scheduler", scheduler),
    ):
        tasks = await leader_start(app)
        await asyncio.sleep(0)
        assert scheduler.await_count == 1
        assert any(task in app.state.singleton_tasks for task in tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
