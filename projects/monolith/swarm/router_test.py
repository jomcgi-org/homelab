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
    assert captured[0][1:] == ("fix", "jomcgi/homelab", "main", 2.0, None)


def test_planned_run_with_explicit_model(monkeypatch):
    monkeypatch.setenv("SWARM_ENABLED", "true")
    captured = []

    class Handle:
        workflow_id = "wf-planned"

    class FakeDBOS:
        def start_workflow(self, *args):
            captured.append(args)
            return Handle()

    async def classify(_task):
        return "planned", 1, "success", None

    monkeypatch.setattr(swarm_router.runtime, "init_dbos", lambda: FakeDBOS())
    monkeypatch.setattr(swarm_router.runtime, "is_launched", lambda: True)
    monkeypatch.setattr("swarm.classifier.classify_task_with_outcome", classify)
    monkeypatch.setattr(swarm_router, "_create_task_sync", lambda *args: None)
    monkeypatch.setattr(swarm_router, "_record_classification_sync", lambda *args: None)
    monkeypatch.setattr(
        swarm_router, "_set_task_link_sync", lambda *args, **kwargs: None
    )

    response = client().post(
        "/api/swarm/classify-and-start",
        json={
            "task": "fix",
            "repo": "jomcgi/homelab",
            "branch": "main",
            "model": "terra",
        },
    )

    assert response.status_code == 200
    assert captured[0][1:] == ("fix", "jomcgi/homelab", "main", None, "terra")


def test_planned_run_rejects_unknown_model(monkeypatch):
    monkeypatch.setenv("SWARM_ENABLED", "true")

    async def classify(_task):
        return "planned", 1, "success", None

    monkeypatch.setattr("swarm.classifier.classify_task_with_outcome", classify)
    monkeypatch.setattr(swarm_router, "_create_task_sync", lambda *args: None)
    monkeypatch.setattr(swarm_router, "_record_classification_sync", lambda *args: None)

    response = client().post(
        "/api/swarm/classify-and-start",
        json={
            "task": "fix",
            "repo": "jomcgi/homelab",
            "branch": "main",
            "model": "invalid",
        },
    )

    assert response.status_code == 400
    assert "Unknown model" in response.json()["detail"]


def test_planned_without_repo_returns_needs_input(monkeypatch):
    monkeypatch.setenv("SWARM_ENABLED", "true")
    recorded = []

    async def classify(_task):
        return "planned", 1, "success", None

    monkeypatch.setattr("swarm.classifier.classify_task_with_outcome", classify)
    monkeypatch.setattr("swarm.models.mint_task_id", lambda: "task-1")
    monkeypatch.setattr(swarm_router, "_create_task_sync", lambda *args: None)
    monkeypatch.setattr(
        swarm_router, "_record_classification_sync", lambda *args: recorded.append(args)
    )
    monkeypatch.setattr(
        swarm_router,
        "start_run",
        lambda _request: (_ for _ in ()).throw(AssertionError("run must not start")),
    )

    response = client().post(
        "/api/swarm/classify-and-start",
        json={"task": "fix", "model": "terra"},
    )

    assert response.status_code == 200
    assert response.json() == {
        "task_id": "task-1",
        "classification": "planned",
        "session_id": None,
        "workflow_id": None,
        "kind": "needs_input",
        "needs_input": {"repo": True, "branch": True},
    }
    assert recorded


