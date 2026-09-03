import time

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


def test_dbos_not_configured_returns_503(monkeypatch):
    monkeypatch.setattr(drainer_router, "drainer_enabled", lambda: True)
    monkeypatch.setattr(drainer_router.runtime, "init_dbos", lambda: None)
    monkeypatch.setattr(drainer_router.runtime, "is_launched", lambda: True)
    monkeypatch.setattr(
        drainer_router.runtime,
        "read_client",
        lambda: (_ for _ in ()).throw(
            AssertionError("the leader must not construct a DBOSClient")
        ),
    )

    response = _client().post("/internal/agent/drain")

    assert response.status_code == 503
    assert "not configured" in response.json()["detail"]


class _FakeWorkflow:
    """Fake workflow object that mimics DBOS workflow_id and updated_at."""

    def __init__(self, workflow_id, updated_at, app_version=None):
        self.workflow_id = workflow_id
        self.updated_at = updated_at
        self.app_version = app_version


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
    monkeypatch.setattr(
        drainer_router.runtime,
        "read_client",
        lambda: (_ for _ in ()).throw(
            AssertionError("the leader must not construct a DBOSClient")
        ),
    )
    monkeypatch.setattr(drainer_router, "drainer_queue", lambda: FakeQueue())

    response = _client().post("/internal/agent/drain")

    assert response.status_code == 202
    assert response.json() == {"status": "started"}
    assert enqueued == [drainer_router.drain_cycle]


def test_follower_enqueues_workflow_through_dbos_client(monkeypatch):
    enqueued = []
    list_calls = []

    class FakeDBOSClient:
        def list_workflows(self, **kwargs):
            list_calls.append(kwargs)
            return []

        def get_latest_application_version(self):
            return {"version_name": "v-current"}

        def enqueue(self, options):
            enqueued.append(options)

    def no_queue():
        raise AssertionError("a follower must not resolve the leader queue")

    monkeypatch.setattr(drainer_router, "drainer_enabled", lambda: True)
    monkeypatch.setattr(
        drainer_router.runtime,
        "init_dbos",
        lambda: (_ for _ in ()).throw(
            AssertionError("a follower must not construct the DBOS singleton")
        ),
    )
    monkeypatch.setattr(drainer_router.runtime, "is_launched", lambda: False)
    monkeypatch.setattr(drainer_router.runtime, "read_client", lambda: FakeDBOSClient())
    monkeypatch.setattr(drainer_router, "drainer_queue", no_queue)
    monkeypatch.setattr(drainer_router, "_current_app_version", lambda: "")

    response = _client().post("/internal/agent/drain")

    assert response.status_code == 202
    assert response.json() == {"status": "started"}
    assert list_calls == [
        {
            "name": "drain_cycle",
            "queue_name": "drainer",
            "status": "PENDING",
            "load_input": False,
            "load_output": False,
        },
        {
            "name": "drain_cycle",
            "queue_name": "drainer",
            "status": ["PENDING", "ENQUEUED"],
            "limit": 1,
            "load_input": False,
            "load_output": False,
        },
    ]
    assert enqueued == [
        {
            "workflow_name": "drain_cycle",
            "queue_name": "drainer",
            "app_version": "v-current",
        }
    ]


def test_follower_returns_already_queued_for_live_cycle(monkeypatch):
    class FakeDBOSClient:
        def list_workflows(self, status=None, **_kwargs):
            if status == "PENDING":
                return []
            return [_FakeWorkflow("live-wf", int(time.time() * 1000))]

        def enqueue(self, _options):
            raise AssertionError("a live cycle must prevent a second enqueue")

    monkeypatch.setattr(drainer_router, "drainer_enabled", lambda: True)
    monkeypatch.setattr(
        drainer_router.runtime,
        "init_dbos",
        lambda: (_ for _ in ()).throw(
            AssertionError("a follower must not construct the DBOS singleton")
        ),
    )
    monkeypatch.setattr(drainer_router.runtime, "is_launched", lambda: False)
    monkeypatch.setattr(drainer_router.runtime, "read_client", lambda: FakeDBOSClient())
    monkeypatch.setattr(
        drainer_router,
        "drainer_queue",
        lambda: (_ for _ in ()).throw(
            AssertionError("a follower must not resolve the leader queue")
        ),
    )
    monkeypatch.setattr(drainer_router, "_current_app_version", lambda: "")

    response = _client().post("/internal/agent/drain")

    assert response.status_code == 200
    assert response.json() == {"status": "already_queued"}


def test_follower_enqueue_failure_returns_retry_after(monkeypatch):
    class FakeDBOSClient:
        def list_workflows(self, **_kwargs):
            return []

        def get_latest_application_version(self):
            return {"version_name": "v-current"}

        def enqueue(self, _options):
            raise RuntimeError("database unavailable")

    monkeypatch.setattr(drainer_router, "drainer_enabled", lambda: True)
    monkeypatch.setattr(
        drainer_router.runtime,
        "init_dbos",
        lambda: (_ for _ in ()).throw(
            AssertionError("a follower must not construct the DBOS singleton")
        ),
    )
    monkeypatch.setattr(drainer_router.runtime, "is_launched", lambda: False)
    monkeypatch.setattr(drainer_router.runtime, "read_client", lambda: FakeDBOSClient())
    monkeypatch.setattr(drainer_router, "_current_app_version", lambda: "")

    response = _client().post("/internal/agent/drain")

    assert response.status_code == 503
    assert response.headers["Retry-After"] == "2"


