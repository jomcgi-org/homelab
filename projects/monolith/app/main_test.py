"""Unit tests for app.main — /healthz endpoint, router registration, static mount,
and lifespan background-task lifecycle.

IMPORTANT: STATIC_DIR must be unset (or point to a non-existent path) *before*
this module is imported so that we can test the "directory missing" code path.
The StaticFiles conditional mount runs at module-import time in app/main.py.
"""

import asyncio
import os
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, SQLModel, create_engine
from sqlmodel.pool import StaticPool

# Ensure no valid static directory is set so the conditional mount is skipped.
os.environ.pop("STATIC_DIR", None)

from core.db import get_session  # noqa: E402
from app.main import _start_singletons, _stop_singletons, app  # noqa: E402


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(name="session")
def session_fixture():
    """In-memory SQLite session with schema stripped (SQLite has no schemas)."""
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    original_schemas = {}
    for table in SQLModel.metadata.tables.values():
        if table.schema is not None:
            original_schemas[table.name] = table.schema
            table.schema = None

    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        yield session

    for table in SQLModel.metadata.tables.values():
        if table.name in original_schemas:
            table.schema = original_schemas[table.name]


@pytest.fixture(name="client")
def client_fixture(session):
    """TestClient with the DB dependency overridden to use in-memory SQLite."""

    def get_session_override():
        yield session

    app.dependency_overrides[get_session] = get_session_override
    client = TestClient(app, raise_server_exceptions=False)
    yield client
    app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# /healthz endpoint
# ---------------------------------------------------------------------------


def test_healthz_returns_ok(client):
    """GET /healthz returns HTTP 200 with {"status": "ok"}."""
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_healthz_content_type_is_json(client):
    """GET /healthz response Content-Type is application/json."""
    response = client.get("/healthz")
    assert "application/json" in response.headers["content-type"]


# ---------------------------------------------------------------------------
# Router registration
# ---------------------------------------------------------------------------


def _iter_route_paths(routes):
    """Yield every route ``.path``, recursing into included/mounted sub-routers.

    Starlette 1.x no longer flattens ``app.include_router()`` output into
    ``app.routes``: each included router appears as a single ``_IncludedRouter``
    wrapper with no ``.path``, and its child ``APIRoute`` objects (which carry
    the full, prefix-resolved ``.path``) live on ``wrapper.original_router.routes``.
    ``Mount`` sub-apps expose their own mount path (e.g. ``/mcp``) but are not
    descended into, matching the old flattened 0.x behaviour (which only ever
    saw the mount point, never the sub-app's internal, un-prefixed routes).
    """
    from starlette.routing import Mount

    for route in routes:
        path = getattr(route, "path", None)
        if path is not None:
            yield path
        if isinstance(route, Mount):
            continue
        sub = getattr(route, "routes", None)
        if sub is None:
            orig = getattr(route, "original_router", None)
            sub = getattr(orig, "routes", None) if orig is not None else None
        if sub:
            yield from _iter_route_paths(sub)


def test_schedule_router_registered():
    """Schedule router is included — routes with /api/home prefix exist."""
    paths = list(_iter_route_paths(app.routes))
    assert any(p.startswith("/api/home") for p in paths), (
        "No /api/home routes found; home router may not be included"
    )


def test_knowledge_router_registered():
    """Knowledge router is included — routes with /api/knowledge prefix exist in the app."""
    paths = list(_iter_route_paths(app.routes))
    assert any(p.startswith("/api/knowledge") for p in paths), (
        "No /api/knowledge routes found; knowledge_router may not be included"
    )


def test_schedule_router_today_endpoint_responds(client):
    """GET /api/home/schedule/today from the home router returns a 200 response."""
    response = client.get("/api/home/schedule/today")
    assert response.status_code == 200


# ---------------------------------------------------------------------------
# Static directory mount — "dir missing" behaviour
# ---------------------------------------------------------------------------


