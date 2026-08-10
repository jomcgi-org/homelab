from fastapi import FastAPI
from fastapi.testclient import TestClient

import swarm.router as swarm_router


def client():
    app = FastAPI()
    app.include_router(swarm_router.router)
    return TestClient(app)


def test_disabled_returns_503(monkeypatch):
    monkeypatch.setenv("SWARM_ENABLED", "false")
    response = client().post(
        "/api/swarm/runs",
        json={"task": "fix", "repo": "jomcgi/homelab", "branch": "main"},
    )
    assert response.status_code == 503
    assert "disabled" in response.json()["detail"]


def test_unknown_repo_rejected(monkeypatch):
    monkeypatch.setenv("SWARM_ENABLED", "true")
    response = client().post(
        "/api/swarm/runs",
        json={"task": "fix", "repo": "not/a-repo", "branch": "main"},
    )
    assert response.status_code == 400
    assert "unknown repo" in response.json()["detail"]


def test_start_run(monkeypatch):
    monkeypatch.setenv("SWARM_ENABLED", "true")

    class Handle:
        workflow_id = "wf-1"

    class FakeDBOS:
        def start_workflow(self, *args):
            return Handle()

    monkeypatch.setattr(swarm_router.runtime, "init_dbos", lambda: FakeDBOS())
    monkeypatch.setattr(swarm_router.runtime, "is_launched", lambda: True)
    response = client().post(
        "/api/swarm/runs",
        json={
            "task": "fix",
            "repo": "jomcgi/homelab",
            "branch": "main",
            "idempotency_key": "request-1",
        },
    )
    assert response.status_code == 200
    assert response.json() == {"workflow_id": "wf-1"}


def test_follower_replica_returns_503(monkeypatch):
    """DBOS launches on the leader only, but every replica serves the router.
    A follower must refuse rather than submit against an unlaunched runtime."""
    monkeypatch.setenv("SWARM_ENABLED", "true")

    class FakeDBOS:
        def start_workflow(self, *args):  # pragma: no cover - must not be reached
            raise AssertionError("a follower replica must not submit a workflow")

    monkeypatch.setattr(swarm_router.runtime, "init_dbos", lambda: FakeDBOS())
    monkeypatch.setattr(swarm_router.runtime, "is_launched", lambda: False)
    response = client().post(
        "/api/swarm/runs",
        json={"task": "fix", "repo": "jomcgi/homelab", "branch": "main"},
    )
    assert response.status_code == 503
    assert "not launched" in response.json()["detail"]


def test_cancel_reaps_after_dbos_cancel(monkeypatch):
    monkeypatch.setenv("SWARM_ENABLED", "true")
    events = []

    class FakeDBOS:
        # async, matching the real DBOS API: the sync cancel_workflow raises
        # from a running event loop (check_async), so a sync fake would let a
        # production-breaking handler pass its tests.
        async def cancel_workflow_async(self, workflow_id, cancel_children=False):
            events.append(("cancel", workflow_id, cancel_children))

    async def reap(workflow_id):
        events.append(("reap", workflow_id))
        return {
            "reaped": [2],
            "failed": [],
            "skipped": [4],
        }

    monkeypatch.setattr(swarm_router.runtime, "init_dbos", lambda: FakeDBOS())
    monkeypatch.setattr(swarm_router.runtime, "is_launched", lambda: True)
    monkeypatch.setattr("agent_sessions.api.reap_sessions_for_workflow", reap)
    response = client().post("/api/swarm/runs/wf-1/cancel")

    assert response.status_code == 200
    assert response.json() == {
        "workflow_id": "wf-1",
        "cancelled": True,
        "guest_sessions": {
            "reaped": [2],
            "failed": [],
            "skipped": [4],
        },
    }
    assert events == [("cancel", "wf-1", True), ("reap", "wf-1")]


def test_cancel_reports_reap_failure_without_failing(monkeypatch):
    monkeypatch.setenv("SWARM_ENABLED", "true")

    class FakeDBOS:
        async def cancel_workflow_async(self, workflow_id, cancel_children=False):
            assert workflow_id == "wf-1"
            assert cancel_children is True

    async def reap(_workflow_id):
        return {
            "reaped": [],
            "failed": [{"session_id": 3, "error": "boom"}],
            "skipped": [],
        }

    monkeypatch.setattr(swarm_router.runtime, "init_dbos", lambda: FakeDBOS())
    monkeypatch.setattr(swarm_router.runtime, "is_launched", lambda: True)
    monkeypatch.setattr("agent_sessions.api.reap_sessions_for_workflow", reap)
    response = client().post("/api/swarm/runs/wf-1/cancel")

    assert response.status_code == 200
    assert response.json()["cancelled"] is True
    assert response.json()["guest_sessions"]["failed"] == [
        {"session_id": 3, "error": "boom"}
    ]
