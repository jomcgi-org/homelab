"""Tests for the lakehouse Temporal schedule definitions (hermetic — no Temporal).

Discovery and spec-shape assertions never connect to a server.
``register_schedules`` is exercised against a mocked client: it must call
``create_schedule`` once per discovered spec and swallow
``ScheduleAlreadyRunningError`` so boot-time registration is idempotent.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
from unittest import mock

import pytest
import temporalio.client

from projects.lakehouse.orchestrator import TaskQueue
from projects.lakehouse.orchestrator import schedules as sched

# Expected (schedule_id, workflow_type, task_queue) for each shipped module.
_EXPECTED = {
    "gap-drain-sweep": ("GapDrainSweepWorkflow", TaskQueue.GAP_DRAIN.value),
    "iceberg-batch-commit": (
        "IcebergBatchCommitWorkflow",
        TaskQueue.ICEBERG_BUILDER.value,
    ),
    "build-serving-artifact": (
        "BuildServingArtifactWorkflow",
        TaskQueue.HOUSEKEEPING.value,
    ),
    "tag-rotation": ("TagRotationWorkflow", TaskQueue.HOUSEKEEPING.value),
}


def _by_id() -> dict[str, sched.ScheduleDefinition]:
    return {d.schedule_id: d for d in sched.all_schedules()}


def test_all_schedules_returns_list() -> None:
    assert isinstance(sched.all_schedules(), list)


def test_discovers_all_four_schedules() -> None:
    found = _by_id()
    assert set(found) == set(_EXPECTED)
    # No duplicate schedule IDs across modules.
    assert len(sched.all_schedules()) == len(_EXPECTED)


def test_each_definition_is_a_schedule_definition() -> None:
    for definition in sched.all_schedules():
        assert isinstance(definition, sched.ScheduleDefinition)
        assert isinstance(definition.schedule, temporalio.client.Schedule)


@pytest.mark.parametrize(
    ("schedule_id", "workflow_type", "task_queue"),
    [(sid, wf, tq) for sid, (wf, tq) in _EXPECTED.items()],
)
def test_spec_targets_expected_workflow_and_queue(
    schedule_id: str, workflow_type: str, task_queue: str
) -> None:
    definition = _by_id()[schedule_id]
    action = definition.schedule.action
    assert isinstance(action, temporalio.client.ScheduleActionStartWorkflow)
    # Workflow referenced by type-name string (not an imported class).
    assert action.workflow == workflow_type
    assert action.task_queue == task_queue
    assert action.id == schedule_id


def test_cron_schedules_use_expected_expressions() -> None:
    found = _by_id()
    assert found["gap-drain-sweep"].schedule.spec.cron_expressions == ["*/5 * * * *"]
    assert found["build-serving-artifact"].schedule.spec.cron_expressions == [
        "*/15 * * * *"
    ]
    assert found["tag-rotation"].schedule.spec.cron_expressions == ["*/15 * * * *"]


def test_iceberg_batch_uses_interval_spec() -> None:
    spec = _by_id()["iceberg-batch-commit"].schedule.spec
    # Interval-based (sub-minute cadence isn't expressible in 5-field cron).
    assert not spec.cron_expressions
    assert len(spec.intervals) == 1
    assert spec.intervals[0].every.total_seconds() == 90


def test_register_schedules_creates_each_spec() -> None:
    client = mock.Mock()
    client.create_schedule = mock.AsyncMock()

    asyncio.run(sched.register_schedules(client))

    created_ids = {call.args[0] for call in client.create_schedule.call_args_list}
    assert created_ids == set(_EXPECTED)
    assert client.create_schedule.await_count == len(_EXPECTED)
    # Each call passes the corresponding Schedule object positionally.
    for call in client.create_schedule.call_args_list:
        assert isinstance(call.args[1], temporalio.client.Schedule)


def test_register_schedules_swallows_already_running() -> None:
    # Every create raises AlreadyRunning (schedules pre-exist from a prior boot);
    # register_schedules must complete without propagating — that's idempotency.
    client = mock.Mock()
    client.create_schedule = mock.AsyncMock(
        side_effect=temporalio.client.ScheduleAlreadyRunningError()
    )

    asyncio.run(sched.register_schedules(client))

    assert client.create_schedule.await_count == len(_EXPECTED)


def test_register_schedules_propagates_unexpected_errors() -> None:
    client = mock.Mock()
    client.create_schedule = mock.AsyncMock(side_effect=RuntimeError("boom"))

    with pytest.raises(RuntimeError, match="boom"):
        asyncio.run(sched.register_schedules(client))


# --- per-module constants ------------------------------------------------


def test_build_serving_module_constants() -> None:
    from projects.lakehouse.orchestrator.schedules import build_serving

    assert build_serving.WORKFLOW_TYPE == "BuildServingArtifactWorkflow"
    assert build_serving.SCHEDULE_ID == "build-serving-artifact"
    assert build_serving.CRON == "*/15 * * * *"


def test_gap_drain_sweep_module_constants() -> None:
    from projects.lakehouse.orchestrator.schedules import gap_drain_sweep

    assert gap_drain_sweep.WORKFLOW_TYPE == "GapDrainSweepWorkflow"
    assert gap_drain_sweep.SCHEDULE_ID == "gap-drain-sweep"
    assert gap_drain_sweep.CRON == "*/5 * * * *"


def test_iceberg_batch_module_constants() -> None:
    from projects.lakehouse.orchestrator.schedules import iceberg_batch

    assert iceberg_batch.WORKFLOW_TYPE == "IcebergBatchCommitWorkflow"
    assert iceberg_batch.SCHEDULE_ID == "iceberg-batch-commit"
    assert iceberg_batch.INTERVAL == timedelta(seconds=90)


def test_tag_rotation_module_constants() -> None:
    from projects.lakehouse.orchestrator.schedules import tag_rotation

    assert tag_rotation.WORKFLOW_TYPE == "TagRotationWorkflow"
    assert tag_rotation.SCHEDULE_ID == "tag-rotation"
    assert tag_rotation.CRON == "*/15 * * * *"


def test_each_module_exports_exactly_one_schedule() -> None:
    """Each schedule module exports a SCHEDULES list with exactly one entry."""
    from projects.lakehouse.orchestrator.schedules import (
        build_serving,
        gap_drain_sweep,
        iceberg_batch,
        tag_rotation,
    )

    for mod in (build_serving, gap_drain_sweep, iceberg_batch, tag_rotation):
        mod_schedules = getattr(mod, "SCHEDULES", None)
        assert mod_schedules is not None, f"{mod.__name__} missing SCHEDULES"
        assert len(mod_schedules) == 1, f"{mod.__name__} should export exactly one schedule"
        assert isinstance(mod_schedules[0], sched.ScheduleDefinition)
