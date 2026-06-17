"""Tests that lifespan calls clone_vault on startup."""

from __future__ import annotations

import os
from unittest.mock import AsyncMock, patch

import pytest

os.environ.pop("STATIC_DIR", None)


@pytest.mark.asyncio
async def test_lifespan_calls_clone_vault():
    """Lifespan calls clone_vault() before starting the scheduler."""
    mock_clone = AsyncMock()
    with (
        patch("knowledge.service.clone_vault", mock_clone),
        patch("app.main._wait_for_sidecar", new_callable=AsyncMock),
        patch("scheduler.api.run_scheduler_loop", new_callable=AsyncMock),
        patch("app.db.get_engine"),
        patch("knowledge.service.on_startup"),
        patch("home.on_startup_jobs"),
        patch("ships.on_startup_jobs"),
        patch("hikes.on_startup_jobs"),
        patch("stars.on_startup_jobs"),
        patch("dr_jobs.on_startup_jobs"),
        patch("home.observability.rollup.register"),
        patch("app.main.prime_snapshots", new_callable=AsyncMock),
    ):
        from app.main import lifespan, app

        async with lifespan(app):
            pass
    mock_clone.assert_awaited_once()
