"""Tests for _wait_for_sidecar() and _start_bot_when_ready() in app.main.

Covers:
- Returns immediately when FRONTEND_HEALTH_URL is empty or unset
- Returns on first try when sidecar responds with a non-5xx status
- Retries on 5xx responses then succeeds
- Retries on httpx.HTTPError then succeeds
- _start_bot_when_ready calls _wait_for_sidecar then bot.start in that order
"""

from __future__ import annotations

import os
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Ensure no valid static directory is set (mirrors other main_* test files)
os.environ.pop("STATIC_DIR", None)

from app.main import _wait_for_sidecar  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_mock_async_client(responses):
    """Return a mock httpx.AsyncClient async-context-manager.

    ``responses`` is a list of either MagicMock objects with a ``status_code``
    attribute (simulating successful HTTP responses) or exception instances to
    be raised by ``client.get()``.
    """
    mock_client = AsyncMock()
    mock_client.get = AsyncMock(side_effect=responses)

    mock_cm = MagicMock()
    mock_cm.__aenter__ = AsyncMock(return_value=mock_client)
    mock_cm.__aexit__ = AsyncMock(return_value=False)
    return mock_cm, mock_client


def _resp(status_code: int) -> MagicMock:
    """Convenience: build a mock HTTP response with the given status_code."""
    r = MagicMock()
    r.status_code = status_code
    return r


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


# ---------------------------------------------------------------------------
# (1) Returns immediately when FRONTEND_HEALTH_URL is empty / unset
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_wait_for_sidecar_returns_immediately_when_url_empty():
    """_wait_for_sidecar returns at once when FRONTEND_HEALTH_URL is set to ''."""
    with patch.dict(os.environ, {"FRONTEND_HEALTH_URL": ""}):
        # Must not block, raise, or call httpx at all
        await _wait_for_sidecar()


@pytest.mark.asyncio
async def test_wait_for_sidecar_returns_immediately_when_url_unset():
    """_wait_for_sidecar returns at once when FRONTEND_HEALTH_URL is absent."""
    env_without_url = {
        k: v for k, v in os.environ.items() if k != "FRONTEND_HEALTH_URL"
    }
    with patch.dict(os.environ, env_without_url, clear=True):
        await _wait_for_sidecar()


@pytest.mark.asyncio
async def test_wait_for_sidecar_never_calls_httpx_when_url_empty():
    """When FRONTEND_HEALTH_URL is empty, httpx.AsyncClient is never instantiated."""
    with patch.dict(os.environ, {"FRONTEND_HEALTH_URL": ""}):
        with patch("httpx.AsyncClient") as mock_cls:
            await _wait_for_sidecar()
        mock_cls.assert_not_called()


# ---------------------------------------------------------------------------
# (2) Returns on first try when sidecar responds < 500
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_wait_for_sidecar_returns_immediately_on_200():
    """Returns after a single HTTP call when the sidecar replies with 200."""
    mock_cm, mock_client = _make_mock_async_client([_resp(200)])

    with patch.dict(os.environ, {"FRONTEND_HEALTH_URL": "http://sidecar/healthz"}):
        with patch("httpx.AsyncClient", return_value=mock_cm):
            with patch("asyncio.sleep", new_callable=AsyncMock):
                await _wait_for_sidecar()

    mock_client.get.assert_called_once()


@pytest.mark.asyncio
async def test_wait_for_sidecar_returns_immediately_on_404():
    """Returns after a single call for a 404 response (non-5xx counts as healthy)."""
    mock_cm, mock_client = _make_mock_async_client([_resp(404)])

    with patch.dict(os.environ, {"FRONTEND_HEALTH_URL": "http://sidecar/healthz"}):
        with patch("httpx.AsyncClient", return_value=mock_cm):
            with patch("asyncio.sleep", new_callable=AsyncMock):
                await _wait_for_sidecar()

    mock_client.get.assert_called_once()