def test_follower_without_dbos_client_returns_503(monkeypatch):
    monkeypatch.setattr(drainer_router, "drainer_enabled", lambda: True)
    monkeypatch.setattr(
        drainer_router.runtime,
        "init_dbos",
        lambda: (_ for _ in ()).throw(
            AssertionError("a follower must not construct the DBOS singleton")
        ),
    )
    monkeypatch.setattr(drainer_router.runtime, "is_launched", lambda: False)
    monkeypatch.setattr(drainer_router.runtime, "read_client", lambda: None)

    response = _client().post("/internal/agent/drain")

    assert response.status_code == 503
    assert "not configured" in response.json()["detail"]


def test_follower_reaps_stale_pending_cycle(monkeypatch):
    cancelled = []
    enqueued = []

    class FakeDBOSClient:
        def list_workflows(self, status=None, **_kwargs):
            if status == "PENDING":
                return [_FakeWorkflow("stale-follower-wf", 1000)]
            return []

        def list_workflow_steps(self, _workflow_id, load_output=False):
            return []

        def cancel_workflow(self, workflow_uuid, *, cancel_children=False):
            cancelled.append((workflow_uuid, cancel_children))

        def get_latest_application_version(self):
            return {"version_name": "v-current"}

        def enqueue(self, options):
            enqueued.append(options)

    def close_coro(coro):
        coro.close()

    monkeypatch.setattr(drainer_router, "drainer_enabled", lambda: True)
    monkeypatch.setattr(drainer_router.runtime, "is_launched", lambda: False)
    monkeypatch.setattr(
        drainer_router.runtime,
        "init_dbos",
        lambda: (_ for _ in ()).throw(
            AssertionError("a follower must not construct the DBOS singleton")
        ),
    )
    monkeypatch.setattr(drainer_router.runtime, "read_client", lambda: FakeDBOSClient())
    monkeypatch.setattr(drainer_router, "_current_app_version", lambda: "")
    monkeypatch.setattr(drainer_router.asyncio, "run", close_coro)

    response = _client().post("/internal/agent/drain")

    assert response.status_code == 202
    assert cancelled == [("stale-follower-wf", True)]
    assert enqueued == [
        {
            "workflow_name": "drain_cycle",
            "queue_name": "drainer",
            "app_version": "v-current",
        }
    ]


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
    queue_resolutions = []

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

    def resolve_queue():
        queue_resolutions.append(True)
        return FakeQueue()

    monkeypatch.setattr(drainer_router, "drainer_enabled", lambda: True)
    monkeypatch.setattr(drainer_router.runtime, "is_launched", lambda: True)
    monkeypatch.setattr(drainer_router.runtime, "init_dbos", lambda: FakeDBOS())
    monkeypatch.setattr(
        drainer_router.runtime,
        "read_client",
        lambda: (_ for _ in ()).throw(
            AssertionError("the leader must not construct a DBOSClient")
        ),
    )
    monkeypatch.setattr(drainer_router, "drainer_queue", resolve_queue)

    response = _client().post("/internal/agent/drain")

    assert response.status_code == 200
    assert response.json() == {"status": "already_queued"}
    assert queue_resolutions == [True]
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


def test_step_read_failure_does_not_reap(monkeypatch):
    """A step-read failure must skip the reap, not fall back to updated_at.

    updated_at is frozen at dequeue for a healthy running cycle, which is the
    whole reason the step signal exists. Falling back to it when the step read
    fails would reap exactly the long healthy cycles the signal protects, so
    the failure path leaves the workflow for the next tick instead.
    """
    enqueued = []
    cancelled = []
    now_ms = int(1_000_000_000 * 1000)

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
                # Old enough to be reaped on updated_at alone.
                return [_FakeWorkflow("active-wf-1", now_ms - (4000 * 1000))]
            return []

        def list_workflow_steps(self, workflow_id, load_output=False):
            raise RuntimeError("step read failed")

        def cancel_workflow(self, workflow_uuid, cancel_children=False):
            cancelled.append(workflow_uuid)

    class FakeQueue:
        def enqueue(self, workflow):
            enqueued.append(workflow)

    import time as time_module

    monkeypatch.setattr(time_module, "time", lambda: now_ms / 1000)
    monkeypatch.setattr(drainer_router, "drainer_enabled", lambda: True)
    monkeypatch.setattr(drainer_router.runtime, "is_launched", lambda: True)
    monkeypatch.setattr(drainer_router.runtime, "init_dbos", lambda: FakeDBOS())
    monkeypatch.setattr(drainer_router, "drainer_queue", lambda: FakeQueue())

    response = _client().post("/internal/agent/drain")

    assert cancelled == [], "a step-read failure must not cancel the workflow"
    assert response.status_code == 202
    assert enqueued == [drainer_router.drain_cycle]


