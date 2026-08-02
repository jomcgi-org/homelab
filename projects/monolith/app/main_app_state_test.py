"""Tests for app.main lifespan app.state assignments and _wait_for_sidecar timeout kwarg.

Covers gaps not addressed by existing main_* test files:

1. app.state.bot is set to the created bot instance when DISCORD_BOT_TOKEN is set.
2. app.state.bot remains None when DISCORD_BOT_TOKEN is absent.
3. _wait_for_sidecar passes timeout=2 as a keyword argument to client.get().

These complement the existing tests in main_sidecar_test.py and main_coverage_test.py
which verify task counts, callbacks, and URL forwarding but not the specific state
assignments or the timeout kwarg.
"""

from __future__ import annotations

import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Ensure no valid static directory is set before importing main
os.environ.pop("STATIC_DIR", None)

from app.main import _start_singletons, _wait_for_sidecar, app, lifespan  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers (mirrors main_sidecar_test.py helper style)
# ---------------------------------------------------------------------------


def _make_mock_async_client(responses):
    """Return a mock httpx.AsyncClient context-manager and the client mock."""
    mock_client = AsyncMock()
    mock_client.get = AsyncMock(side_effect=responses)

    mock_cm = MagicMock()
    mock_cm.__aenter__ = AsyncMock(return_value=mock_client)
    mock_cm.__aexit__ = AsyncMock(return_value=False)
    return mock_cm, mock_client


def _resp(status_code: int) -> MagicMock:
    r = MagicMock()
    r.status_code = status_code
    return r


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
# _wait_for_sidecar() — timeout kwarg forwarded to client.get()
# ---------------------------------------------------------------------------


class TestWaitForSidecarTimeoutKwarg:
    @pytest.mark.asyncio
    async def test_wait_for_sidecar_passes_timeout_2_to_client_get(self):
        """client.get() is called with keyword argument timeout=2."""
        mock_cm, mock_client = _make_mock_async_client([_resp(200)])

        with patch.dict(os.environ, {"FRONTEND_HEALTH_URL": "http://sidecar/healthz"}):
            with patch("httpx.AsyncClient", return_value=mock_cm):
                with patch("asyncio.sleep", new_callable=AsyncMock):
                    await _wait_for_sidecar()

        # Verify keyword argument
        kwargs = mock_client.get.call_args[1]
        assert "timeout" in kwargs, (
            "client.get() was not called with a 'timeout' keyword argument"
        )
        assert kwargs["timeout"] == 2, (
            f"Expected timeout=2 but got timeout={kwargs['timeout']!r}"
        )

    @pytest.mark.asyncio
    async def test_wait_for_sidecar_timeout_kwarg_present_on_retry(self):
        """timeout=2 kwarg is present on every retry attempt, not just the first call."""
        mock_cm, mock_client = _make_mock_async_client([_resp(500), _resp(200)])

        with patch.dict(os.environ, {"FRONTEND_HEALTH_URL": "http://sidecar/healthz"}):
            with patch("httpx.AsyncClient", return_value=mock_cm):
                with patch("asyncio.sleep", new_callable=AsyncMock):
                    await _wait_for_sidecar()

        assert mock_client.get.call_count == 2
        for call in mock_client.get.call_args_list:
            assert call[1].get("timeout") == 2, (
                f"Expected timeout=2 on every call but got: {call[1]}"
            )


# ---------------------------------------------------------------------------
# lifespan() — app.state.bot assignment
# ---------------------------------------------------------------------------


class TestLifespanAppStateBotAssignment:
    @pytest.mark.asyncio
    async def test_app_state_bot_is_set_to_bot_when_token_present(self):
        """app.state.bot is set to the create_bot() return value when the leader
        starts the singletons (the bot now runs only on the elected leader)."""
        mock_bot = MagicMock()
        mock_bot.close = AsyncMock()
        mock_bot.start = AsyncMock()

        def capture_create_task(coro, **kwargs):
            if hasattr(coro, "close"):
                coro.close()
            return MagicMock()

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
            await _start_singletons(app)
            assert app.state.bot is mock_bot, (
                "app.state.bot was not set to the bot created by create_bot()"
            )

    @pytest.mark.asyncio
    async def test_app_state_bot_is_none_when_no_token(self):
        """app.state.bot is None during lifespan when DISCORD_BOT_TOKEN is absent."""

        def capture_create_task(coro, **kwargs):
            if hasattr(coro, "close"):
                coro.close()
            return MagicMock()

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
                assert app.state.bot is None, (
                    "app.state.bot should be None when no DISCORD_BOT_TOKEN is set"
                )

    @pytest.mark.asyncio
    async def test_app_state_backfill_task_initialised_to_none_on_startup(self):
        """lifespan initialises app.state.backfill_task to None at startup."""

        def capture_create_task(coro, **kwargs):
            if hasattr(coro, "close"):
                coro.close()
            return MagicMock()

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
                assert hasattr(app.state, "backfill_task"), (
                    "app.state.backfill_task was not set during lifespan startup"
                )
                assert app.state.backfill_task is None, (
                    "app.state.backfill_task should be None at startup"
                )