@pytest.mark.asyncio
async def test_wait_for_sidecar_returns_immediately_on_499():
    """Returns immediately for status 499 — the boundary just below 500."""
    mock_cm, mock_client = _make_mock_async_client([_resp(499)])

    with patch.dict(os.environ, {"FRONTEND_HEALTH_URL": "http://sidecar/healthz"}):
        with patch("httpx.AsyncClient", return_value=mock_cm):
            with patch("asyncio.sleep", new_callable=AsyncMock):
                await _wait_for_sidecar()

    mock_client.get.assert_called_once()


@pytest.mark.asyncio
async def test_wait_for_sidecar_does_not_sleep_when_first_response_is_healthy():
    """No asyncio.sleep call when the sidecar replies with a non-5xx on the first try."""
    mock_cm, _ = _make_mock_async_client([_resp(200)])
    sleep_mock = AsyncMock()

    with patch.dict(os.environ, {"FRONTEND_HEALTH_URL": "http://sidecar/healthz"}):
        with patch("httpx.AsyncClient", return_value=mock_cm):
            with patch("asyncio.sleep", sleep_mock):
                await _wait_for_sidecar()

    sleep_mock.assert_not_called()


# ---------------------------------------------------------------------------
# (3) Retries on 5xx then succeeds
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_wait_for_sidecar_retries_once_after_500():
    """Makes exactly two HTTP requests when the first returns 500 and second 200."""
    mock_cm, mock_client = _make_mock_async_client([_resp(500), _resp(200)])
    sleep_mock = AsyncMock()

    with patch.dict(os.environ, {"FRONTEND_HEALTH_URL": "http://sidecar/healthz"}):
        with patch("httpx.AsyncClient", return_value=mock_cm):
            with patch("asyncio.sleep", sleep_mock):
                await _wait_for_sidecar()

    assert mock_client.get.call_count == 2
    sleep_mock.assert_called_once_with(2)


@pytest.mark.asyncio
async def test_wait_for_sidecar_retries_on_503():
    """Retries after a 503 response and succeeds on the next attempt."""
    mock_cm, mock_client = _make_mock_async_client([_resp(503), _resp(200)])
    sleep_mock = AsyncMock()

    with patch.dict(os.environ, {"FRONTEND_HEALTH_URL": "http://sidecar/healthz"}):
        with patch("httpx.AsyncClient", return_value=mock_cm):
            with patch("asyncio.sleep", sleep_mock):
                await _wait_for_sidecar()

    assert mock_client.get.call_count == 2
    sleep_mock.assert_called_once_with(2)


@pytest.mark.asyncio
async def test_wait_for_sidecar_retries_multiple_5xx_responses():
    """Retries across three 5xx responses before a successful 200."""
    mock_cm, mock_client = _make_mock_async_client(
        [_resp(500), _resp(502), _resp(503), _resp(200)]
    )
    sleep_mock = AsyncMock()

    with patch.dict(os.environ, {"FRONTEND_HEALTH_URL": "http://sidecar/healthz"}):
        with patch("httpx.AsyncClient", return_value=mock_cm):
            with patch("asyncio.sleep", sleep_mock):
                await _wait_for_sidecar()

    assert mock_client.get.call_count == 4
    assert sleep_mock.call_count == 3


@pytest.mark.asyncio
async def test_wait_for_sidecar_sleeps_2s_between_5xx_retries():
    """Each retry after a 5xx sleeps exactly 2 seconds before the next attempt."""
    mock_cm, _ = _make_mock_async_client([_resp(500), _resp(200)])
    sleep_mock = AsyncMock()

    with patch.dict(os.environ, {"FRONTEND_HEALTH_URL": "http://sidecar/healthz"}):
        with patch("httpx.AsyncClient", return_value=mock_cm):
            with patch("asyncio.sleep", sleep_mock):
                await _wait_for_sidecar()

    sleep_mock.assert_called_once_with(2)


