from fastapi import FastAPI
from fastapi.testclient import TestClient
import time

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


# Real SQL test against Postgres fixture
def test_reaper_against_real_postgres(session, monkeypatch):
    """Test the reaper with real SQL against a real PostgreSQL instance.

    This test ensures the schema assumptions (column names, types, values)
    match reality and that the SQL executes correctly. The fake tests above
    verify shape; this one verifies arrival.
    """
    from sqlalchemy import text as sql_text

    # Create the dbos.workflow_status table with the exact schema from prod.
    session.execute(sql_text("CREATE SCHEMA IF NOT EXISTS dbos"))
    session.execute(
        sql_text(
            """
            CREATE TABLE IF NOT EXISTS dbos.workflow_status (
                workflow_uuid text PRIMARY KEY,
                name text NOT NULL,
                status text NOT NULL,
                queue_name text,
                created_at bigint,
                updated_at bigint
            )
            """
        )
    )
    session.commit()

    # Current time in milliseconds.
    now_ms = int(time.time() * 1000)

    # staleness_threshold_seconds = 1800 + (3 * 60) + 600 = 2880 seconds = 2880000 ms
    # (assuming drainer defaults)
    staleness_threshold_ms = 2880000

    # Insert a stale PENDING cycle (created 4000 ms ago, well past the 2880 ms threshold).
    stale_updated_at_ms = now_ms - 4000
    session.execute(
        sql_text(
            """
            INSERT INTO dbos.workflow_status
            (workflow_uuid, name, status, queue_name, created_at, updated_at)
            VALUES (:uuid, :name, :status, :queue_name, :created_at, :updated_at)
            """
        ),
        {
            "uuid": "stale-wf",
            "name": "drain_cycle",
            "status": "PENDING",
            "queue_name": "drainer",
            "created_at": stale_updated_at_ms - 10000,
            "updated_at": stale_updated_at_ms,
        },
    )

    # Insert a fresh PENDING cycle (created 1000 ms ago, within the 2880 ms threshold).
    fresh_updated_at_ms = now_ms - 1000
    session.execute(
        sql_text(
            """
            INSERT INTO dbos.workflow_status
            (workflow_uuid, name, status, queue_name, created_at, updated_at)
            VALUES (:uuid, :name, :status, :queue_name, :created_at, :updated_at)
            """
        ),
        {
            "uuid": "fresh-wf",
            "name": "drain_cycle",
            "status": "PENDING",
            "queue_name": "drainer",
            "created_at": fresh_updated_at_ms - 5000,
            "updated_at": fresh_updated_at_ms,
        },
    )
    session.commit()

    # Patch get_engine to return the test session's engine.
    from core import db as core_db

    monkeypatch.setattr(core_db, "get_engine", lambda: session.get_bind())

    # Create a fake DBOS that tracks which workflows were cancelled.
    cancelled = []

    class FakeDBOS:
        def cancel_workflow(self, workflow_uuid, cancel_children=False):
            cancelled.append(workflow_uuid)

    # Create a fake queue that tracks enqueues.
    enqueued = []

    class FakeQueue:
        def enqueue(self, workflow):
            enqueued.append(workflow)

    monkeypatch.setattr(drainer_router, "drainer_enabled", lambda: True)
    monkeypatch.setattr(drainer_router.runtime, "is_launched", lambda: True)
    monkeypatch.setattr(drainer_router.runtime, "init_dbos", lambda: FakeDBOS())
    monkeypatch.setattr(drainer_router, "drainer_queue", lambda: FakeQueue())
    monkeypatch.setattr(drainer_router.asyncio, "run", lambda coro: None)

    response = _client().post("/internal/agent/drain")

    # The reaper should cancel only the stale workflow.
    assert "stale-wf" in cancelled
    assert "fresh-wf" not in cancelled

    # Since no live workflows exist after reaping the stale one, enqueue should happen.
    assert response.status_code == 202
    assert response.json() == {"status": "started"}
    assert enqueued == [drainer_router.drain_cycle]
