import asyncio
from datetime import datetime, timedelta, timezone

import pytest
from types import SimpleNamespace
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from sqlmodel import Session, create_engine, select

import swarm.router as swarm_router
from swarm import store as swarm_store
from swarm.models import (
    SwarmConductorCall,
    SwarmDecision,
    SwarmPlanNode,
    SwarmPlanVersion,
    SwarmTask,
)


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

    class FakeHandle:
        def __init__(self, attributes):
            self._attributes = attributes

        def get_status(self):
            return SimpleNamespace(attributes=dict(self._attributes))

    class FakeDBOS:
        """Models DBOS's REAL attribute semantics: a write REPLACES the whole
        object (dbos/_sys_db.py update_workflow_attributes is a bare
        .values(attributes=...)). A fake that merged for us would have hidden
        #5417 entirely, which is how the bug survived this long."""

        def __init__(self, stored=None):
            self.stored = dict(stored or {})
            self.attributes = []

        def retrieve_workflow(self, workflow_id):
            return FakeHandle(self.stored)

        async def update_workflow_attributes_async(self, workflow_id, values):
            self.attributes.append((workflow_id, values))
            self.stored = dict(values)

    dbos = FakeDBOS({"plan": {"implementer_model": "sol", "max_attempts": 3}})

    def compose(_dbos, workflow_id):
        if workflow_id != "wf-1":
            raise HTTPException(status_code=404, detail="workflow not found")
        return {"workflow_id": workflow_id, "dbos_status": "PENDING"}

    monkeypatch.setattr(swarm_router.runtime, "init_dbos", lambda: dbos)
    monkeypatch.setattr(swarm_router.runtime, "is_launched", lambda: True)
    monkeypatch.setattr(swarm_router, "_compose_run_view", compose)
    return client(), dbos


@pytest.fixture
def start_engine(monkeypatch, tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'swarm_router_start_test.db'}",
        connect_args={"check_same_thread": False},
    )
    tables = [
        SwarmTask.__table__,
        SwarmPlanVersion.__table__,
        SwarmPlanNode.__table__,
        SwarmConductorCall.__table__,
    ]
    schemas = {table: table.schema for table in tables}
    for table in tables:
        table.schema = None
    try:
        for table in tables:
            table.create(engine)
        monkeypatch.setattr("core.db.get_engine", lambda: engine)
        monkeypatch.setattr(swarm_router, "_CLASSIFICATION_TASKS", {})
        yield engine
    finally:
        for table, schema in schemas.items():
            table.schema = schema


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
        json={"task": "fix", "repo": "jomcgi-org/homelab", "branch": "main"},
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
            "repo": "jomcgi-org/homelab",
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
            "repo": "jomcgi-org/homelab",
            "branch": "main",
            "budget_usd": 2.0,
        },
    )
    assert response.status_code == 200
    assert captured[0][1:] == ("fix", "jomcgi-org/homelab", "main", 2.0, None)


def _submit_and_capture(monkeypatch, payload):
    scheduled = []
    monkeypatch.setattr(swarm_router, "_schedule_classification", scheduled.append)
    response = client().post("/api/swarm/classify-and-start", json=payload)
    assert response.status_code == 200
    assert response.json() == {
        "task_id": response.json()["task_id"],
        "kind": "classifying",
    }
    assert len(scheduled) == 1
    return response.json()["task_id"], scheduled[0]


def test_classify_and_start_returns_before_classifier(monkeypatch, start_engine):
    task_id, context = _submit_and_capture(
        monkeypatch,
        {
            "task": "fix",
            "repo": "jomcgi-org/homelab",
            "branch": "main",
            "model": "terra",
        },
    )
    assert context.task_id == task_id
    assert client().get(f"/api/swarm/tasks/{task_id}/start-status").json() == {
        "kind": "classifying"
    }


def test_planned_run_resolves_through_status(monkeypatch, start_engine):
    async def classify(_task):
        return "planned", 1, "success", None

    started = []
    monkeypatch.setattr("swarm.classifier.classify_task_with_outcome", classify)
    monkeypatch.setattr(
        swarm_router,
        "start_run",
        lambda request: started.append(request) or {"workflow_id": "wf-planned"},
    )
    task_id, context = _submit_and_capture(
        monkeypatch,
        {
            "task": "fix",
            "repo": "jomcgi-org/homelab",
            "branch": "main",
            "model": "terra",
        },
    )

    asyncio.run(swarm_router._classify_and_resolve(context))

    status = client().get(f"/api/swarm/tasks/{task_id}/start-status").json()
    assert status["kind"] == "run"
    assert status["run_id"] == "wf-planned"
    assert started[0].idempotency_key == task_id
    assert started[0].model == "terra"