def test_no_frontend_mount_when_static_dir_missing():
    """When STATIC_DIR doesn't point to an existing directory, no static mount is added."""
    # The module was imported without a valid STATIC_DIR (see module-level setup).
    # Verify there is no route named "frontend".
    frontend_mount = next(
        (r for r in app.routes if getattr(r, "name", None) == "frontend"),
        None,
    )
    assert frontend_mount is None, (
        "StaticFiles mount 'frontend' was unexpectedly added to the app"
    )


def test_api_routes_still_work_without_static_dir(client):
    """/healthz responds even when the static frontend directory is absent."""
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_unknown_path_returns_404_without_static_dir(client):
    """Without a catch-all static mount, an unknown path returns 404."""
    response = client.get("/nonexistent-page.html")
    assert response.status_code == 404


# ---------------------------------------------------------------------------
# lifespan() context manager — background task lifecycle
# ---------------------------------------------------------------------------


def _lifespan_patches_no_discord():
    """Return a list of context-manager patches needed for lifespan without discord token."""
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


@pytest.mark.asyncio
async def test_lifespan_creates_one_background_task_on_startup():
    """Without discord, the leader creates a single task (ships ingest); the
    scheduler dispatch loop was removed - batch jobs run as Argo CronWorkflows."""
    from app.main import lifespan

    created_tasks = []

    def capture_create_task(coro, **kwargs):
        if hasattr(coro, "close"):
            coro.close()
        mock_task = MagicMock()
        created_tasks.append(mock_task)
        return mock_task

    patches = _lifespan_patches_no_discord()
    with patch("asyncio.create_task", side_effect=capture_create_task):
        with patches[0], patches[1], patches[2], patches[3], patches[4]:
            await _start_singletons(app)

    assert (
        len(created_tasks) == 4
    )  # ships + agent_sessions sweep + title refresh + cd probe


@pytest.mark.asyncio
async def test_lifespan_cancels_all_tasks_on_shutdown():
    """Background task is cancelled when the lifespan context exits."""
    from app.main import lifespan

    mock_tasks = []

    def capture_create_task(coro, **kwargs):
        if hasattr(coro, "close"):
            coro.close()
        mock_task = MagicMock()
        mock_tasks.append(mock_task)
        return mock_task

    patches = _lifespan_patches_no_discord()
    with patch("asyncio.create_task", side_effect=capture_create_task):
        with patches[0], patches[1], patches[2], patches[3], patches[4]:
            await _start_singletons(app)
            await _stop_singletons(app)

    assert (
        len(mock_tasks) == 4
    )  # ships + agent_sessions sweep + title refresh + cd probe
    for task in mock_tasks:
        task.cancel.assert_called_once()


@pytest.mark.asyncio
async def test_lifespan_no_tasks_cancelled_before_shutdown():
    """Tasks are created but not cancelled until the lifespan context manager exits."""
    from app.main import lifespan

    mock_tasks = []

    def capture_create_task(coro, **kwargs):
        if hasattr(coro, "close"):
            coro.close()
        mock_task = MagicMock()
        mock_tasks.append(mock_task)
        return mock_task

    patches = _lifespan_patches_no_discord()
    with patch("asyncio.create_task", side_effect=capture_create_task):
        with patches[0], patches[1], patches[2], patches[3], patches[4]:
            await _start_singletons(app)
            assert (
                len(mock_tasks) == 4
            )  # ships + agent_sessions sweep + title refresh + cd probe
            for task in mock_tasks:
                task.cancel.assert_not_called()
            await _stop_singletons(app)

    # After _stop_singletons, every task must have been cancelled
    for task in mock_tasks:
        task.cancel.assert_called_once()


# ---------------------------------------------------------------------------
# _log_task_exception() — background-task error callback
# ---------------------------------------------------------------------------


def test_log_task_exception_does_not_log_when_task_cancelled():
    """_log_task_exception silently ignores cancelled tasks."""
    from app.main import _log_task_exception

    mock_task = MagicMock()
    mock_task.cancelled.return_value = True

    with patch("framework.core.logger") as mock_logger:
        _log_task_exception(mock_task)

    mock_logger.error.assert_not_called()


