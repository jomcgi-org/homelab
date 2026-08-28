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


def _fake_session_no_live_workflows(monkeypatch):
    """Mock Session to return no stale or live workflows."""

    class FakeSession:
        def __init__(self, engine):
            self.engine = engine

        def execute(self, sql, params=None):
            # No stale workflows, no live workflows
            return FakeResult([])

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

    class FakeResult:
        def __init__(self, value):
            self.value = value

        def fetchall(self):
            return self.value or []

        def scalar(self):
            return None

    monkeypatch.setattr(drainer_router, "Session", FakeSession)


def test_launched_enqueues_workflow_on_drainer_queue(monkeypatch):
    enqueued = []

    class FakeDBOS:
        def start_workflow(self, _workflow):
            raise AssertionError("drain cycle must be queued")

    class FakeQueue:
        def enqueue(self, workflow):
            enqueued.append(workflow)

    _fake_session_no_live_workflows(monkeypatch)

    monkeypatch.setattr(drainer_router, "drainer_enabled", lambda: True)
    monkeypatch.setattr(drainer_router.runtime, "is_launched", lambda: True)
    monkeypatch.setattr(drainer_router.runtime, "init_dbos", lambda: FakeDBOS())
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
        def cancel_workflow(self, workflow_uuid, cancel_children=False):
            cancelled.append(workflow_uuid)

        def start_workflow(self, _workflow):
            raise AssertionError("drain cycle must be queued")

    class FakeQueue:
        def enqueue(self, workflow):
            enqueued.append(workflow)

    class FakeSession:
        def __init__(self, engine):
            self.engine = engine

        def execute(self, sql, params=None):
            sql_text = str(sql)
            if "SELECT 1" in sql_text:
                # No live drain_cycle, so enqueue
                return FakeResult(None)
            # Query returns one stale workflow
            return FakeResult([("stale-wf-1", 1000)])

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

    class FakeResult:
        def __init__(self, value):
            self.value = value

        def fetchall(self):
            return self.value or []

        def scalar(self):
            return self.value

    monkeypatch.setattr(drainer_router, "drainer_enabled", lambda: True)
    monkeypatch.setattr(drainer_router.runtime, "is_launched", lambda: True)
    monkeypatch.setattr(drainer_router.runtime, "init_dbos", lambda: FakeDBOS())
    monkeypatch.setattr(drainer_router, "drainer_queue", lambda: FakeQueue())
    monkeypatch.setattr(drainer_router, "Session", FakeSession)
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
        def cancel_workflow(self, workflow_uuid, cancel_children=False):
            raise AssertionError("should not cancel fresh cycle")

        def start_workflow(self, _workflow):
            raise AssertionError("drain cycle must be queued")

    class FakeQueue:
        def enqueue(self, workflow):
            enqueued.append(workflow)

    class FakeSession:
        def __init__(self, engine):
            self.engine = engine

        def execute(self, sql, params=None):
            sql_text = str(sql)
            if "SELECT 1" in sql_text:
                # A live drain_cycle exists, so don't enqueue
                return FakeResult(1)
            # Query returns no stale workflows
            return FakeResult([])

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

    class FakeResult:
        def __init__(self, value):
            self.value = value

        def fetchall(self):
            return self.value or []

        def scalar(self):
            return self.value

    monkeypatch.setattr(drainer_router, "drainer_enabled", lambda: True)
    monkeypatch.setattr(drainer_router.runtime, "is_launched", lambda: True)
    monkeypatch.setattr(drainer_router.runtime, "init_dbos", lambda: FakeDBOS())
    monkeypatch.setattr(drainer_router, "drainer_queue", lambda: FakeQueue())
    monkeypatch.setattr(drainer_router, "Session", FakeSession)

    response = _client().post("/internal/agent/drain")

    assert response.status_code == 200
    assert response.json() == {"status": "already_queued"}
    assert enqueued == []


