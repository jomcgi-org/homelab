import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from sqlmodel import Session, create_engine, select

import swarm.router as swarm_router
from swarm import store as swarm_store
from swarm.models import SwarmDecision


def client():
    app = FastAPI()
    app.include_router(swarm_router.router)
    return TestClient(app)


@pytest.fixture
def decision_engine(monkeypatch, tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'swarm_router_decision_test.db'}",
        connect_args={"check_same_thread": False},
    )
    table = SwarmDecision.__table__
    schema = table.schema
    table.schema = None
    try:
        table.create(engine)
        monkeypatch.setattr("core.db.get_engine", lambda: engine)
        yield engine
    finally:
        table.schema = schema


@pytest.fixture
def decision_api(monkeypatch, decision_engine):
    monkeypatch.setenv("SWARM_ENABLED", "true")

    class FakeDBOS:
        def __init__(self):
            self.attributes = []

        async def update_workflow_attributes_async(self, workflow_id, values):
            self.attributes.append((workflow_id, values))

    dbos = FakeDBOS()

    def compose(_dbos, workflow_id):
        if workflow_id != "wf-1":
            raise HTTPException(status_code=404, detail="workflow not found")
        return {"workflow_id": workflow_id, "dbos_status": "PENDING"}

    monkeypatch.setattr(swarm_router.runtime, "init_dbos", lambda: dbos)
    monkeypatch.setattr(swarm_router.runtime, "is_launched", lambda: True)
    monkeypatch.setattr(swarm_router, "_compose_run_view", compose)
    return client(), dbos


def _open_decision(engine, *, options=None):
    with Session(engine) as session:
        return swarm_store.open_decision(
            session,
            "wf-1",
            "push_gate",
            "push_gate",
            options or ["approve", "send_back"],
            "Approve the unverified branch?",
        )


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


def test_decide_run_records_header_actor(decision_api, decision_engine):
    test_client, dbos = decision_api
    _open_decision(decision_engine)

    response = test_client.post(
        "/api/swarm/runs/wf-1/nodes/push_gate/decision",
        headers={"Cf-Access-Authenticated-User-Email": "alice@example.com"},
        json={"decision": "approve", "note": "Ship it."},
    )

    assert response.status_code == 200
    body = response.json()
    assert body == {
        "workflow_id": "wf-1",
        "node_key": "push_gate",
        "decision": "approve",
        "decided_at": body["decided_at"],
        "actor_subject": "alice@example.com",
        "idempotent": False,
    }
    assert body["decided_at"] is not None
    assert dbos.attributes == [
        (
            "wf-1",
            {
                "decided_by": {
                    "actor": "alice@example.com",
                    "at": body["decided_at"],
                }
            },
        )
    ]
    with Session(decision_engine) as session:
        row = session.get(SwarmDecision, 1)
        assert row.decision_note == "Ship it."
        assert row.actor_authority == "cloudflare-access"


def test_decide_run_repeat_is_idempotent_and_preserves_anonymous_actor(
    decision_api, decision_engine
):
    test_client, _dbos = decision_api
    _open_decision(decision_engine)

    first = test_client.post(
        "/api/swarm/runs/wf-1/nodes/push_gate/decision",
        json={"decision": "send_back"},
    )
    second = test_client.post(
        "/api/swarm/runs/wf-1/nodes/push_gate/decision",
        headers={"Cf-Access-Authenticated-User-Email": "later@example.com"},
        json={"decision": "send_back", "note": "A repeated click."},
    )

    assert first.status_code == 200
    assert first.json()["actor_subject"] == "operator"
    assert first.json()["idempotent"] is False
    assert second.status_code == 200
    assert second.json()["actor_subject"] == "operator"
    assert second.json()["decided_at"] == first.json()["decided_at"]
    assert second.json()["idempotent"] is True
    with Session(decision_engine) as session:
        row = session.get(SwarmDecision, 1)
        assert row.actor_authority == "anonymous"
        assert row.decision_note is None