def test_planned_resubmission_reuses_task_id(monkeypatch):
    monkeypatch.setenv("SWARM_ENABLED", "true")
    started = []
    updated = []

    async def classify(_task):
        return "planned", 1, "success", None

    monkeypatch.setattr("swarm.classifier.classify_task_with_outcome", classify)
    monkeypatch.setattr("swarm.models.mint_task_id", lambda: "task-1")
    monkeypatch.setattr(swarm_router, "_create_task_sync", lambda *args: None)
    monkeypatch.setattr(
        swarm_router,
        "_update_task_inputs_sync",
        lambda *args: updated.append(args),
    )
    monkeypatch.setattr(swarm_router, "_record_classification_sync", lambda *args: None)
    monkeypatch.setattr(
        swarm_router,
        "start_run",
        lambda request: started.append(request) or {"workflow_id": "wf-1"},
    )
    monkeypatch.setattr(
        swarm_router, "_set_task_link_sync", lambda *args, **kwargs: None
    )

    first = client().post(
        "/api/swarm/classify-and-start",
        json={"task": "fix", "model": "terra"},
    )
    second = client().post(
        "/api/swarm/classify-and-start",
        json={
            "task": "fix",
            "task_id": first.json()["task_id"],
            "repo": "jomcgi/homelab",
            "branch": "main",
            "model": "terra",
        },
    )

    assert first.json()["task_id"] == "task-1"
    assert second.status_code == 200
    assert second.json()["task_id"] == "task-1"
    assert second.json()["kind"] == "run"
    assert len(updated) == 1
    assert len(started) == 1


def test_one_shot_without_repo_starts_session(monkeypatch):
    monkeypatch.setenv("SWARM_ENABLED", "true")

    async def classify(_task):
        return "one_shot", 1, "success", None

    async def start_session(_request, _body):
        return {"session_id": 42}

    monkeypatch.setattr("swarm.classifier.classify_task_with_outcome", classify)
    monkeypatch.setattr(swarm_router, "_create_task_sync", lambda *args: None)
    monkeypatch.setattr(swarm_router, "_record_classification_sync", lambda *args: None)
    monkeypatch.setattr("agent_sessions.router.start_session", start_session)
    monkeypatch.setattr(
        swarm_router, "_set_task_link_sync", lambda *args, **kwargs: None
    )

    response = client().post(
        "/api/swarm/classify-and-start",
        json={"task": "explain", "model": "terra"},
    )

    assert response.status_code == 200
    assert response.json()["kind"] == "session"
    assert response.json()["session_id"] == 42


def test_promote_session_carries_model(monkeypatch):
    captured = []
    row = type(
        "Row",
        (),
        {"id": 7, "repo": "jomcgi/homelab", "branch": "main", "model": "qwen"},
    )()
    turn = type("Turn", (), {"prompt": "fix"})()

    class Result:
        def first(self):
            return turn

    class FakeSession:
        def __init__(self, _engine):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def get(self, _model, _session_id):
            return row

        def exec(self, _statement):
            return Result()

    monkeypatch.setattr("core.db.get_engine", lambda: object())
    monkeypatch.setattr("sqlmodel.Session", FakeSession)
    monkeypatch.setattr(
        swarm_router,
        "start_run",
        lambda request: captured.append(request) or {"workflow_id": "wf-promoted"},
    )
    monkeypatch.setattr("swarm.models.mint_task_id", lambda: "task-1")
    monkeypatch.setattr("swarm.models.create_task", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        "swarm.models.append_plan_version", lambda *args, **kwargs: None
    )

    response = client().put(
        "/api/swarm/promote-session",
        json={"session_id": 7},
    )

    assert response.status_code == 200
    assert captured[0].model == "qwen"


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


def test_list_runs_clamps_limit_query_parameter(monkeypatch):
    captured = []

    class FakeSession:
        def __init__(self, engine):
            self.engine = engine

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    def compose(*args, **kwargs):
        captured.append(kwargs["limit"])
        return {"runs": []}

    monkeypatch.setattr("core.db.get_engine", lambda: object())
    monkeypatch.setattr("sqlmodel.Session", FakeSession)
    monkeypatch.setattr("swarm.rows.swarm_session_views", lambda session: {})
    monkeypatch.setattr(swarm_router, "_dbos", lambda: object())
    monkeypatch.setattr(swarm_router, "_server_app_version", lambda: "version")
    monkeypatch.setattr("swarm.view.compose_master", compose)

    assert client().get("/api/swarm/runs?active=false&limit=100").status_code == 200
    assert client().get("/api/swarm/runs?active=false&limit=0").status_code == 200
    assert captured == [50, 1]