# ---------------------------------------------------------------------------
# (4) Retries on httpx.HTTPError then succeeds
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_wait_for_sidecar_retries_on_connect_error():
    """Retries when httpx.ConnectError is raised and succeeds on next attempt."""
    import httpx

    mock_cm, mock_client = _make_mock_async_client(
        [httpx.ConnectError("connection refused"), _resp(200)]
    )
    sleep_mock = AsyncMock()

    with patch.dict(os.environ, {"FRONTEND_HEALTH_URL": "http://sidecar/healthz"}):
        with patch("httpx.AsyncClient", return_value=mock_cm):
            with patch("asyncio.sleep", sleep_mock):
                await _wait_for_sidecar()

    assert mock_client.get.call_count == 2
    sleep_mock.assert_called_once_with(2)


@pytest.mark.asyncio
async def test_wait_for_sidecar_retries_on_read_timeout():
    """Retries when httpx.ReadTimeout is raised and succeeds on next attempt."""
    import httpx

    mock_cm, mock_client = _make_mock_async_client(
        [httpx.ReadTimeout("timed out"), _resp(200)]
    )
    sleep_mock = AsyncMock()

    with patch.dict(os.environ, {"FRONTEND_HEALTH_URL": "http://sidecar/healthz"}):
        with patch("httpx.AsyncClient", return_value=mock_cm):
            with patch("asyncio.sleep", sleep_mock):
                await _wait_for_sidecar()

    assert mock_client.get.call_count == 2
    sleep_mock.assert_called_once_with(2)


@pytest.mark.asyncio
async def test_wait_for_sidecar_retries_multiple_http_errors():
    """Retries across multiple HTTPError subclasses before succeeding."""
    import httpx

    mock_cm, mock_client = _make_mock_async_client(
        [
            httpx.ConnectError("refused"),
            httpx.ReadTimeout("timeout"),
            _resp(200),
        ]
    )
    sleep_mock = AsyncMock()

    with patch.dict(os.environ, {"FRONTEND_HEALTH_URL": "http://sidecar/healthz"}):
        with patch("httpx.AsyncClient", return_value=mock_cm):
            with patch("asyncio.sleep", sleep_mock):
                await _wait_for_sidecar()

    assert mock_client.get.call_count == 3
    assert sleep_mock.call_count == 2


@pytest.mark.asyncio
async def test_wait_for_sidecar_retries_mixed_errors_and_5xx():
    """Retries seamlessly across a mix of HTTPErrors and 5xx responses."""
    import httpx

    mock_cm, mock_client = _make_mock_async_client(
        [
            httpx.ConnectError("refused"),
            _resp(503),
            _resp(200),
        ]
    )
    sleep_mock = AsyncMock()

    with patch.dict(os.environ, {"FRONTEND_HEALTH_URL": "http://sidecar/healthz"}):
        with patch("httpx.AsyncClient", return_value=mock_cm):
            with patch("asyncio.sleep", sleep_mock):
                await _wait_for_sidecar()

    assert mock_client.get.call_count == 3
    assert sleep_mock.call_count == 2


# ---------------------------------------------------------------------------
# _wait_for_sidecar() — URL forwarding and logging
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_wait_for_sidecar_passes_correct_url_to_get():
    """client.get() is called with the URL from FRONTEND_HEALTH_URL."""
    test_url = "http://my-sidecar:9000/live"
    mock_cm, mock_client = _make_mock_async_client([_resp(200)])

    with patch.dict(os.environ, {"FRONTEND_HEALTH_URL": test_url}):
        with patch("httpx.AsyncClient", return_value=mock_cm):
            with patch("asyncio.sleep", new_callable=AsyncMock):
                await _wait_for_sidecar()

    called_url = mock_client.get.call_args.args[0]
    assert called_url == test_url


@pytest.mark.asyncio
async def test_wait_for_sidecar_logs_waiting_message():
    """Logs an informational 'waiting' message before polling starts."""
    mock_cm, _ = _make_mock_async_client([_resp(200)])

    with patch.dict(os.environ, {"FRONTEND_HEALTH_URL": "http://sidecar/healthz"}):
        with patch("httpx.AsyncClient", return_value=mock_cm):
            with patch("asyncio.sleep", new_callable=AsyncMock):
                with patch("chat.leader.logger") as mock_logger:
                    await _wait_for_sidecar()

    messages = [str(c) for c in mock_logger.info.call_args_list]
    assert any("Waiting for frontend sidecar" in m for m in messages)