def test_decide_run_returns_404_for_unknown_workflow(decision_api):
    test_client, _dbos = decision_api

    response = test_client.post(
        "/api/swarm/runs/missing/nodes/push_gate/decision",
        json={"decision": "approve"},
    )

    assert response.status_code == 404
    assert response.json() == {"detail": "workflow not found"}


def test_decide_run_returns_409_without_open_decision(decision_api):
    test_client, _dbos = decision_api

    response = test_client.post(
        "/api/swarm/runs/wf-1/nodes/push_gate/decision",
        json={"decision": "approve"},
    )

    assert response.status_code == 409
    assert response.json() == {"detail": "no open decision for this node"}


@pytest.mark.parametrize("dbos_status", ["SUCCESS", "CANCELLED"])
def test_decide_run_returns_409_for_finished_workflow(
    decision_api, decision_engine, monkeypatch, dbos_status
):
    test_client, _dbos = decision_api
    _open_decision(decision_engine)
    monkeypatch.setattr(
        swarm_router,
        "_compose_run_view",
        lambda _dbos, workflow_id: {
            "workflow_id": workflow_id,
            "dbos_status": dbos_status,
        },
    )

    response = test_client.post(
        "/api/swarm/runs/wf-1/nodes/push_gate/decision",
        json={"decision": "approve"},
    )

    assert response.status_code == 409
    assert response.json() == {"detail": "workflow is not awaiting a decision"}
    with Session(decision_engine) as session:
        assert swarm_store.get_open_decision(session, "wf-1", "push_gate") is not None


def test_decide_run_rejects_note_over_2000_characters(decision_api, decision_engine):
    test_client, _dbos = decision_api
    _open_decision(decision_engine)

    response = test_client.post(
        "/api/swarm/runs/wf-1/nodes/push_gate/decision",
        json={"decision": "approve", "note": "x" * 2001},
    )

    assert response.status_code == 422
    with Session(decision_engine) as session:
        assert swarm_store.get_open_decision(session, "wf-1", "push_gate") is not None


def test_decide_run_returns_422_with_allowed_options(decision_api, decision_engine):
    test_client, _dbos = decision_api
    _open_decision(decision_engine)

    response = test_client.post(
        "/api/swarm/runs/wf-1/nodes/push_gate/decision",
        json={"decision": "maybe"},
    )

    assert response.status_code == 422
    assert "approve" in response.json()["detail"]
    assert "send_back" in response.json()["detail"]


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


def test_cancel_expires_every_open_decision(monkeypatch, decision_engine):
    monkeypatch.setenv("SWARM_ENABLED", "true")

    class FakeDBOS:
        async def cancel_workflow_async(self, workflow_id, *, cancel_children=False):
            assert workflow_id == "wf-1"
            assert cancel_children is True

        async def update_workflow_attributes_async(self, workflow_id, values):
            return None

    async def reap(_workflow_id):
        return {"reaped": [], "failed": [], "skipped": []}

    with Session(decision_engine) as session:
        swarm_store.open_decision(
            session,
            "wf-1",
            "push_gate",
            "push_gate",
            ["approve", "send_back"],
            None,
        )
        swarm_store.open_decision(
            session,
            "wf-1",
            "review",
            "review_escalation",
            ["retry", "send_back"],
            None,
        )

    monkeypatch.setattr(swarm_router.runtime, "init_dbos", lambda: FakeDBOS())
    monkeypatch.setattr(swarm_router.runtime, "is_launched", lambda: True)
    monkeypatch.setattr("agent_sessions.api.reap_sessions_for_workflow", reap)

    response = client().post("/api/swarm/runs/wf-1/cancel")

    assert response.status_code == 200
    with Session(decision_engine) as session:
        rows = session.exec(select(SwarmDecision)).all()
        assert [row.decision for row in rows] == ["expired", "expired"]
        assert all(row.decided_at is not None for row in rows)