def test_queue_is_resolved_even_when_a_cycle_is_already_live(monkeypatch):
    """The already_queued path must still resolve the queue.

    drainer_queue() is what constructs the DBOS Queue and so registers it in
    the DBOS registry, and the queue thread only polls registered queues. If
    the early return skipped that call, a pod that rolled holding a backlog
    would never register the queue, never dequeue the backlog, and so keep
    seeing a live cycle forever. That stall is self-reinforcing and is exactly
    what this endpoint exists to prevent.
    """
    resolved = []
    enqueued = []

    class FakeDBOS:
        def list_workflows(self, **_kwargs):
            # A live cycle exists, so trigger_drain takes the early return.
            return [_FakeWorkflow("live-wf", int(time.time() * 1000))]

        def list_workflow_steps(self, _workflow_id, load_output=False):
            return []

        def cancel_workflow(self, _workflow_uuid, cancel_children=False):
            raise AssertionError("a live cycle must not be cancelled")

    class FakeQueue:
        def enqueue(self, workflow):
            enqueued.append(workflow)

    def fake_drainer_queue():
        resolved.append(True)
        return FakeQueue()

    monkeypatch.setattr(drainer_router, "drainer_enabled", lambda: True)
    monkeypatch.setattr(drainer_router.runtime, "is_launched", lambda: True)
    monkeypatch.setattr(drainer_router.runtime, "init_dbos", lambda: FakeDBOS())
    monkeypatch.setattr(drainer_router, "drainer_queue", fake_drainer_queue)

    response = _client().post("/internal/agent/drain")

    assert response.json() == {"status": "already_queued"}
    assert enqueued == [], "no second cycle should be stacked"
    assert resolved == [True], "the queue must still be resolved, to register it"


def _stranded_dbos(monkeypatch, enqueued_version, cancelled, latest_version="v-new"):
    """A DBOS whose only live row is an ENQUEUED cycle at some app version."""
    now_ms = int(time.time() * 1000)

    class FakeDBOS:
        def list_workflows(self, status=None, **_kwargs):
            if status == "ENQUEUED":
                return [_FakeWorkflow("stranded-wf", now_ms, enqueued_version)]
            return []

        def list_workflow_steps(self, _workflow_id, load_output=False):
            return []

        def cancel_workflow(self, workflow_uuid, cancel_children=False):
            cancelled.append(workflow_uuid)

        def get_latest_application_version(self):
            return {"version_name": latest_version}

    monkeypatch.setattr(drainer_router, "drainer_enabled", lambda: True)
    monkeypatch.setattr(drainer_router.runtime, "is_launched", lambda: True)
    monkeypatch.setattr(drainer_router.runtime, "init_dbos", lambda: FakeDBOS())
    monkeypatch.setattr(drainer_router, "drainer_queue", lambda: _NoopQueue())


class _NoopQueue:
    def enqueue(self, _workflow):
        return None


def test_version_stranded_enqueued_cycle_is_cancelled(monkeypatch):
    """An ENQUEUED row from a previous image is undequeuable, so cancel it.

    The dequeue query filters application_version against the worker's own, so
    such a row is never claimed. It is not PENDING, so the staleness reaper
    never sees it, yet the live check counts it, which latches already_queued
    forever.
    """
    cancelled = []
    monkeypatch.setattr(drainer_router, "_current_app_version", lambda: "v-new")
    _stranded_dbos(monkeypatch, "v-old", cancelled)

    _client().post("/internal/agent/drain")

    assert cancelled == ["stranded-wf"]


def test_matching_version_enqueued_cycle_is_left_alone(monkeypatch):
    """A successor queued by THIS image is healthy work waiting for the slot."""
    cancelled = []
    monkeypatch.setattr(drainer_router, "_current_app_version", lambda: "v-new")
    _stranded_dbos(monkeypatch, "v-new", cancelled)

    _client().post("/internal/agent/drain")

    assert cancelled == []


def test_null_version_enqueued_cycle_is_cancelled_after_rollback(monkeypatch):
    """A NULL-version row is stranded when this leader is not the latest."""
    cancelled = []
    monkeypatch.setattr(drainer_router, "_current_app_version", lambda: "v-old")
    _stranded_dbos(monkeypatch, None, cancelled, latest_version="v-new")

    _client().post("/internal/agent/drain")

    assert cancelled == ["stranded-wf"]


def test_unresolvable_app_version_cancels_nothing(monkeypatch):
    """Cannot tell is not evidence of stranding.

    swarm/router.py documents a real incident where an unresolved version was
    compared literally and marked every live run stranded. Same discipline
    here: an empty version means do nothing.
    """
    cancelled = []
    monkeypatch.setattr(drainer_router, "_current_app_version", lambda: "")
    _stranded_dbos(monkeypatch, "v-old", cancelled)

    _client().post("/internal/agent/drain")

    assert cancelled == []