@pytest.mark.asyncio
async def test_wait_for_sidecar_logs_ready_message():
    """Logs a 'ready' message after the sidecar returns a non-5xx response."""
    mock_cm, _ = _make_mock_async_client([_resp(200)])

    with patch.dict(os.environ, {"FRONTEND_HEALTH_URL": "http://sidecar/healthz"}):
        with patch("httpx.AsyncClient", return_value=mock_cm):
            with patch("asyncio.sleep", new_callable=AsyncMock):
                with patch("chat.leader.logger") as mock_logger:
                    await _wait_for_sidecar()

    messages = [str(c) for c in mock_logger.info.call_args_list]
    assert any("Frontend sidecar is ready" in m for m in messages)


# ---------------------------------------------------------------------------
# (5) _start_bot_when_ready calls _wait_for_sidecar then bot.start
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_start_bot_when_ready_calls_wait_for_sidecar_before_bot_start():
    """_start_bot_when_ready awaits _wait_for_sidecar before calling bot.start."""
    from app.main import _start_singletons, app

    call_order = []

    async def _mock_wait_for_sidecar():
        call_order.append("_wait_for_sidecar")

    mock_bot = MagicMock()
    mock_bot.close = AsyncMock()

    async def _mock_bot_start(token):  # noqa: ARG001
        call_order.append("bot.start")

    mock_bot.start = _mock_bot_start

    mock_chat_module = MagicMock()
    mock_chat_module.create_bot.return_value = mock_bot

    # Capture the first coroutine passed to create_task (bot=1)
    captured_bot_coro: list = []
    task_counter = [0]

    def capture_create_task(coro, **kwargs):
        task_counter[0] += 1
        if task_counter[0] == 1:
            # Task 1 is the bot coroutine — capture it for sequencing test
            captured_bot_coro.append(coro)
        else:
            # Drain scheduler (2) and sweep (3) coroutines
            if hasattr(coro, "close"):
                coro.close()
        return MagicMock()

    patches = _lifespan_patches_with_discord(mock_bot)
    with (
        patch.dict(os.environ, {"DISCORD_BOT_TOKEN": "fake-token-for-test"}),
        patch.dict(sys.modules, {"chat.bot": mock_chat_module}),
        patch("asyncio.create_task", side_effect=capture_create_task),
        patch("chat.leader.wait_for_sidecar", side_effect=_mock_wait_for_sidecar),
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

        assert len(captured_bot_coro) == 1, (
            "Expected exactly one bot coroutine captured from create_task"
        )
        # Actually run _start_bot_when_ready to verify sequencing
        await captured_bot_coro[0]

    assert call_order == ["_wait_for_sidecar", "bot.start"], (
        f"Expected _wait_for_sidecar before bot.start; got: {call_order}"
    )


@pytest.mark.asyncio
async def test_start_bot_when_ready_not_scheduled_when_no_token():
    """When DISCORD_BOT_TOKEN is absent, no bot task (and thus no sidecar wait) is created."""
    from app.main import _start_singletons, app

    created_tasks: list = []

    def capture_create_task(coro, **kwargs):
        if hasattr(coro, "close"):
            coro.close()
        t = MagicMock()
        created_tasks.append(t)
        return t

    env_without_token = {
        k: v for k, v in os.environ.items() if k != "DISCORD_BOT_TOKEN"
    }

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
        await _start_singletons(app)

    # Ships ingest plus the agent_sessions sweep and title refresh (no bot;
    # scheduler loop removed).
    # 4 not 3: cd-probe (cluster) writes the platform_probe latch, see core/platform_probe.py
    assert len(created_tasks) == 4, (
        f"Expected 4 tasks without a bot token, got {len(created_tasks)}"
    )
