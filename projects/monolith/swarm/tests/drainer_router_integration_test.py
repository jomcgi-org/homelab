"""Integration tests for drainer router against real Postgres."""

from __future__ import annotations

import time

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import text as sql_text

import swarm.drainer_router as drainer_router


def _client() -> TestClient:
    app = FastAPI()
    app.include_router(drainer_router.router)
    return TestClient(app)


def test_reaper_against_real_postgres(session, monkeypatch):
    """Test the reaper with real SQL against a real PostgreSQL instance.

    This test ensures the schema assumptions (column names, types, values)
    match reality and that the SQL executes correctly. The fake tests in
    drainer_router_test.py verify shape; this one verifies arrival.
    """
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
    assert "stale-wf" in cancelled, f"Expected 'stale-wf' in cancelled, got {cancelled}"
    assert "fresh-wf" not in cancelled, f"Did not expect 'fresh-wf' in cancelled"

    # Since no live workflows exist after reaping the stale one, enqueue should happen.
    assert response.status_code == 202, (
        f"Expected 202, got {response.status_code}: {response.json()}"
    )
    assert response.json() == {"status": "started"}
    assert enqueued == [drainer_router.drain_cycle]
