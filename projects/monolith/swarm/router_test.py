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
        args = None

        def start_workflow(self, *args):
            self.args = args
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


def test_budget_must_be_positive(monkeypatch):
    monkeypatch.setenv("SWARM_ENABLED", "true")
    response = client().post(
        "/api/swarm/runs",
        json={
            "task": "fix",
            "repo": "jomcgi/homelab",
            "branch": "main",
            "budget_usd": -1,
        },
    )
    assert response.status_code == 400


def test_start_run_passes_budget(monkeypatch):
    monkeypatch.setenv("SWARM_ENABLED", "true")
    captured = []

    class Handle:
        workflow_id = "wf-1"

    class FakeDBOS:
        def start_workflow(self, *args):
            captured.append(args)
            return Handle()

    monkeypatch.setattr(swarm_router.runtime, "init_dbos", lambda: FakeDBOS())
    monkeypatch.setattr(swarm_router.runtime, "is_launched", lambda: True)
    response = client().post(
        "/api/swarm/runs",
        json={
            "task": "fix",
            "repo": "jomcgi/homelab",
            "branch": "main",
            "budget_usd": 2.0,
        },
    )
    assert response.status_code == 200
    assert captured[0][1:] == ("fix", "jomcgi/homelab", "main", 2.0)


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
        async def cancel_workflow_async(self, workflow_id, *, cancel_children=False):
            events.append(("cancel", workflow_id, cancel_children))

        async def update_workflow_attributes_async(self, workflow_id, values):
            events.append(("attributes", workflow_id, values["cancelled_by"]["actor"]))

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
    response = client().post(
        "/api/swarm/runs/wf-1/cancel",
        headers={"Cf-Access-Authenticated-User-Email": "alice@example.com"},
    )

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
    assert events == [
        ("cancel", "wf-1", True),
        ("reap", "wf-1"),
        ("attributes", "wf-1", "alice@example.com"),
    ]


def test_cancel_reports_reap_failure_without_failing(monkeypatch):
    monkeypatch.setenv("SWARM_ENABLED", "true")

    class FakeDBOS:
        async def cancel_workflow_async(self, workflow_id, *, cancel_children=False):
            assert workflow_id == "wf-1"
            assert cancel_children is True

        async def update_workflow_attributes_async(self, workflow_id, values):
            assert workflow_id == "wf-1"
            assert values["cancelled_by"]["actor"] == "operator"

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


def test_cancel_survives_attribute_write_failure(monkeypatch):
    monkeypatch.setenv("SWARM_ENABLED", "true")

    class FakeDBOS:
        async def cancel_workflow_async(self, workflow_id, *, cancel_children=False):
            pass

        async def update_workflow_attributes_async(self, workflow_id, values):
            raise RuntimeError("attribute store unavailable")

    async def reap(_workflow_id):
        return {"reaped": [], "failed": [], "skipped": []}

    monkeypatch.setattr(swarm_router.runtime, "init_dbos", lambda: FakeDBOS())
    monkeypatch.setattr(swarm_router.runtime, "is_launched", lambda: True)
    monkeypatch.setattr("agent_sessions.api.reap_sessions_for_workflow", reap)
    response = client().post("/api/swarm/runs/wf-1/cancel")

    assert response.status_code == 200
    assert response.json()["cancelled"] is True


def test_server_app_version_comes_from_dbos_not_the_environment(monkeypatch):
    """The comparison target must be DBOS's own version.

    APP_VERSION and GIT_SHA are a different namespace from the hash DBOS
    computes over workflow source, so reading them could only ever produce a
    value no run matches, which marked every pending run stranded.
    """
    from dbos._utils import GlobalParams

    monkeypatch.setenv("APP_VERSION", "chart-0.285.531")
    monkeypatch.setenv("GIT_SHA", "deadbeef")
    monkeypatch.setattr(GlobalParams, "app_version", "dbos-computed-hash")
    assert swarm_router._server_app_version() == "dbos-computed-hash"


def test_server_app_version_is_empty_when_dbos_has_not_launched(monkeypatch):
    """Empty means "cannot tell", which compose_run must not read as stranded."""
    from dbos._utils import GlobalParams

    monkeypatch.setattr(GlobalParams, "app_version", "")
    assert swarm_router._server_app_version() == ""