def test_enqueued_row_prevents_second_enqueue(monkeypatch):
    """An ENQUEUED row present means no second enqueue."""
    enqueued = []

    class FakeDBOS:
        def start_workflow(self, _workflow):
            raise AssertionError("drain cycle must be queued")

    class FakeQueue:
        def enqueue(self, workflow):
            enqueued.append(workflow)

    class FakeSession:
        def __init__(self, engine):
            self.engine = engine

        def execute(self, sql, params=None):
            sql_text = str(sql)
            if "SELECT 1" in sql_text:
                # A live ENQUEUED drain_cycle exists
                return FakeResult(1)
            # Query returns no stale workflows
            return FakeResult([])

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

    class FakeResult:
        def __init__(self, value):
            self.value = value

        def fetchall(self):
            return self.value or []

        def scalar(self):
            return self.value

    monkeypatch.setattr(drainer_router, "drainer_enabled", lambda: True)
    monkeypatch.setattr(drainer_router.runtime, "is_launched", lambda: True)
    monkeypatch.setattr(drainer_router.runtime, "init_dbos", lambda: FakeDBOS())
    monkeypatch.setattr(drainer_router, "drainer_queue", lambda: FakeQueue())
    monkeypatch.setattr(drainer_router, "Session", FakeSession)

    response = _client().post("/internal/agent/drain")

    assert response.status_code == 200
    assert response.json() == {"status": "already_queued"}
    assert enqueued == []


def test_nothing_live_means_normal_enqueue(monkeypatch):
    """Nothing live means normal enqueue, unchanged from today."""
    enqueued = []

    class FakeDBOS:
        def start_workflow(self, _workflow):
            raise AssertionError("drain cycle must be queued")

    class FakeQueue:
        def enqueue(self, workflow):
            enqueued.append(workflow)

    class FakeSession:
        def __init__(self, engine):
            self.engine = engine

        def execute(self, sql, params=None):
            # No stale workflows, no live workflows
            return FakeResult([])

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

    class FakeResult:
        def __init__(self, value):
            self.value = value

        def fetchall(self):
            return self.value or []

        def scalar(self):
            return None

    monkeypatch.setattr(drainer_router, "drainer_enabled", lambda: True)
    monkeypatch.setattr(drainer_router.runtime, "is_launched", lambda: True)
    monkeypatch.setattr(drainer_router.runtime, "init_dbos", lambda: FakeDBOS())
    monkeypatch.setattr(drainer_router, "drainer_queue", lambda: FakeQueue())
    monkeypatch.setattr(drainer_router, "Session", FakeSession)

    response = _client().post("/internal/agent/drain")

    assert response.status_code == 202
    assert response.json() == {"status": "started"}
    assert enqueued == [drainer_router.drain_cycle]


def test_drainer_disabled_still_short_circuits(monkeypatch):
    """Drainer disabled still short-circuits before any of this."""

    class FakeSession:
        def execute(self, sql, params=None):
            raise AssertionError("should not query when disabled")

    monkeypatch.setattr(drainer_router, "drainer_enabled", lambda: False)
    monkeypatch.setattr(drainer_router, "Session", FakeSession)

    response = _client().post("/internal/agent/drain")

    assert response.status_code == 200
    assert response.json() == {"status": "disabled"}


def test_reaper_sql_matches_dbos_schema():
    """Verify the reaper's hand-written SQL matches the DBOS schema.

    The reaper hand-writes SQL against dbos.workflow_status, a table DBOS owns.
    If a DBOS upgrade renames a column or changes a type, this test must fail
    before the change reaches production, rather than silently disabling the
    drainer (which nearly happened when we queried the nonexistent workflow_id
    column and caught the exception broadly, returning already_queued forever).

    This test is fast and deterministic: it needs no database, only the DBOS
    package installed in the test environment.
    """
    from dbos._schemas.system_database import SystemSchema
    from sqlalchemy import BigInteger

    columns = SystemSchema.workflow_status.c

    # Every column our SQL queries must exist under the name we use.
    for name in ("workflow_uuid", "updated_at", "status", "queue_name", "name"):
        assert name in columns, (
            f"workflow_status column '{name}' not found; DBOS schema changed"
        )

    # We compare updated_at and created_at against epoch milliseconds,
    # so these must be integer columns.
    for name in ("updated_at", "created_at"):
        assert isinstance(columns[name].type, BigInteger), (
            f"{name} must be BigInteger, got {type(columns[name].type)}"
        )

    # The specific bug that nearly shipped: this column does not exist.
    # If you see this fail, you queried workflow_id instead of workflow_uuid.
    assert "workflow_id" not in columns, "workflow_id does not exist; use workflow_uuid"
