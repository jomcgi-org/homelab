from fastapi import FastAPI
from fastapi.testclient import TestClient

import swarm.drainer_router as drainer_router


def _client() -> TestClient:
    app = FastAPI()
    app.include_router(drainer_router.router)
    return TestClient(app)


def test_disabled_returns_200(monkeypatch):
    monkeypatch.setattr(drainer_router, "drainer_enabled", lambda: False)

    response = _client().post("/internal/agent/drain")

    assert response.status_code == 200
    assert response.json() == {"status": "disabled"}


def test_dbos_not_launched_returns_503(monkeypatch):
    monkeypatch.setattr(drainer_router, "drainer_enabled", lambda: True)
    monkeypatch.setattr(drainer_router.runtime, "is_launched", lambda: False)

    response = _client().post("/internal/agent/drain")

    assert response.status_code == 503
    assert "not launched" in response.json()["detail"]


class _FakeWorkflow:
    """Fake workflow object that mimics DBOS workflow_id and updated_at."""

    def __init__(self, workflow_id, updated_at):
        self.workflow_id = workflow_id
        self.updated_at = updated_at


def _fake_dbos_no_live_workflows(monkeypatch):
    """Mock DBOS to return no stale or live workflows."""

    class FakeDBOS:
        def list_workflows(
            self,
            name=None,
            queue_name=None,
            status=None,
            limit=None,
            load_input=False,
            load_output=False,
        ):
            # No workflows at all
            return []

        def list_workflow_steps(self, workflow_id, load_output=False):
            return []

        def cancel_workflow(self, workflow_uuid, cancel_children=False):
            pass

    monkeypatch.setattr(drainer_router.runtime, "init_dbos", lambda: FakeDBOS())


def test_launched_enqueues_workflow_on_drainer_queue(monkeypatch):
    enqueued = []

    class FakeQueue:
        def enqueue(self, workflow):
            enqueued.append(workflow)

    _fake_dbos_no_live_workflows(monkeypatch)

    monkeypatch.setattr(drainer_router, "drainer_enabled", lambda: True)
    monkeypatch.setattr(drainer_router.runtime, "is_launched", lambda: True)
    monkeypatch.setattr(drainer_router, "drainer_queue", lambda: FakeQueue())

    response = _client().post("/internal/agent/drain")

    assert response.status_code == 202
    assert response.json() == {"status": "started"}
    assert enqueued == [drainer_router.drain_cycle]


def test_drainer_flag_enables_shared_dbos_runtime(monkeypatch):
    monkeypatch.setattr(drainer_router.runtime.config, "enabled", lambda: False)
    monkeypatch.setenv("DRAINER_ENABLED", "true")

    assert drainer_router.runtime._enabled() is True


def test_stale_pending_reaper_then_enqueue(monkeypatch):
    """A stale PENDING row is cancelled and then a fresh cycle IS enqueued."""
    enqueued = []
    cancelled = []

    class FakeDBOS:
        def list_workflows(
            self,
            name=None,
            queue_name=None,
            status=None,
            limit=None,
            load_input=False,
            load_output=False,
        ):
            # status is a string for PENDING (from reaper), or list for live check
            if status == "PENDING":
                # From _reap_stale_drain_cycles: return one stale workflow
                return [_FakeWorkflow("stale-wf-1", 1000)]
            # From _has_live_drain_cycle (status is a list): no live workflows
            return []

        def list_workflow_steps(self, workflow_id, load_output=False):
            # Workflow has no steps or very old steps
            return []

        def cancel_workflow(self, workflow_uuid, cancel_children=False):
            cancelled.append(workflow_uuid)

    class FakeQueue:
        def enqueue(self, workflow):
            enqueued.append(workflow)

    monkeypatch.setattr(drainer_router, "drainer_enabled", lambda: True)
    monkeypatch.setattr(drainer_router.runtime, "is_launched", lambda: True)
    monkeypatch.setattr(drainer_router.runtime, "init_dbos", lambda: FakeDBOS())
    monkeypatch.setattr(drainer_router, "drainer_queue", lambda: FakeQueue())
    monkeypatch.setattr(drainer_router.asyncio, "run", lambda coro: None)

    response = _client().post("/internal/agent/drain")

    assert response.status_code == 202
    assert response.json() == {"status": "started"}
    assert "stale-wf-1" in cancelled
    assert enqueued == [drainer_router.drain_cycle]


def test_fresh_pending_not_reaped_returns_already_queued(monkeypatch):
    """A fresh PENDING row is left alone and NO new cycle is enqueued."""
    enqueued = []

    class FakeDBOS:
        def list_workflows(
            self,
            name=None,
            queue_name=None,
            status=None,
            limit=None,
            load_input=False,
            load_output=False,
        ):
            if status == "PENDING":
                # No stale workflows
                return []
            # Live workflows check (status is a list): return one live PENDING
            return [_FakeWorkflow("live-wf-1", int(1000 * 1000))]

        def list_workflow_steps(self, workflow_id, load_output=False):
            return []

        def cancel_workflow(self, workflow_uuid, cancel_children=False):
            raise AssertionError("should not cancel fresh cycle")

    class FakeQueue:
        def enqueue(self, workflow):
            enqueued.append(workflow)

    monkeypatch.setattr(drainer_router, "drainer_enabled", lambda: True)
    monkeypatch.setattr(drainer_router.runtime, "is_launched", lambda: True)
    monkeypatch.setattr(drainer_router.runtime, "init_dbos", lambda: FakeDBOS())
    monkeypatch.setattr(drainer_router, "drainer_queue", lambda: FakeQueue())

    response = _client().post("/internal/agent/drain")

    assert response.status_code == 200
    assert response.json() == {"status": "already_queued"}
    assert enqueued == []


