"""Tests for the _lock_sweep_loop background task in app.main.

Covers paths not addressed by existing main_* test files:
- _lock_sweep_loop runs sweep and cleanup when no locks are expired
- _lock_sweep_loop calls bot.reprocess_message for each expired lock
- _lock_sweep_loop logs info for each reclaimed lock
- _lock_sweep_loop logs debug when cleanup_completed returns a non-zero count
- _lock_sweep_loop does NOT log debug when cleanup_completed returns 0
- _lock_sweep_loop catches Exception and calls logger.exception('Lock sweep failed')
- _lock_sweep_loop continues after an exception (resilience)
- sweep_task.add_done_callback(_log_task_exception) is registered
- 'Message lock sweep started (30s interval)' is logged when token is set

NOTE on loop structure in _lock_sweep_loop (chat/leader.py leader_start):
  1. while not bot.is_ready(): sleep(2)    # poll until bot connected
  2. while True:
       await asyncio.sleep(30)             # sleep is FIRST in each iteration
       try:
           ... store operations ...
       except Exception:
           logger.exception(...)

So to reach the store operations, the first asyncio.sleep must NOT raise.
Tests that want to verify store behaviour let the first sleep pass through
and cancel on the second sleep (or raise inside the store itself).
"""

from __future__ import annotations

import asyncio
import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Ensure no valid STATIC_DIR is set before importing main
os.environ.pop("STATIC_DIR", None)

from app.main import _log_task_exception, _start_singletons, app  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


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


def _capture_sweep_coro():
    """Capture the _lock_sweep_loop coroutine without closing it.

    Identified by coroutine name rather than creation order, so the test does
    not depend on which order the composed leader hooks spawn their tasks
    (chat starts bot, outbox drain, and sweep; ships starts the AIS ingest).
    """
    mock_bot = MagicMock()
    mock_bot.close = AsyncMock()
    mock_bot.is_ready = MagicMock(return_value=True)

    coros: list = []

    def capture_create_task(coro, **kwargs):
        t = MagicMock()
        cr_code = getattr(coro, "cr_code", None)
        if cr_code is not None and cr_code.co_name == "_lock_sweep_loop":
            coros.append(coro)  # preserve — do NOT close
        else:
            if hasattr(coro, "close"):
                coro.close()
        return t

    return coros, capture_create_task, mock_bot


def _make_lock(discord_message_id: int, channel_id: int) -> MagicMock:
    """Build a mock lock object with discord_message_id and channel_id attributes."""
    lock = MagicMock()
    lock.discord_message_id = discord_message_id
    lock.channel_id = channel_id
    return lock


def _make_session_mock(store: MagicMock) -> MagicMock:
    """Return a context-manager session mock that yields the given store's session."""
    mock_session_obj = MagicMock()
    mock_session_obj.__enter__ = MagicMock(return_value=mock_session_obj)
    mock_session_obj.__exit__ = MagicMock(return_value=False)
    return mock_session_obj


# ---------------------------------------------------------------------------
# sweep_task registration wiring
# ---------------------------------------------------------------------------


class TestSweepTaskRegistration:
    @pytest.mark.asyncio
    async def test_sweep_task_registers_done_callback_with_log_task_exception(self):
        """When DISCORD_BOT_TOKEN is set, sweep_task.add_done_callback(_log_task_exception) is called."""
        mock_bot = MagicMock()
        mock_bot.close = AsyncMock()

        sweep_task_mock = MagicMock()

        def capture_create_task(coro, **kwargs):
            # Identify the sweep by coroutine name, not creation order.
            cr_code = getattr(coro, "cr_code", None)
            is_sweep = cr_code is not None and cr_code.co_name == "_lock_sweep_loop"
            if hasattr(coro, "close"):
                coro.close()
            return sweep_task_mock if is_sweep else MagicMock()

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

        sweep_task_mock.add_done_callback.assert_called_once_with(_log_task_exception)

    @pytest.mark.asyncio
    async def test_sweep_started_log_message_when_token_set(self):
        """'Message lock sweep started (30s interval)' is logged when DISCORD_BOT_TOKEN is set."""
        mock_bot = MagicMock()
        mock_bot.close = AsyncMock()

        def capture_create_task(coro, **kwargs):
            if hasattr(coro, "close"):
                coro.close()
            return MagicMock()

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
            patch("chat.leader.logger") as mock_logger,
        ):
            await _start_singletons(app)

        messages = [str(c) for c in mock_logger.info.call_args_list]
        assert any("Message lock sweep started" in m for m in messages), (
            "Expected 'Message lock sweep started' to be logged when DISCORD_BOT_TOKEN is set"
        )

    @pytest.mark.asyncio
    async def test_sweep_not_started_when_token_absent(self):
        """'Message lock sweep started' is NOT logged when DISCORD_BOT_TOKEN is absent."""
        tasks_created = []

        def capture_create_task(coro, **kwargs):
            if hasattr(coro, "close"):
                coro.close()
            t = MagicMock()
            tasks_created.append(t)
            return t

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
            patch("chat.leader.logger") as mock_logger,
        ):
            await _start_singletons(app)

        # 2 tasks: ships ingest and the agent_sessions pending-message sweep.
        # The scheduler dispatch loop was removed (batch jobs run as Argo
        # CronWorkflows) and the Discord bot is patched out here, so those two
        # leader-elected singletons are the whole set.
        assert len(tasks_created) == 2
        messages = [str(c) for c in mock_logger.info.call_args_list]
        assert not any("Message lock sweep started" in m for m in messages)