def test_log_task_exception_logs_error_when_exception_present():
    """_log_task_exception logs an error (with exc_info) when the task raised."""
    from app.main import _log_task_exception

    exc = ValueError("boom")
    mock_task = MagicMock()
    mock_task.cancelled.return_value = False
    mock_task.exception.return_value = exc
    mock_task.get_name.return_value = "my-task"

    with patch("framework.core.logger") as mock_logger:
        _log_task_exception(mock_task)

    mock_logger.error.assert_called_once()
    call_kwargs = mock_logger.error.call_args[1]
    assert call_kwargs.get("exc_info") is exc


def test_log_task_exception_does_not_log_when_task_succeeded():
    """_log_task_exception is silent for tasks that finished without an exception."""
    from app.main import _log_task_exception

    mock_task = MagicMock()
    mock_task.cancelled.return_value = False
    mock_task.exception.return_value = None

    with patch("framework.core.logger") as mock_logger:
        _log_task_exception(mock_task)

    mock_logger.error.assert_not_called()


def test_log_task_exception_includes_task_name_in_error_message():
    """_log_task_exception includes the task name in the logged error message."""
    from app.main import _log_task_exception

    exc = RuntimeError("task failed")
    mock_task = MagicMock()
    mock_task.cancelled.return_value = False
    mock_task.exception.return_value = exc
    mock_task.get_name.return_value = "important-task"

    with patch("framework.core.logger") as mock_logger:
        _log_task_exception(mock_task)

    call_args = mock_logger.error.call_args[0]
    # The format string is the first arg; the task name is the second
    assert "important-task" in call_args[1]


# ---------------------------------------------------------------------------
# lifespan() — startup and shutdown log messages
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_lifespan_logs_monolith_started_on_startup():
    """Lifespan logs 'Monolith started' after background tasks are created."""
    from app.main import lifespan

    def capture_create_task(coro, **kwargs):
        if hasattr(coro, "close"):
            coro.close()
        return MagicMock()

    patches = _lifespan_patches_no_discord()
    with patch("asyncio.create_task", side_effect=capture_create_task):
        with patches[0], patches[1], patches[2], patches[3], patches[4]:
            with patch("framework.core.logger") as mock_logger:
                async with lifespan(app):
                    pass

    logged_messages = [str(c) for c in mock_logger.info.call_args_list]
    assert any("Monolith started" in m for m in logged_messages), (
        "Expected 'Monolith started' to be logged during lifespan startup"
    )


@pytest.mark.asyncio
async def test_lifespan_logs_shutting_down_on_exit():
    """Lifespan logs 'Monolith shutting down' when the context exits."""
    from app.main import lifespan

    def capture_create_task(coro, **kwargs):
        if hasattr(coro, "close"):
            coro.close()
        return MagicMock()

    patches = _lifespan_patches_no_discord()
    with patch("asyncio.create_task", side_effect=capture_create_task):
        with patches[0], patches[1], patches[2], patches[3], patches[4]:
            with patch("framework.core.logger") as mock_logger:
                async with lifespan(app):
                    pass

    logged_messages = [str(c) for c in mock_logger.info.call_args_list]
    assert any("Monolith shutting down" in m for m in logged_messages), (
        "Expected 'Monolith shutting down' to be logged during lifespan teardown"
    )


# ---------------------------------------------------------------------------
# lifespan() — Discord bot integration (token present)
# ---------------------------------------------------------------------------


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