def test_enqueued_row_prevents_second_enqueue(monkeypatch):
    """An ENQUEUED row present means no second enqueue."""
    enqueued = []

    class FakeDBOS:
        def list_workflows(
            self,
            name=None,
            queue_name=None,
            status=None,
            limit=None,
            load_input=False,
            load_output=False,
        ):
            if status == "PENDING":
                # No stale workflows
                return []
            # Live workflows check (status is a list): return one ENQUEUED
            return [_FakeWorkflow("live-wf-1", int(1000 * 1000))]

        def list_workflow_steps(self, workflow_id, load_output=False):
            return []

    class FakeQueue:
        def enqueue(self, workflow):
            enqueued.append(workflow)

    monkeypatch.setattr(drainer_router, "drainer_enabled", lambda: True)
    monkeypatch.setattr(drainer_router.runtime, "is_launched", lambda: True)
    monkeypatch.setattr(drainer_router.runtime, "init_dbos", lambda: FakeDBOS())
    monkeypatch.setattr(drainer_router, "drainer_queue", lambda: FakeQueue())

    response = _client().post("/internal/agent/drain")

    assert response.status_code == 200
    assert response.json() == {"status": "already_queued"}
    assert enqueued == []


def test_nothing_live_means_normal_enqueue(monkeypatch):
    """Nothing live means normal enqueue, unchanged from today."""
    enqueued = []

    class FakeDBOS:
        def list_workflows(
            self,
            name=None,
            queue_name=None,
            status=None,
            limit=None,
            load_input=False,
            load_output=False,
        ):
            # No stale workflows, no live workflows
            return []

        def list_workflow_steps(self, workflow_id, load_output=False):
            return []

    class FakeQueue:
        def enqueue(self, workflow):
            enqueued.append(workflow)

    monkeypatch.setattr(drainer_router, "drainer_enabled", lambda: True)
    monkeypatch.setattr(drainer_router.runtime, "is_launched", lambda: True)
    monkeypatch.setattr(drainer_router.runtime, "init_dbos", lambda: FakeDBOS())
    monkeypatch.setattr(drainer_router, "drainer_queue", lambda: FakeQueue())

    response = _client().post("/internal/agent/drain")

    assert response.status_code == 202
    assert response.json() == {"status": "started"}
    assert enqueued == [drainer_router.drain_cycle]


def test_drainer_disabled_still_short_circuits(monkeypatch):
    """Drainer disabled still short-circuits before any of this."""

    class FakeDBOS:
        def list_workflows(
            self,
            name=None,
            queue_name=None,
            status=None,
            limit=None,
            load_input=False,
            load_output=False,
        ):
            raise AssertionError("should not query when disabled")

    monkeypatch.setattr(drainer_router, "drainer_enabled", lambda: False)
    monkeypatch.setattr(drainer_router.runtime, "init_dbos", lambda: FakeDBOS())

    response = _client().post("/internal/agent/drain")

    assert response.status_code == 200
    assert response.json() == {"status": "disabled"}


def test_old_updated_at_with_recent_step_not_reaped(monkeypatch):
    """A PENDING workflow with old updated_at but recent step is NOT reaped.

    This test validates the fix: staleness is determined by max(updated_at, last_step_activity),
    not just updated_at. A workflow may have an old updated_at (from a status transition)
    but be actively running steps, and should not be reaped.
    """
    enqueued = []
    cancelled = []

    # Current time for this test
    now_ms = int(1_000_000_000 * 1000)  # Some large epoch ms value

    class FakeDBOS:
        def list_workflows(
            self,
            name=None,
            queue_name=None,
            status=None,
            limit=None,
            load_input=False,
            load_output=False,
        ):
            # status is a string for PENDING (from reaper), or list for live check
            if status == "PENDING":
                # From _reap_stale_drain_cycles: return one workflow
                # with very old updated_at (frozen at some status transition)
                return [
                    _FakeWorkflow(
                        "active-wf-1",
                        now_ms - (4000 * 1000),  # 4000 seconds old
                    )
                ]
            # From _has_live_drain_cycle (status is a list): no live workflows
            return []

        def list_workflow_steps(self, workflow_id, load_output=False):
            # Workflow has a recently completed step (within last 30 seconds)
            return [
                {
                    "function_id": "poll_turn",
                    "function_name": "poll_turn",
                    "started_at_epoch_ms": now_ms - (100 * 1000),  # 100 seconds ago
                    "completed_at_epoch_ms": now_ms
                    - (10 * 1000),  # 10 seconds ago, RECENT
                }
            ]

        def cancel_workflow(self, workflow_uuid, cancel_children=False):
            cancelled.append(workflow_uuid)

    class FakeQueue:
        def enqueue(self, workflow):
            enqueued.append(workflow)

    # Mock time.time() to return our test now_ms value
    import time as time_module

    monkeypatch.setattr(time_module, "time", lambda: now_ms / 1000)

    monkeypatch.setattr(drainer_router, "drainer_enabled", lambda: True)
    monkeypatch.setattr(drainer_router.runtime, "is_launched", lambda: True)
    monkeypatch.setattr(drainer_router.runtime, "init_dbos", lambda: FakeDBOS())
    monkeypatch.setattr(drainer_router, "drainer_queue", lambda: FakeQueue())

    response = _client().post("/internal/agent/drain")

    # The workflow should NOT be cancelled because its last step is recent
    assert cancelled == [], "Should not cancel workflow with recent step activity"
    # A new cycle should be enqueued
    assert response.status_code == 202
    assert response.json() == {"status": "started"}
    assert enqueued == [drainer_router.drain_cycle]