def test_planned_run_rejects_unknown_model(monkeypatch, start_engine):
    response = client().post(
        "/api/swarm/classify-and-start",
        json={"task": "fix", "model": "invalid"},
    )
    assert response.status_code == 400
    assert "Unknown model" in response.json()["detail"]


def test_planned_without_repo_resolves_needs_input(monkeypatch, start_engine):
    async def classify(_task):
        return "planned", 1, "success", None

    monkeypatch.setattr("swarm.classifier.classify_task_with_outcome", classify)
    task_id, context = _submit_and_capture(
        monkeypatch, {"task": "fix", "model": "terra"}
    )
    asyncio.run(swarm_router._classify_and_resolve(context))

    status = client().get(f"/api/swarm/tasks/{task_id}/start-status").json()
    assert status["kind"] == "needs_input"
    assert status["needs_input"] == {"repo": True, "branch": True}


def test_planned_resubmission_reuses_task_id(monkeypatch, start_engine):
    with Session(start_engine) as session:
        row = SwarmTask(
            id="task-1",
            task_text="fix the original wording",
            conductor_model="spark",
            start_state="needs_input",
        )
        session.add(row)
        session.add(
            SwarmConductorCall(
                task_id="task-1",
                conductor_model="spark",
                tool="classify_task",
                args_json='{"task":"fix the original wording"}',
                outcome="success",
                latency_ms=1,
            )
        )
        session.add(
            SwarmPlanVersion(
                task_id="task-1",
                version=1,
                op="bootstrap",
                author_kind="system",
                author="classifier",
                change_json='{"classification":"planned"}',
                cause_kind="classification",
            )
        )
        session.commit()

    task_id, context = _submit_and_capture(
        monkeypatch,
        {
            "task": "fix the edited wording",
            "task_id": "task-1",
            "repo": "jomcgi-org/homelab",
            "branch": "main",
            "model": "terra",
        },
    )
    assert task_id == "task-1"
    assert context.record_plan is False
    assert context.has_classification is True
    assert context.classification == "planned"
    assert context.task == "fix the edited wording"
    with Session(start_engine) as session:
        row = session.get(SwarmTask, task_id)
        assert row.repo == "jomcgi-org/homelab"
        assert row.start_state == "classifying"
        assert row.task_text == "fix the edited wording"


def test_one_shot_resolves_session_and_login_payload(monkeypatch, start_engine):
    async def classify(_task):
        return "one_shot", 1, "success", None

    async def start_session(_triggered_by, _task_id, _body):
        return {
            "session_id": 42,
            "login_required": True,
            "verification_url": "https://example.test/device",
            "user_code": "CODE-123",
            "grant": "codex-cluster",
            "message": "Approve the Codex login in your browser.",
        }

    monkeypatch.setattr("swarm.classifier.classify_task_with_outcome", classify)
    monkeypatch.setattr("agent_sessions.router.start_session_for_task", start_session)
    task_id, context = _submit_and_capture(
        monkeypatch, {"task": "explain", "model": "luna"}
    )
    asyncio.run(swarm_router._classify_and_resolve(context))

    status = client().get(f"/api/swarm/tasks/{task_id}/start-status").json()
    assert status["kind"] == "session"
    assert status["session_id"] == 42
    assert status["login_required"] is True
    assert status["user_code"] == "CODE-123"


def test_classifier_refusal_never_starts_session(monkeypatch, start_engine):
    async def classify(_task):
        return "one_shot", 1, "success", "policy refusal"

    async def start_session(*_args):
        raise AssertionError("a refusal must not start a session")

    monkeypatch.setattr("swarm.classifier.classify_task_with_outcome", classify)
    monkeypatch.setattr("agent_sessions.router.start_session_for_task", start_session)
    task_id, context = _submit_and_capture(
        monkeypatch, {"task": "refuse this", "model": "terra"}
    )
    asyncio.run(swarm_router._classify_and_resolve(context))

    status = client().get(f"/api/swarm/tasks/{task_id}/start-status").json()
    assert status["kind"] == "refused"
    assert status["refusal_code"] == "policy refusal"
    assert status["message"] == "policy refusal"


