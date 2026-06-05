"""Tests for the Temporal worker skeleton (hermetic — no real Temporal).

``temporalio.worker.Worker`` and ``get_client`` are mocked so nothing connects.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

from projects.lakehouse.orchestrator import TaskQueue
from projects.lakehouse.orchestrator import worker as worker_module
from projects.lakehouse.orchestrator.worker import discover_workflows, run_worker


def test_discover_workflows_stub_returns_empty() -> None:
    assert discover_workflows() == []


def test_run_worker_constructs_worker_with_task_queue(monkeypatch) -> None:
    mock_client = MagicMock(name="client")
    mock_get_client = AsyncMock(return_value=mock_client)
    monkeypatch.setattr(worker_module, "get_client", mock_get_client)

    mock_worker = MagicMock(name="worker")
    mock_worker.run = AsyncMock()
    mock_worker_cls = MagicMock(return_value=mock_worker)
    monkeypatch.setattr(worker_module.temporalio.worker, "Worker", mock_worker_cls)

    asyncio.run(run_worker(TaskQueue.GAP_DRAIN))

    # Connected via get_client (no client injected).
    mock_get_client.assert_awaited_once()

    # Worker built with the given task queue and empty registrations.
    mock_worker_cls.assert_called_once()
    args, kwargs = mock_worker_cls.call_args
    assert args[0] is mock_client
    assert kwargs["task_queue"] == TaskQueue.GAP_DRAIN
    assert kwargs["task_queue"] == "gap-drain"
    assert kwargs["workflows"] == []
    assert kwargs["activities"] == []

    mock_worker.run.assert_awaited_once()


def test_run_worker_uses_injected_client_and_registrations(monkeypatch) -> None:
    mock_get_client = AsyncMock()
    monkeypatch.setattr(worker_module, "get_client", mock_get_client)

    mock_worker = MagicMock()
    mock_worker.run = AsyncMock()
    mock_worker_cls = MagicMock(return_value=mock_worker)
    monkeypatch.setattr(worker_module.temporalio.worker, "Worker", mock_worker_cls)

    injected_client = MagicMock(name="injected")

    class _WorkflowA:
        pass

    def _activity_b() -> None: ...

    asyncio.run(
        run_worker(
            "housekeeping",
            workflows=[_WorkflowA],
            activities=[_activity_b],
            client=injected_client,
        )
    )

    # Injected client short-circuits get_client.
    mock_get_client.assert_not_awaited()

    _, kwargs = mock_worker_cls.call_args
    assert mock_worker_cls.call_args.args[0] is injected_client
    assert kwargs["task_queue"] == "housekeeping"
    assert kwargs["workflows"] == [_WorkflowA]
    assert kwargs["activities"] == [_activity_b]
    mock_worker.run.assert_awaited_once()