def test_cancel_decision_expiry_is_best_effort(monkeypatch, decision_engine):
    monkeypatch.setenv("SWARM_ENABLED", "true")

    class FakeDBOS:
        async def cancel_workflow_async(self, workflow_id, *, cancel_children=False):
            return None

        async def update_workflow_attributes_async(self, workflow_id, values):
            return None

    async def reap(_workflow_id):
        return {"reaped": [], "failed": [], "skipped": []}

    with Session(decision_engine) as session:
        for node_key, kind in (
            ("push_gate", "push_gate"),
            ("review", "review_escalation"),
        ):
            swarm_store.open_decision(
                session,
                "wf-1",
                node_key,
                kind,
                ["approve", "send_back"],
                None,
            )

    original_expire = swarm_store.expire_decision

    def expire_one(session, workflow_id, node_key):
        if node_key == "push_gate":
            raise RuntimeError("decision store write failed")
        return original_expire(session, workflow_id, node_key)

    monkeypatch.setattr(swarm_store, "expire_decision", expire_one)
    monkeypatch.setattr(swarm_router.runtime, "init_dbos", lambda: FakeDBOS())
    monkeypatch.setattr(swarm_router.runtime, "is_launched", lambda: True)
    monkeypatch.setattr("agent_sessions.api.reap_sessions_for_workflow", reap)

    response = client().post("/api/swarm/runs/wf-1/cancel")

    assert response.status_code == 200
    with Session(decision_engine) as session:
        assert swarm_store.get_open_decision(session, "wf-1", "push_gate") is not None
        assert swarm_store.get_open_decision(session, "wf-1", "review") is None


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


@pytest.mark.parametrize("message", ["workflow not found", "workflow non-existent"])
def test_compose_run_view_maps_dbos_missing_workflow_errors(monkeypatch, message):
    class DBOSMissingWorkflowError(RuntimeError):
        pass

    monkeypatch.setattr(swarm_router, "_session_rows", lambda _workflow_id: [])
    monkeypatch.setattr(swarm_router, "_decision_rows", lambda _workflow_id: [])
    monkeypatch.setattr(swarm_router, "_server_app_version", lambda: "version")
    monkeypatch.setattr(
        "swarm.view.compose_run",
        lambda *args: (_ for _ in ()).throw(DBOSMissingWorkflowError(message)),
    )

    with pytest.raises(HTTPException) as exc_info:
        swarm_router._compose_run_view(object(), "missing")

    assert exc_info.value.status_code == 404
    assert exc_info.value.detail == "workflow not found"


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
    monkeypatch.setattr(
        "swarm.store.list_open_decisions_for", lambda session, workflow_ids: {}
    )
    monkeypatch.setattr(swarm_router, "_dbos", lambda: object())
    monkeypatch.setattr(swarm_router, "_server_app_version", lambda: "version")
    monkeypatch.setattr("swarm.view.compose_master", compose)

    assert client().get("/api/swarm/runs?active=false&limit=100").status_code == 200
    assert client().get("/api/swarm/runs?active=false&limit=0").status_code == 200
    assert captured == [50, 1]


def test_update_task_inputs_refuses_a_task_not_awaiting_inputs(monkeypatch):
    """`task_id` comes from the client, so the row it names has to be checked.

    A resubmission may only fill in a task still waiting for its inputs.
    Anything already carrying a repo, a workflow or a session is somebody
    else's started work, and naming its id must fail rather than silently
    rewrite it.
    """

    class Row:
        def __init__(self, **kwargs):
            self.repo = kwargs.get("repo")
            self.base_branch = None
            self.workflow_id = kwargs.get("workflow_id")
            self.session_id = kwargs.get("session_id")

    class FakeSession:
        def __init__(self, row):
            self._row = row

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def get(self, _model, _task_id):
            return self._row

        def add(self, _row):
            return None

        def commit(self):
            return None

    def run_with(row):
        monkeypatch.setattr(
            "sqlmodel.Session", lambda *args, **kwargs: FakeSession(row)
        )
        swarm_router._update_task_inputs_sync("task-1", "jomcgi/homelab", "main")

    for started in (
        Row(repo="jomcgi/homelab"),
        Row(workflow_id="wf-1"),
        Row(session_id=7),
    ):
        with pytest.raises(ValueError, match="not awaiting inputs"):
            run_with(started)

    # The awaiting case still writes.
    awaiting = Row()
    run_with(awaiting)
    assert awaiting.repo == "jomcgi/homelab"
    assert awaiting.base_branch == "main"