def test_classifier_timeout_falls_back_to_session(monkeypatch, start_engine):
    async def classify(_task):
        return "one_shot", 1, "timeout", "classifier timeout"

    async def start_session(*_args):
        return {"session_id": 73}

    monkeypatch.setattr("swarm.classifier.classify_task_with_outcome", classify)
    monkeypatch.setattr("agent_sessions.router.start_session_for_task", start_session)
    task_id, context = _submit_and_capture(
        monkeypatch, {"task": "explain", "model": "terra"}
    )
    asyncio.run(swarm_router._classify_and_resolve(context))

    status = client().get(f"/api/swarm/tasks/{task_id}/start-status").json()
    assert status["kind"] == "session"
    assert status["session_id"] == 73
    with Session(start_engine) as session:
        call = session.exec(select(SwarmConductorCall)).one()
        assert call.outcome == "timeout_fallback"


def test_unparseable_classifier_reply_falls_back_to_session(monkeypatch, start_engine):
    async def classify(_task):
        return "one_shot", 1, "unparseable", "unparseable response"

    async def start_session(*_args):
        return {"session_id": 74}

    monkeypatch.setattr("swarm.classifier.classify_task_with_outcome", classify)
    monkeypatch.setattr("agent_sessions.router.start_session_for_task", start_session)
    task_id, context = _submit_and_capture(
        monkeypatch, {"task": "explain", "model": "terra"}
    )
    asyncio.run(swarm_router._classify_and_resolve(context))

    status = client().get(f"/api/swarm/tasks/{task_id}/start-status").json()
    assert status["kind"] == "session"
    assert status["session_id"] == 74
    with Session(start_engine) as session:
        call = session.exec(select(SwarmConductorCall)).one()
        assert call.outcome == "unparseable_fallback"


def test_stuck_status_reuses_classification_with_compare_and_set(
    monkeypatch, start_engine
):
    old = datetime.now(timezone.utc) - timedelta(minutes=2)
    with Session(start_engine) as session:
        task = SwarmTask(
            id="task-stuck",
            task_text="explain",
            conductor_model="spark",
            start_model="terra",
            start_updated_at=old,
        )
        session.add(task)
        session.add(
            SwarmConductorCall(
                task_id="task-stuck",
                conductor_model="spark",
                tool="classify_task",
                args_json='{"task":"explain"}',
                outcome="success",
                latency_ms=1,
            )
        )
        session.add(
            SwarmPlanVersion(
                task_id="task-stuck",
                version=1,
                op="bootstrap",
                author_kind="system",
                author="classifier",
                change_json='{"classification":"one_shot"}',
                cause_kind="classification",
            )
        )
        session.commit()
    scheduled = []
    monkeypatch.setattr(swarm_router, "_schedule_classification", scheduled.append)

    first = client().get("/api/swarm/tasks/task-stuck/start-status")
    second = client().get("/api/swarm/tasks/task-stuck/start-status")

    assert first.json()["kind"] == "classifying"
    assert second.json()["kind"] == "classifying"
    assert len(scheduled) == 1
    assert scheduled[0].has_classification is True
    assert scheduled[0].classification == "one_shot"

    async def classify(_task):
        raise AssertionError("a re-kick must not call the classifier")

    async def start_session(*_args):
        return {"session_id": 75}

    monkeypatch.setattr("swarm.classifier.classify_task_with_outcome", classify)
    monkeypatch.setattr("agent_sessions.router.start_session_for_task", start_session)
    asyncio.run(swarm_router._classify_and_resolve(scheduled[0]))
    assert client().get("/api/swarm/tasks/task-stuck/start-status").json() == {
        "kind": "session",
        "session_id": 75,
    }


def test_dbos_follower_start_failure_remains_retryable(monkeypatch, start_engine):
    async def classify(_task):
        return "planned", 1, "success", None

    def start_run(_request):
        raise HTTPException(
            status_code=503,
            detail="Swarm DBOS is not launched on this replica",
        )

    monkeypatch.setattr("swarm.classifier.classify_task_with_outcome", classify)
    monkeypatch.setattr(swarm_router, "start_run", start_run)
    task_id, context = _submit_and_capture(
        monkeypatch,
        {
            "task": "fix",
            "repo": "jomcgi-org/homelab",
            "branch": "main",
            "model": "terra",
        },
    )

    asyncio.run(swarm_router._classify_and_resolve(context))

    assert client().get(f"/api/swarm/tasks/{task_id}/start-status").json() == {
        "kind": "classifying"
    }
    with Session(start_engine) as session:
        row = session.get(SwarmTask, task_id)
        assert row.start_state == "starting_run"
        assert row.settled_at is None
        assert "not launched on this replica" in row.start_payload_json


