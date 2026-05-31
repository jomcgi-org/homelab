"""Tests for the worker entrypoint composition (``workflows/run.py``).

The entrypoint owns "seed the Temporal Schedules, then run a worker" — the
composition that used to (wrongly) live in the worker skeleton and formed an
``orchestrator -> schedules -> orchestrator`` dependency cycle. These tests pin
the contract: schedules are registered against the *same* client the worker
uses, registration is best-effort (a failure must not stop the worker), and a
missing ``TASK_QUEUE`` aborts before touching the cluster.

Driven via ``asyncio.run`` (not ``@pytest.mark.asyncio``) so they execute
without the pytest-asyncio plugin, which is not yet wired into this harness.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from projects.lakehouse.orchestrator.workflows import run as run_module


def _patch_entrypoint(monkeypatch, *, register=None):
    """Patch run.py's collaborators so ``_run`` drives without a real Temporal.

    Returns ``(client, register_mock, run_worker_mock)``. ``register`` lets a
    test inject a failing registration (side_effect) while keeping the rest.
    """
    monkeypatch.setenv("TASK_QUEUE", "gap-drain")

    client = MagicMock(name="client")
    monkeypatch.setattr(run_module, "get_client", AsyncMock(return_value=client))

    register_mock = register if register is not None else AsyncMock()
    monkeypatch.setattr(run_module, "register_schedules", register_mock)

    run_worker_mock = AsyncMock()
    monkeypatch.setattr(run_module, "run_worker", run_worker_mock)

    # Keep discovery hermetic — don't walk/import real workflow modules.
    monkeypatch.setattr(run_module, "discover_workflows", lambda: [])
    monkeypatch.setattr(run_module, "discover_activities", lambda: [])

    return client, register_mock, run_worker_mock


def test_run_seeds_schedules_and_injects_shared_client(monkeypatch) -> None:
    client, register_mock, run_worker_mock = _patch_entrypoint(monkeypatch)

    asyncio.run(run_module._run())

    # Schedules registered against the connected client...
    register_mock.assert_awaited_once_with(client)
    # ...and the worker runs the queue with that SAME client injected (so it is
    # not connected twice).
    run_worker_mock.assert_awaited_once()
    args, kwargs = run_worker_mock.await_args
    assert args[0] == "gap-drain"
    assert kwargs["client"] is client


def test_run_survives_schedule_registration_failure(monkeypatch) -> None:
    # Best-effort seeding: a Temporal hiccup at boot is logged, not fatal — the
    # worker must still serve its queue (crash-looping the pool is strictly
    # worse than running with already-registered schedules un-refreshed).
    failing = AsyncMock(side_effect=RuntimeError("temporal unavailable"))
    _, register_mock, run_worker_mock = _patch_entrypoint(monkeypatch, register=failing)

    asyncio.run(run_module._run())  # must not raise

    register_mock.assert_awaited_once()
    run_worker_mock.assert_awaited_once()


def test_run_requires_task_queue(monkeypatch) -> None:
    monkeypatch.delenv("TASK_QUEUE", raising=False)
    # Patch collaborators so a regression that reorders the check can't silently
    # connect/seed before raising.
    get_client = AsyncMock()
    register_mock = AsyncMock()
    run_worker_mock = AsyncMock()
    monkeypatch.setattr(run_module, "get_client", get_client)
    monkeypatch.setattr(run_module, "register_schedules", register_mock)
    monkeypatch.setattr(run_module, "run_worker", run_worker_mock)

    with pytest.raises(SystemExit):
        asyncio.run(run_module._run())

    get_client.assert_not_awaited()
    register_mock.assert_not_awaited()
    run_worker_mock.assert_not_awaited()