@pytest.mark.asyncio
async def test_lifespan_registers_done_callback_on_bot_task_when_token_set():
    """When DISCORD_BOT_TOKEN is set, bot_task.add_done_callback(_log_task_exception) is called."""
    from app.main import lifespan, _log_task_exception

    mock_bot = MagicMock()
    mock_bot.close = AsyncMock()

    bot_task_mock = MagicMock()
    task_counter = [0]

    def capture_create_task(coro, **kwargs):
        if hasattr(coro, "close"):
            coro.close()
        task_counter[0] += 1
        # Tasks created: 1=bot, 2=scheduler, 3=ships ingest, 4=sweep
        if task_counter[0] == 1:
            return bot_task_mock
        return MagicMock()

    patches = _lifespan_patches_with_discord(mock_bot)
    with patch.dict(os.environ, {"DISCORD_BOT_TOKEN": "fake-test-token"}):
        with patch("asyncio.create_task", side_effect=capture_create_task):
            with (
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

    bot_task_mock.add_done_callback.assert_called_once_with(_log_task_exception)


@pytest.mark.asyncio
async def test_lifespan_logs_discord_bot_starting_when_token_set():
    """When DISCORD_BOT_TOKEN is set, 'Discord bot starting' is logged."""
    from app.main import lifespan

    mock_bot = MagicMock()
    mock_bot.close = AsyncMock()

    def capture_create_task(coro, **kwargs):
        if hasattr(coro, "close"):
            coro.close()
        return MagicMock()

    patches = _lifespan_patches_with_discord(mock_bot)
    with patch.dict(os.environ, {"DISCORD_BOT_TOKEN": "fake-test-token"}):
        with patch("asyncio.create_task", side_effect=capture_create_task):
            with (
                patches[0],
                patches[1],
                patches[2],
                patches[3],
                patches[4],
                patches[5],
                patches[6],
                patches[7],
            ):
                with patch("chat.leader.logger") as mock_logger:
                    await _start_singletons(app)

    logged_messages = [str(c) for c in mock_logger.info.call_args_list]
    assert any("Discord bot starting" in m for m in logged_messages), (
        "Expected 'Discord bot starting' to be logged when DISCORD_BOT_TOKEN is set"
    )


@pytest.mark.asyncio
async def test_lifespan_creates_four_tasks_when_discord_token_set():
    """When DISCORD_BOT_TOKEN is set, the leader creates four tasks (bot, outbox
    drain, ships ingest, sweep). The scheduler dispatch loop was removed."""
    from app.main import lifespan

    mock_bot = MagicMock()
    mock_bot.close = AsyncMock()

    created_tasks = []

    def capture_create_task(coro, **kwargs):
        if hasattr(coro, "close"):
            coro.close()
        task = MagicMock()
        created_tasks.append(task)
        return task

    patches = _lifespan_patches_with_discord(mock_bot)
    with patch.dict(os.environ, {"DISCORD_BOT_TOKEN": "fake-test-token"}):
        with patch("asyncio.create_task", side_effect=capture_create_task):
            with (
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

    assert (
        len(created_tasks) == 7
    )  # bot + outbox + ships + agent_sessions + lock sweeps + title refresh


@pytest.mark.asyncio
async def test_lifespan_does_not_log_discord_bot_starting_when_token_absent():
    """When DISCORD_BOT_TOKEN is absent, 'Discord bot starting' is NOT logged."""
    from app.main import lifespan

    env_without_token = {
        k: v for k, v in os.environ.items() if k != "DISCORD_BOT_TOKEN"
    }

    def capture_create_task(coro, **kwargs):
        if hasattr(coro, "close"):
            coro.close()
        return MagicMock()

    patches = _lifespan_patches_no_discord()
    with patch.dict(os.environ, env_without_token, clear=True):
        with patch("asyncio.create_task", side_effect=capture_create_task):
            with patches[0], patches[1], patches[2], patches[3], patches[4]:
                with patch("chat.leader.logger") as mock_logger:
                    async with lifespan(app):
                        pass

    logged_messages = [str(c) for c in mock_logger.info.call_args_list]
    assert not any("Discord bot starting" in m for m in logged_messages), (
        "'Discord bot starting' should not be logged when no DISCORD_BOT_TOKEN is set"
    )