def test_resolution_timeout_persists_terminal_error(monkeypatch, start_engine):
    async def classify(_task):
        return "one_shot", 1, "success", None

    async def start_session(*_args):
        await asyncio.Event().wait()

    monkeypatch.setattr("swarm.classifier.classify_task_with_outcome", classify)
    monkeypatch.setattr("agent_sessions.router.start_session_for_task", start_session)
    monkeypatch.setattr(swarm_router, "_CLASSIFY_RESOLUTION_TIMEOUT_SECONDS", 0.01)
    task_id, context = _submit_and_capture(
        monkeypatch, {"task": "explain", "model": "terra"}
    )

    asyncio.run(swarm_router._classify_and_resolve(context))

    assert client().get(f"/api/swarm/tasks/{task_id}/start-status").json() == {
        "kind": "error",
        "message": "task start timed out",
    }


def test_hard_stuck_status_cancels_local_task_and_rekicks(monkeypatch, start_engine):
    old = datetime.now(timezone.utc) - timedelta(minutes=6)
    with Session(start_engine) as session:
        session.add(
            SwarmTask(
                id="task-hard-stuck",
                task_text="explain",
                conductor_model="spark",
                start_model="terra",
                start_updated_at=old,
            )
        )
        session.commit()
    scheduled = []
    monkeypatch.setattr(swarm_router, "_schedule_classification", scheduled.append)

    async def check_status():
        live_task = asyncio.create_task(asyncio.Event().wait())
        swarm_router._CLASSIFICATION_TASKS["task-hard-stuck"] = live_task
        response = await swarm_router.task_start_status("task-hard-stuck")
        await asyncio.sleep(0)
        return response, live_task

    response, live_task = asyncio.run(check_status())

    assert response.kind == "classifying"
    assert live_task.cancelled()
    assert len(scheduled) == 1


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


def test_follower_serves_run_reads_from_the_read_client(monkeypatch):
    """The read surfaces must answer on a replica that never launched DBOS.

    Two replicas sit behind a Service with no session affinity and DBOS
    launches on the leader only, so gating reads on is_launched made roughly
    half of every console poll 503 and the browser reported the engine
    unreachable while the run was healthy.
    """
    monkeypatch.setenv("SWARM_ENABLED", "true")
    composed = []

    class FakeReadClient:
        def start_workflow(self, *args):  # pragma: no cover - must not be reached
            raise AssertionError("the read client must never submit a workflow")

    read_client = FakeReadClient()

    def compose(dbos, workflow_id):
        composed.append(dbos)
        return {"workflow_id": workflow_id, "dbos_status": "PENDING"}

    monkeypatch.setattr(swarm_router.runtime, "init_dbos", lambda: None)
    monkeypatch.setattr(swarm_router.runtime, "is_launched", lambda: False)
    monkeypatch.setattr(swarm_router.runtime, "read_client", lambda: read_client)
    monkeypatch.setattr(swarm_router, "_compose_run_view", compose)

    response = client().get("/api/swarm/runs/wf-1")

    assert response.status_code == 200
    # The read client, not the unlaunched instance, is what composed the view.
    assert composed == [read_client]


def test_leader_serves_run_reads_from_its_launched_instance(monkeypatch):
    """The leader already holds a pool, so it must not build a second one."""
    monkeypatch.setenv("SWARM_ENABLED", "true")
    composed = []
    leader = object()

    def no_client():  # pragma: no cover - must not be reached
        raise AssertionError("the leader must not construct a read client")

    monkeypatch.setattr(swarm_router.runtime, "init_dbos", lambda: leader)
    monkeypatch.setattr(swarm_router.runtime, "is_launched", lambda: True)
    monkeypatch.setattr(swarm_router.runtime, "read_client", no_client)
    monkeypatch.setattr(
        swarm_router,
        "_compose_run_view",
        lambda dbos, workflow_id: (
            composed.append(dbos)
            or {"workflow_id": workflow_id, "dbos_status": "PENDING"}
        ),
    )

    response = client().get("/api/swarm/runs/wf-1")

    assert response.status_code == 200
    assert composed == [leader]