# ---------------------------------------------------------------------------
# _lock_sweep_loop body: no expired locks
# ---------------------------------------------------------------------------


class TestLockSweepLoopNoExpiredLocks:
    @pytest.mark.asyncio
    async def test_sweep_loop_calls_reclaim_expired_on_each_iteration(self):
        """_lock_sweep_loop calls store.reclaim_expired on each iteration after the sleep."""
        coros, capture_fn, mock_bot = _capture_sweep_coro()

        mock_store = MagicMock()
        mock_store.reclaim_expired.return_value = []  # no expired locks
        mock_store.cleanup_completed.return_value = 0

        mock_session_obj = MagicMock()
        mock_session_obj.__enter__ = MagicMock(return_value=mock_session_obj)
        mock_session_obj.__exit__ = MagicMock(return_value=False)

        patches = _lifespan_patches_with_discord(mock_bot)
        with (
            patch.dict(os.environ, {"DISCORD_BOT_TOKEN": "fake-token"}),
            patch("asyncio.create_task", side_effect=capture_fn),
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

        assert len(coros) == 1

        # Sleep #1: pass through (let iteration 1 execute the store operations).
        # Sleep #2: cancel to exit the loop.
        sleep_count = [0]

        async def controlled_sleep(_secs):
            sleep_count[0] += 1
            if sleep_count[0] >= 2:
                raise asyncio.CancelledError()

        with (
            patch("asyncio.sleep", side_effect=controlled_sleep),
            patch("app.db.get_engine", return_value=MagicMock()),
            patch("sqlmodel.Session", return_value=mock_session_obj),
            patch("chat.store.MessageStore", return_value=mock_store),
            patch("shared.embedding.EmbeddingClient", return_value=MagicMock()),
            patch("chat.leader.logger"),
        ):
            try:
                await coros[0]
            except asyncio.CancelledError:
                pass

        mock_store.reclaim_expired.assert_called_once_with(ttl_seconds=30, limit=5)

    @pytest.mark.asyncio
    async def test_sweep_loop_calls_cleanup_completed_on_each_iteration(self):
        """_lock_sweep_loop calls store.cleanup_completed on each iteration."""
        coros, capture_fn, mock_bot = _capture_sweep_coro()

        mock_store = MagicMock()
        mock_store.reclaim_expired.return_value = []
        mock_store.cleanup_completed.return_value = 0

        mock_session_obj = MagicMock()
        mock_session_obj.__enter__ = MagicMock(return_value=mock_session_obj)
        mock_session_obj.__exit__ = MagicMock(return_value=False)

        patches = _lifespan_patches_with_discord(mock_bot)
        with (
            patch.dict(os.environ, {"DISCORD_BOT_TOKEN": "fake-token"}),
            patch("asyncio.create_task", side_effect=capture_fn),
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

        assert len(coros) == 1

        sleep_count = [0]

        async def controlled_sleep(_secs):
            sleep_count[0] += 1
            if sleep_count[0] >= 2:
                raise asyncio.CancelledError()

        with (
            patch("asyncio.sleep", side_effect=controlled_sleep),
            patch("app.db.get_engine", return_value=MagicMock()),
            patch("sqlmodel.Session", return_value=mock_session_obj),
            patch("chat.store.MessageStore", return_value=mock_store),
            patch("shared.embedding.EmbeddingClient", return_value=MagicMock()),
            patch("chat.leader.logger"),
        ):
            try:
                await coros[0]
            except asyncio.CancelledError:
                pass

        mock_store.cleanup_completed.assert_called_once_with(max_age_seconds=3600)

    @pytest.mark.asyncio
    async def test_sweep_loop_sleeps_30s_between_iterations(self):
        """_lock_sweep_loop calls asyncio.sleep(30) at the start of each sweep iteration."""
        coros, capture_fn, mock_bot = _capture_sweep_coro()

        mock_store = MagicMock()
        mock_store.reclaim_expired.return_value = []
        mock_store.cleanup_completed.return_value = 0

        mock_session_obj = MagicMock()
        mock_session_obj.__enter__ = MagicMock(return_value=mock_session_obj)
        mock_session_obj.__exit__ = MagicMock(return_value=False)

        patches = _lifespan_patches_with_discord(mock_bot)
        with (
            patch.dict(os.environ, {"DISCORD_BOT_TOKEN": "fake-token"}),
            patch("asyncio.create_task", side_effect=capture_fn),
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

        assert len(coros) == 1

        sleep_calls = []
        sleep_count = [0]

        async def capturing_sleep(secs):
            sleep_calls.append(secs)
            sleep_count[0] += 1
            if sleep_count[0] >= 1:
                raise asyncio.CancelledError()

        with (
            patch("asyncio.sleep", side_effect=capturing_sleep),
            patch("app.db.get_engine", return_value=MagicMock()),
            patch("sqlmodel.Session", return_value=mock_session_obj),
            patch("chat.store.MessageStore", return_value=mock_store),
            patch("shared.embedding.EmbeddingClient", return_value=MagicMock()),
            patch("chat.leader.logger"),
        ):
            try:
                await coros[0]
            except asyncio.CancelledError:
                pass

        assert sleep_calls == [30], (
            f"Expected asyncio.sleep(30) but got calls: {sleep_calls}"
        )

    @pytest.mark.asyncio
    async def test_sweep_loop_does_not_log_debug_when_no_locks_cleaned(self):
        """_lock_sweep_loop does not call logger.debug when cleanup_completed returns 0."""
        coros, capture_fn, mock_bot = _capture_sweep_coro()

        mock_store = MagicMock()
        mock_store.reclaim_expired.return_value = []
        mock_store.cleanup_completed.return_value = 0

        mock_session_obj = MagicMock()
        mock_session_obj.__enter__ = MagicMock(return_value=mock_session_obj)
        mock_session_obj.__exit__ = MagicMock(return_value=False)

        patches = _lifespan_patches_with_discord(mock_bot)
        with (
            patch.dict(os.environ, {"DISCORD_BOT_TOKEN": "fake-token"}),
            patch("asyncio.create_task", side_effect=capture_fn),
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

        assert len(coros) == 1

        # Let one full iteration run (sleep passes), cancel on the second sleep.
        sleep_count = [0]

        async def controlled_sleep(_secs):
            sleep_count[0] += 1
            if sleep_count[0] >= 2:
                raise asyncio.CancelledError()

        with (
            patch("asyncio.sleep", side_effect=controlled_sleep),
            patch("app.db.get_engine", return_value=MagicMock()),
            patch("sqlmodel.Session", return_value=mock_session_obj),
            patch("chat.store.MessageStore", return_value=mock_store),
            patch("shared.embedding.EmbeddingClient", return_value=MagicMock()),
            patch("chat.leader.logger") as mock_logger,
        ):
            try:
                await coros[0]
            except asyncio.CancelledError:
                pass

        mock_logger.debug.assert_not_called()


# ---------------------------------------------------------------------------
# _lock_sweep_loop body: expired locks present
# ---------------------------------------------------------------------------


class TestLockSweepLoopWithExpiredLocks:
    @pytest.mark.asyncio
    async def test_sweep_loop_calls_reprocess_for_each_expired_lock(self):
        """_lock_sweep_loop calls bot.reprocess_message for every expired lock returned."""
        coros, capture_fn, mock_bot = _capture_sweep_coro()
        mock_bot.reprocess_message = AsyncMock()

        lock1 = _make_lock(discord_message_id=111, channel_id=999)
        lock2 = _make_lock(discord_message_id=222, channel_id=888)

        mock_store = MagicMock()
        mock_store.reclaim_expired.return_value = [lock1, lock2]
        mock_store.cleanup_completed.return_value = 0

        mock_session_obj = MagicMock()
        mock_session_obj.__enter__ = MagicMock(return_value=mock_session_obj)
        mock_session_obj.__exit__ = MagicMock(return_value=False)

        patches = _lifespan_patches_with_discord(mock_bot)
        with (
            patch.dict(os.environ, {"DISCORD_BOT_TOKEN": "fake-token"}),
            patch("asyncio.create_task", side_effect=capture_fn),
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

        assert len(coros) == 1

        # Let one full iteration run, cancel on the second sleep.
        sleep_count = [0]

        async def controlled_sleep(_secs):
            sleep_count[0] += 1
            if sleep_count[0] >= 2:
                raise asyncio.CancelledError()

        with (
            patch("asyncio.sleep", side_effect=controlled_sleep),
            patch("app.db.get_engine", return_value=MagicMock()),
            patch("sqlmodel.Session", return_value=mock_session_obj),
            patch("chat.store.MessageStore", return_value=mock_store),
            patch("shared.embedding.EmbeddingClient", return_value=MagicMock()),
            patch("chat.leader.logger"),
        ):
            try:
                await coros[0]
            except asyncio.CancelledError:
                pass

        assert mock_bot.reprocess_message.await_count == 2
        mock_bot.reprocess_message.assert_any_await(111, 999)
        mock_bot.reprocess_message.assert_any_await(222, 888)

    @pytest.mark.asyncio
    async def test_sweep_loop_skips_non_discord_locks(self):
        """A lock whose channel_id is not a numeric snowflake (a WhatsApp group
        JID) is skipped, never reprocessed via the Discord bot -- and it does not
        poison the sweep, so a Discord lock in the same batch still reprocesses."""
        coros, capture_fn, mock_bot = _capture_sweep_coro()
        mock_bot.reprocess_message = AsyncMock()

        wa_lock = _make_lock(
            discord_message_id="3B331C180B2C166EE82A",
            channel_id="120363412404798712@g.us",
        )
        discord_lock = _make_lock(discord_message_id=222, channel_id=888)

        mock_store = MagicMock()
        mock_store.reclaim_expired.return_value = [wa_lock, discord_lock]
        mock_store.cleanup_completed.return_value = 0

        mock_session_obj = MagicMock()
        mock_session_obj.__enter__ = MagicMock(return_value=mock_session_obj)
        mock_session_obj.__exit__ = MagicMock(return_value=False)

        patches = _lifespan_patches_with_discord(mock_bot)
        with (
            patch.dict(os.environ, {"DISCORD_BOT_TOKEN": "fake-token"}),
            patch("asyncio.create_task", side_effect=capture_fn),
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

        assert len(coros) == 1

        sleep_count = [0]

        async def controlled_sleep(_secs):
            sleep_count[0] += 1
            if sleep_count[0] >= 2:
                raise asyncio.CancelledError()

        with (
            patch("asyncio.sleep", side_effect=controlled_sleep),
            patch("app.db.get_engine", return_value=MagicMock()),
            patch("sqlmodel.Session", return_value=mock_session_obj),
            patch("chat.store.MessageStore", return_value=mock_store),
            patch("shared.embedding.EmbeddingClient", return_value=MagicMock()),
            patch("chat.leader.logger"),
        ):
            try:
                await coros[0]
            except asyncio.CancelledError:
                pass

        # Only the Discord lock is reprocessed; the WhatsApp JID lock is skipped.
        assert mock_bot.reprocess_message.await_count == 1
        mock_bot.reprocess_message.assert_awaited_once_with(222, 888)
        # cleanup still ran (the sweep was not poisoned by the WhatsApp lock).
        mock_store.cleanup_completed.assert_called_once_with(max_age_seconds=3600)

    @pytest.mark.asyncio
    async def test_sweep_loop_logs_info_for_each_reclaimed_lock(self):
        """_lock_sweep_loop calls logger.info with the lock's discord_message_id for each reclaimed lock."""
        coros, capture_fn, mock_bot = _capture_sweep_coro()
        mock_bot.reprocess_message = AsyncMock()

        lock = _make_lock(discord_message_id=42, channel_id=7)

        mock_store = MagicMock()
        mock_store.reclaim_expired.return_value = [lock]
        mock_store.cleanup_completed.return_value = 0

        mock_session_obj = MagicMock()
        mock_session_obj.__enter__ = MagicMock(return_value=mock_session_obj)
        mock_session_obj.__exit__ = MagicMock(return_value=False)

        patches = _lifespan_patches_with_discord(mock_bot)
        with (
            patch.dict(os.environ, {"DISCORD_BOT_TOKEN": "fake-token"}),
            patch("asyncio.create_task", side_effect=capture_fn),
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

        assert len(coros) == 1

        # Let one full iteration run, cancel on the second sleep.
        sleep_count = [0]

        async def controlled_sleep(_secs):
            sleep_count[0] += 1
            if sleep_count[0] >= 2:
                raise asyncio.CancelledError()

        with (
            patch("asyncio.sleep", side_effect=controlled_sleep),
            patch("app.db.get_engine", return_value=MagicMock()),
            patch("sqlmodel.Session", return_value=mock_session_obj),
            patch("chat.store.MessageStore", return_value=mock_store),
            patch("shared.embedding.EmbeddingClient", return_value=MagicMock()),
            patch("chat.leader.logger") as mock_logger,
        ):
            try:
                await coros[0]
            except asyncio.CancelledError:
                pass

        # Check that info was logged with the message ID
        info_calls = [str(c) for c in mock_logger.info.call_args_list]
        assert any("42" in m for m in info_calls), (
            "Expected logger.info to include the discord_message_id (42)"
        )

    @pytest.mark.asyncio
    async def test_sweep_loop_logs_debug_when_locks_cleaned(self):
        """_lock_sweep_loop calls logger.debug when cleanup_completed returns a non-zero count."""
        coros, capture_fn, mock_bot = _capture_sweep_coro()
        mock_bot.reprocess_message = AsyncMock()

        mock_store = MagicMock()
        mock_store.reclaim_expired.return_value = []
        mock_store.cleanup_completed.return_value = 3  # 3 locks cleaned

        mock_session_obj = MagicMock()
        mock_session_obj.__enter__ = MagicMock(return_value=mock_session_obj)
        mock_session_obj.__exit__ = MagicMock(return_value=False)

        patches = _lifespan_patches_with_discord(mock_bot)
        with (
            patch.dict(os.environ, {"DISCORD_BOT_TOKEN": "fake-token"}),
            patch("asyncio.create_task", side_effect=capture_fn),
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

        assert len(coros) == 1

        # Let one full iteration run, cancel on the second sleep.
        sleep_count = [0]

        async def controlled_sleep(_secs):
            sleep_count[0] += 1
            if sleep_count[0] >= 2:
                raise asyncio.CancelledError()

        with (
            patch("asyncio.sleep", side_effect=controlled_sleep),
            patch("app.db.get_engine", return_value=MagicMock()),
            patch("sqlmodel.Session", return_value=mock_session_obj),
            patch("chat.store.MessageStore", return_value=mock_store),
            patch("shared.embedding.EmbeddingClient", return_value=MagicMock()),
            patch("chat.leader.logger") as mock_logger,
        ):
            try:
                await coros[0]
            except asyncio.CancelledError:
                pass

        mock_logger.debug.assert_called_once()
        debug_msg = str(mock_logger.debug.call_args)
        assert "3" in debug_msg, "Expected the cleaned count (3) in the debug log"


# ---------------------------------------------------------------------------
# _lock_sweep_loop exception handling
# ---------------------------------------------------------------------------


class TestLockSweepLoopExceptionHandling:
    @pytest.mark.asyncio
    async def test_sweep_loop_logs_exception_when_store_raises(self):
        """_lock_sweep_loop calls logger.exception('Lock sweep failed') when an error occurs."""
        coros, capture_fn, mock_bot = _capture_sweep_coro()

        mock_store = MagicMock()
        mock_store.reclaim_expired.side_effect = RuntimeError("DB connection lost")

        mock_session_obj = MagicMock()
        mock_session_obj.__enter__ = MagicMock(return_value=mock_session_obj)
        mock_session_obj.__exit__ = MagicMock(return_value=False)

        patches = _lifespan_patches_with_discord(mock_bot)
        with (
            patch.dict(os.environ, {"DISCORD_BOT_TOKEN": "fake-token"}),
            patch("asyncio.create_task", side_effect=capture_fn),
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

        assert len(coros) == 1

        sleep_count = [0]

        async def controlled_sleep(_secs):
            sleep_count[0] += 1
            if sleep_count[0] >= 2:
                raise asyncio.CancelledError()

        with (
            patch("asyncio.sleep", side_effect=controlled_sleep),
            patch("app.db.get_engine", return_value=MagicMock()),
            patch("sqlmodel.Session", return_value=mock_session_obj),
            patch("chat.store.MessageStore", return_value=mock_store),
            patch("shared.embedding.EmbeddingClient", return_value=MagicMock()),
            patch("chat.leader.logger") as mock_logger,
        ):
            try:
                await coros[0]
            except asyncio.CancelledError:
                pass

        mock_logger.exception.assert_called_once_with("Lock sweep failed")

    @pytest.mark.asyncio
    async def test_sweep_loop_continues_after_exception(self):
        """_lock_sweep_loop does not propagate exceptions — it catches, logs, then loops."""
        coros, capture_fn, mock_bot = _capture_sweep_coro()

        store_call_count = [0]

        mock_store = MagicMock()

        def raising_reclaim(*_args, **_kwargs):
            store_call_count[0] += 1
            raise RuntimeError("transient error")

        mock_store.reclaim_expired.side_effect = raising_reclaim

        mock_session_obj = MagicMock()
        mock_session_obj.__enter__ = MagicMock(return_value=mock_session_obj)
        mock_session_obj.__exit__ = MagicMock(return_value=False)

        patches = _lifespan_patches_with_discord(mock_bot)
        with (
            patch.dict(os.environ, {"DISCORD_BOT_TOKEN": "fake-token"}),
            patch("asyncio.create_task", side_effect=capture_fn),
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

        assert len(coros) == 1

        sleep_count = [0]

        async def controlled_sleep(_secs):
            sleep_count[0] += 1
            if sleep_count[0] >= 3:
                raise asyncio.CancelledError()

        with (
            patch("asyncio.sleep", side_effect=controlled_sleep),
            patch("app.db.get_engine", return_value=MagicMock()),
            patch("sqlmodel.Session", return_value=mock_session_obj),
            patch("chat.store.MessageStore", return_value=mock_store),
            patch("shared.embedding.EmbeddingClient", return_value=MagicMock()),
            patch("chat.leader.logger"),
        ):
            try:
                await coros[0]
            except asyncio.CancelledError:
                pass

        # The loop ran two iterations despite repeated failures.
        assert store_call_count[0] == 2, (
            f"Expected 2 sweep iterations but got {store_call_count[0]}"
        )

    @pytest.mark.asyncio
    async def test_sweep_loop_awaits_bot_ready_before_loop(self):
        """_lock_sweep_loop polls bot.is_ready() before entering the while loop."""
        coros, capture_fn, mock_bot = _capture_sweep_coro()

        mock_store = MagicMock()
        mock_store.reclaim_expired.return_value = []
        mock_store.cleanup_completed.return_value = 0

        mock_session_obj = MagicMock()
        mock_session_obj.__enter__ = MagicMock(return_value=mock_session_obj)
        mock_session_obj.__exit__ = MagicMock(return_value=False)

        patches = _lifespan_patches_with_discord(mock_bot)
        with (
            patch.dict(os.environ, {"DISCORD_BOT_TOKEN": "fake-token"}),
            patch("asyncio.create_task", side_effect=capture_fn),
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

        assert len(coros) == 1

        # Cancel immediately on the first sleep — we only care that
        # is_ready was called before the loop started.
        async def cancel_on_sleep(_secs):
            raise asyncio.CancelledError()

        with (
            patch("asyncio.sleep", side_effect=cancel_on_sleep),
            patch("app.db.get_engine", return_value=MagicMock()),
            patch("sqlmodel.Session", return_value=mock_session_obj),
            patch("chat.store.MessageStore", return_value=mock_store),
            patch("shared.embedding.EmbeddingClient", return_value=MagicMock()),
            patch("chat.leader.logger"),
        ):
            try:
                await coros[0]
            except asyncio.CancelledError:
                pass

        mock_bot.is_ready.assert_called()
