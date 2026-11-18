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


async def _start(env):
    """Start the leader with a mock bot; return the bot, its tasks and mocks."""
    app = SimpleNamespace(state=SimpleNamespace(singleton_tasks=[]))
    bot = MagicMock()
    bot.start = AsyncMock()
    bot.login = AsyncMock()
    bot.is_ready.return_value = False
    bot.user = None
    outbox_drain = AsyncMock()
    scheduler = AsyncMock()
    with (
        patch.dict("os.environ", {"DISCORD_BOT_TOKEN": "test-token", **env}),
        patch("chat.acl.bootstrap_defaults"),
        patch("chat.bot.create_bot", return_value=bot),
        patch("chat.leader.wait_for_sidecar", new_callable=AsyncMock),
        patch("core.db.get_engine", return_value=MagicMock()),
        patch("chat.outbox.run_outbox_drain", outbox_drain),
        patch("chat.scheduled_tasks.run_scheduler", scheduler),
    ):
        tasks = await leader_start(app)
        await asyncio.sleep(0)
    return bot, tasks, outbox_drain, scheduler


async def _stop(tasks):
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


@pytest.mark.asyncio
async def test_gateway_off_logs_in_over_rest_and_still_drains():
    # HOME-221: the factory bridge holds the gateway session for the same
    # token, so the hub must not connect, but its outbox and reminders post.
    bot, tasks, outbox_drain, scheduler = await _start(
        {"DISCORD_GATEWAY_ENABLED": "false"}
    )
    try:
        bot.login.assert_awaited_once_with("test-token")
        bot.start.assert_not_awaited()
        # The outbox drain and the scheduler run; digests end, because nothing
        # stores new channel messages without the gateway.
        assert outbox_drain.await_count == 1
        assert scheduler.await_count == 1
        assert scheduler.await_args.kwargs["digests"] is False
        # bot, outbox drain and scheduler; no lock sweep.
        assert len(tasks) == 3
        ready = outbox_drain.await_args.kwargs["ready"]
        assert ready() is False
        bot.user = MagicMock()  # login() sets the client user
        assert ready() is True
    finally:
        await _stop(tasks)


@pytest.mark.asyncio
async def test_gateway_on_by_default_connects_and_sweeps_locks():
    bot, tasks, outbox_drain, scheduler = await _start({})
    try:
        bot.start.assert_awaited_once_with("test-token")
        bot.login.assert_not_awaited()
        assert scheduler.await_count == 1
        assert scheduler.await_args.kwargs["digests"] is True
        assert len(tasks) == 4
        ready = outbox_drain.await_args.kwargs["ready"]
        bot.user = MagicMock()
        assert ready() is False  # the gateway is not ready yet
        bot.is_ready.return_value = True
        assert ready() is True
    finally:
        await _stop(tasks)