def test_follower_with_no_read_client_still_refuses(monkeypatch):
    """No DATABASE_URL means no client to read through, and inventing one would
    report an empty console as a real answer."""
    monkeypatch.setenv("SWARM_ENABLED", "true")
    monkeypatch.setattr(swarm_router.runtime, "init_dbos", lambda: None)
    monkeypatch.setattr(swarm_router.runtime, "is_launched", lambda: False)
    monkeypatch.setattr(swarm_router.runtime, "read_client", lambda: None)

    response = client().get("/api/swarm/runs/wf-1")

    assert response.status_code == 503
    assert "not configured" in response.json()["detail"]


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
        json={"task": "fix", "repo": "jomcgi-org/homelab", "branch": "main"},
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
    # The pinned plan must SURVIVE the decision. Before #5417 this write
    # replaced the whole attributes object and unpinned the run, so the console
    # silently fell back to reporting the current deploy's models.
    assert dbos.stored["plan"] == {"implementer_model": "sol", "max_attempts": 3}
    assert dbos.attributes == [
        (
            "wf-1",
            {
                "plan": {"implementer_model": "sol", "max_attempts": 3},
                "decided_by": {
                    "actor": "alice@example.com",
                    "at": body["decided_at"],
                },
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

        def retrieve_workflow(self, workflow_id):
            return SimpleNamespace(
                get_status=lambda: SimpleNamespace(attributes={"plan": {"v": 1}})
            )

        async def update_workflow_attributes_async(self, workflow_id, values):
            events.append(("attributes", workflow_id, values["cancelled_by"]["actor"]))
            events.append(("preserved", values.get("plan")))

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
        # Cancelling must not unpin the run on its way out (#5417).
        ("preserved", {"v": 1}),
    ]


def test_attribute_write_is_skipped_when_the_prior_read_fails(monkeypatch):
    """A failed read must not degrade into a blind overwrite.

    The whole point of merging is that the pinned plan survives. If the current
    attributes cannot be read, writing the patch alone would destroy exactly
    what the merge exists to protect, so the metadata is dropped instead and
    the caller's best-effort handler logs it.
    """
    monkeypatch.setenv("SWARM_ENABLED", "true")
    writes = []

    class FakeDBOS:
        async def cancel_workflow_async(self, workflow_id, *, cancel_children=False):
            return None

        def retrieve_workflow(self, workflow_id):
            raise RuntimeError("system database unreachable")

        async def update_workflow_attributes_async(self, workflow_id, values):
            writes.append(values)

    async def reap(workflow_id):
        return {"reaped": [], "failed": [], "skipped": []}

    monkeypatch.setattr(swarm_router.runtime, "init_dbos", lambda: FakeDBOS())
    monkeypatch.setattr(swarm_router.runtime, "is_launched", lambda: True)
    monkeypatch.setattr("agent_sessions.api.reap_sessions_for_workflow", reap)

    response = client().post("/api/swarm/runs/wf-1/cancel")

    # The cancel itself still succeeds; only the actor record is lost.
    assert response.status_code == 200
    assert writes == []


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
    # list_runs is a READ, so it resolves through _dbos_read and answers on a
    # follower replica that never launched DBOS.
    monkeypatch.setattr(swarm_router, "_dbos_read", lambda: object())
    monkeypatch.setattr(swarm_router, "_server_app_version", lambda: "version")
    monkeypatch.setattr("swarm.view.compose_master", compose)

    assert client().get("/api/swarm/runs?active=false&limit=100").status_code == 200
    assert client().get("/api/swarm/runs?active=false&limit=0").status_code == 200
    assert captured == [50, 1]


def test_update_task_inputs_refuses_a_task_not_awaiting_inputs(monkeypatch):
    """`task_id` comes from the client, so the row it names has to be checked.

    A resubmission may only fill in a task waiting for inputs or retrying a
    terminal error. A workflow or session link still makes the task ineligible
    even if its state is stale.
    """

    class Row:
        def __init__(self, **kwargs):
            self.task_text = kwargs.get("task_text", "original task")
            self.repo = kwargs.get("repo")
            self.base_branch = None
            self.workflow_id = kwargs.get("workflow_id")
            self.session_id = kwargs.get("session_id")
            self.start_state = kwargs.get("start_state", "needs_input")
            self.start_model = None
            self.start_triggered_by = None
            self.start_payload_json = None
            self.start_updated_at = None

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
        swarm_router._update_task_inputs_sync(
            "task-1",
            "edited task",
            "jomcgi/homelab",
            "main",
            "terra",
            "joe@example.com",
        )

    for started in (
        Row(start_state="session"),
        Row(workflow_id="wf-1"),
        Row(session_id=7),
    ):
        with pytest.raises(ValueError, match="not awaiting inputs"):
            run_with(started)

    # The awaiting case still writes.
    for awaiting in (Row(), Row(start_state="error")):
        run_with(awaiting)
        assert awaiting.task_text == "edited task"
        assert awaiting.repo == "jomcgi/homelab"
        assert awaiting.base_branch == "main"
        assert awaiting.start_model == "terra"
        assert awaiting.start_triggered_by == "joe@example.com"
        assert awaiting.start_state == "classifying"
