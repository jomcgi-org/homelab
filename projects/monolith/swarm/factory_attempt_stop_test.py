"""Operator attempt-stop acceptance through durable owners and external fakes."""

import asyncio
import copy
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import json
import sqlite3
from threading import Event
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient
import httpx
import pytest
from sqlalchemy import event
from sqlmodel import Session, SQLModel, create_engine, select

from agent_sessions import (
    admission,
    execution_api,
    mcp,
    result_receipts,
    store,
    transport,
)
from agent_sessions.constants import UNKNOWN_INVOCATION
from agent_sessions.models import (
    AgentCapacityPool,
    AgentCapacityReservation,
    AgentResultReceipt,
    AgentSession,
    AgentTurn,
    PendingMessage,
)
from auth.api import Authority, Principal, PrincipalKind, get_principal
from core import db as core_db
from goosecracker.api import REPO_CATALOG
from swarm import factory_conductor as conductor
from swarm import factory_controls as controls
from swarm import graph
from swarm.factory_intake import admit_next, receive_issue
from swarm.factory_models import (
    FactoryAudit,
    FactoryControl,
    FactoryReceipt,
    FactoryStart,
)
from swarm.factory_router import router
from swarm.models import (
    SwarmConductorCall,
    SwarmNodeRun,
    SwarmPlanNode,
    SwarmPlanVersion,
    SwarmTask,
)

HEAD = "a" * 40
REPO = next(iter(REPO_CATALOG))


class Workflows:
    """Only the DBOS boundary is fake; all factory transitions remain real."""

    def __init__(self):
        self.states = {}
        self.started = []
        self.cancelled = []

    def get_workflow_status(self, key):
        status = self.states.get(key)
        return None if status is None else SimpleNamespace(status=status)

    def cancel_workflow(self, key, *, cancel_children=False):
        self.cancelled.append((key, cancel_children))
        self.states[key] = "CANCELLED"

    def start_workflow(self, _function, pin):
        key = pin["workflow_id"]
        assert key not in self.states, "a stopped physical attempt must not be replayed"
        self.started.append(copy.deepcopy(pin))
        self.states[key] = "PENDING"


@pytest.fixture
def database(tmp_path, monkeypatch):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'factory-attempt-stop.db'}",
        connect_args={"check_same_thread": False, "timeout": 3},
        execution_options={
            "schema_translate_map": {"swarm": None, "agent_sessions": None}
        },
    )

    @event.listens_for(engine, "connect")
    def foreign_keys(connection, _record):
        connection.execute("PRAGMA foreign_keys=ON")

    models = (
        SwarmTask,
        SwarmPlanVersion,
        SwarmPlanNode,
        SwarmNodeRun,
        SwarmConductorCall,
        FactoryControl,
        FactoryReceipt,
        FactoryStart,
        FactoryAudit,
        AgentSession,
        AgentTurn,
        PendingMessage,
        AgentCapacityPool,
        AgentCapacityReservation,
        AgentResultReceipt,
    )
    SQLModel.metadata.create_all(engine, tables=[model.__table__ for model in models])
    with Session(engine) as db:
        db.add(FactoryControl(id="factory", actor="migration"))
        db.commit()
    for module in (
        conductor,
        controls,
        graph,
        admission,
        execution_api,
        mcp,
        result_receipts,
        store,
        core_db,
    ):
        monkeypatch.setattr(module, "get_engine", lambda: engine)
    monkeypatch.setenv("AGENT_RESULT_RECEIPTS_ENABLED", "false")
    monkeypatch.setenv("AGENT_RESULT_RECEIPT_ADOPTION_ENABLED", "false")
    monkeypatch.setenv("FACTORY_STOP_SUPERVISION_ENABLED", "true")
    # Dispatch is driven explicitly by the test, using the same pending executor.
    monkeypatch.setattr(mcp, "_schedule_next_message", lambda _sid: None)
    monkeypatch.setattr(execution_api, "_schedule_next_message", lambda _sid: None)

    async def notify(*_args, **_kwargs):
        return None

    monkeypatch.setattr(mcp, "_notify_terminal", notify)

    def github(repo, suffix):
        assert repo == REPO and suffix.startswith("git/ref/heads/factory%2F")
        return {"object": {"sha": HEAD}}

    monkeypatch.setattr(conductor, "github_get", github)
    yield engine
    engine.dispose()


@pytest.fixture
def operator_client():
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_principal] = lambda: Principal(
        subject="operator@example.test",
        actor=(),
        scope=(),
        groups=("operators",),
        email="operator@example.test",
        kind=PrincipalKind.HUMAN,
        authority=Authority.STANDING,
    )
    with TestClient(app, raise_server_exceptions=False) as client:
        yield client


def snapshot(engine, sid):
    with Session(engine) as db:
        return {
            "session": db.get(AgentSession, sid).model_dump(),
            **{
                name: [
                    row.model_dump()
                    for row in db.exec(
                        select(model).where(model.session_id == sid)
                    ).all()
                ]
                for name, model in (
                    ("pending", PendingMessage),
                    ("turns", AgentTurn),
                    ("permits", AgentCapacityReservation),
                )
            },
        }


@pytest.fixture
def attempt(database, operator_client, monkeypatch):
    policy = {
        "repo": REPO,
        "issue_numbers": [7, 8],
        "generation": 0,
        "max_tasks": 2,
        "max_turns_per_task": 8,
        "task_budget_usd": 30.0,
        "turn_budget_usd": 2.0,
        "allowed_models": ["opus", "luna"],
        "conductor_model": "opus",
        "worker_model": "luna",
        "base_branch": "main",
        "turn_timeout_seconds": 3600,
        "task_timeout_seconds": 14400,
        "max_attempts": 2,
    }
    for body in ({"action": "configure", "policy": policy}, {"action": "enable"}):
        response = operator_client.post("/api/swarm/factory/control", json=body)
        assert response.status_code == 200 and response.json()["ok"]
    for issue in (7, 8):
        receive_issue(
            REPO,
            issue,
            "Bounded task",
            "Preserve partial delivery",
            f"https://github.com/{REPO}/issues/{issue}",
            "poller",
        )
    admitted = admit_next("scheduler")
    assert admitted["ok"]
    task_id = admitted["task_id"]
    assert graph.add_node(
        task_id,
        author_kind="conductor",
        author="opus",
        cause_kind="condition",
        cause_ref="fixture-plan",
        stated_reason="Implement bounded work",
        expected_version=0,
        node_key="implement_fix",
        kind="work",
        prompt="Implement the bounded fix",
        model="luna",
        deps=[],
        max_cost_usd=2.0,
        side_effects=True,
        max_attempts=2,
        turn_timeout_seconds=3600,
    ).ok
    dbos = Workflows()
    conductor.reconcile_task(task_id, policy, dbos)
    run = graph.node_runs(task_id)[0]
    pin = run["pin"]
    sid = execution_api.start_session_for_swarm(
        f"factory:{task_id}:implement_fix:1",
        pin["prompt"],
        "luna",
        REPO,
        pin["hydration_branch"],
        workflow_id=pin["workflow_id"],
        node_key="implement_fix",
        node_attempt=1,
    )
    with Session(database) as db:
        store.set_ember_session(db, sid, "s-original", "guest-token", None)
        agent = db.get(AgentSession, sid)
        agent.ember_lineage_id = "lineage-preserved"
        db.add(agent)
        db.commit()
    assert graph.record_dispatch(task_id, pin["node_key"], 1, sid, HEAD).ok
    dbos.states[pin["workflow_id"]] = "PENDING"
    result = SimpleNamespace(
        engine=database,
        client=operator_client,
        task_id=task_id,
        policy=policy,
        dbos=dbos,
        sid=sid,
        pin=pin,
        run=graph.node_runs(task_id)[0],
    )
    yield result


class HeartbeatGate:
    """Wake the real executor heartbeat without waiting ten wall-clock seconds."""

    def __init__(self):
        self.waiting = Event()
        self.loop = None
        self.wake = None
        self.cycle = 0
        self.cycles = [Event() for _ in range(4)]

    def __getattr__(self, name):
        return getattr(asyncio, name)

    async def sleep(self, seconds):
        assert seconds == 10
        self.loop = asyncio.get_running_loop()
        self.wake = asyncio.Event()
        self.waiting.set()
        self.cycle += 1
        self.cycles[self.cycle - 1].set()
        await self.wake.wait()
        self.waiting.clear()

    def release(self, cycle=1):
        assert self.cycles[cycle - 1].wait(3), (
            "the executor did not start its heartbeat"
        )
        assert self.cycle == cycle
        self.loop.call_soon_threadsafe(self.wake.set)


class Ember:
    """Fake only the HTTP boundary, including the original still-open model POST."""

    def __init__(self):
        self.precondition = {
            "session_id": "s-original",
            "generation": 0,
            "invoke_started_at": 100,
            "vm_id": "vm-original",
            "node_id": "node-original",
            "instance_id": "node-original/pod-original",
            "pod_uid": "pod-original",
            "boot_id": "boot-original",
        }
        self.view = {
            "session_id": "s-original",
            "state": "running",
            "generation": 0,
            "invoke_started_at": 100,
            "last_invoke_at": None,
            "stop_precondition": copy.deepcopy(self.precondition),
            "stop_intent": None,
            "stop_completion": None,
        }
        self.posts = []
        self.deletes = []
        self.reads = 0
        self.started = Event()
        self.exited = Event()
        self.loop = None
        self.task = None
        self.reply_ready = None
        self.native_record = None
        self.lose_delete_ack = False
        self.on_get = None

    def install(self, monkeypatch):
        ember = self

        class Client:
            def __init__(self, **_kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                pass

            async def post(self, url, **kwargs):
                request = httpx.Request("POST", url, **kwargs)
                assert request.url.path == "/v1/sessions/s-original/invoke"
                ember.posts.append(request)
                ember.loop = asyncio.get_running_loop()
                ember.task = asyncio.current_task()
                ember.reply_ready = asyncio.Event()
                ember.started.set()
                try:
                    await ember.reply_ready.wait()
                    if ember.native_record is not None:
                        return httpx.Response(
                            200, json=ember.native_record, request=request
                        )
                    raise httpx.ReadError(
                        "original response unavailable", request=request
                    )
                finally:
                    ember.exited.set()

            async def get(self, url, **kwargs):
                request = httpx.Request("GET", url, **kwargs)
                assert request.url.path == "/v1/sessions/s-original"
                ember.reads += 1
                if ember.on_get is not None:
                    callback, ember.on_get = ember.on_get, None
                    callback()
                return httpx.Response(
                    200, json=copy.deepcopy(ember.view), request=request
                )

            async def request(self, method, url, **kwargs):
                request = httpx.Request(method, url, **kwargs)
                assert (
                    method == "DELETE" and request.url.path == "/v1/sessions/s-original"
                )
                expected = json.loads(request.content)["stop_precondition"]
                ember.deletes.append(copy.deepcopy(expected))
                assert expected == ember.precondition
                assert expected == ember.view["stop_precondition"], (
                    "stale guest must not be stopped"
                )
                if ember.view["stop_intent"] is None:
                    ember.view["stop_intent"] = {
                        **expected,
                        "operation_id": "node-stop-original",
                        "requested_at_unix_ms": 150,
                    }
                    ember.view["state"] = "destroying"
                if ember.lose_delete_ack:
                    ember.lose_delete_ack = False
                    raise httpx.ReadTimeout(
                        "lost accepted stop acknowledgment", request=request
                    )
                return httpx.Response(
                    202, json={"state": "destroying"}, request=request
                )

            async def delete(self, *_args, **_kwargs):
                pytest.fail(
                    "exact attempt stop must never call legacy unconditioned cleanup"
                )

        monkeypatch.setattr(transport.httpx, "AsyncClient", Client)
        monkeypatch.setattr(transport, "EMBERVM_URL", "https://ember.test")
        monkeypatch.setattr(
            transport, "auth_headers", lambda: {"Authorization": "Bearer test"}
        )
        monkeypatch.setattr(mcp, "_transport", transport.EmberVmShimTransport())

    def complete_stop(self):
        assert self.view["stop_intent"] is not None
        self.view["state"] = "destroyed"
        self.view["stop_completion"] = {
            **self.view["stop_intent"],
            "completed_at_unix_ms": 200,
        }

    def finish_post(self, record=None):
        self.native_record = record
        if self.loop is not None and not self.loop.is_closed():
            self.loop.call_soon_threadsafe(self.reply_ready.set)


@pytest.fixture
def running(attempt, monkeypatch):
    s = attempt
    ember = Ember()
    ember.install(monkeypatch)
    heartbeat = HeartbeatGate()
    monkeypatch.setattr(mcp, "asyncio", heartbeat)
    pool = ThreadPoolExecutor(max_workers=1)
    future = pool.submit(lambda: asyncio.run(mcp._execute_pending_message(s.sid)))
    try:
        if not ember.started.wait(5):
            if future.done():
                future.result()
            pytest.fail("the real pending executor never reached its POST")
        with Session(s.engine) as db, db.begin():
            pending = store.get_pending_message(db, s.sid, 1)
            pending.partial_text = (
                "Pushed partial implementation; native response unavailable"
            )
            db.add(pending)
        s.ember, s.heartbeat, s.executor = ember, heartbeat, future
        s.original = snapshot(s.engine, s.sid)
        s.factory_before = controls.task_snapshot(s.task_id)
        yield s
    finally:
        ember.finish_post()
        try:
            future.result(timeout=5)
        except asyncio.CancelledError:
            pass
        except TimeoutError:
            ember.loop.call_soon_threadsafe(ember.task.cancel)
            try:
                future.result(timeout=5)
            except asyncio.CancelledError:
                pass
            pytest.fail(
                "the test executor did not exit after its response was released"
            )
        finally:
            pool.shutdown(wait=True)


def preview(s):
    response = s.client.get(
        "/api/swarm/factory/attempt-stop",
        params={
            "task_id": s.task_id,
            "node_key": s.pin["node_key"],
            "attempt": 1,
            "session_id": s.sid,
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


def request_body(s, *, key="operator-stop-original"):
    return {
        "action": "stop_attempt",
        "task_id": s.task_id,
        "node_key": s.pin["node_key"],
        "attempt": 1,
        "session_id": s.sid,
        "request_key": key,
        "expected_identity_sha256": preview(s)["identity_sha256"],
        "reason": "Observed failed response; preserve pushed partial branch and unknown cost",
    }


def post_stop(s, body=None):
    response = s.client.post("/api/swarm/factory/control", json=body or request_body(s))
    assert response.status_code == 200, response.text
    assert response.json()["ok"]
    return response.json()


def tick_until(s, condition, limit=8):
    for _ in range(limit):
        if condition():
            return
        conductor.reconcile_task(s.task_id, s.policy, s.dbos)
    assert condition(), (
        "the bounded public conductor path did not reach the expected state"
    )


def audits(s, action):
    with Session(s.engine) as db:
        return [
            row.model_dump()
            for row in db.exec(
                select(FactoryAudit).where(
                    FactoryAudit.task_id == s.task_id,
                    FactoryAudit.action == action,
                )
            ).all()
        ]


def assert_limits_preserved(s):
    current = controls.task_snapshot(s.task_id)
    for key in ("turns_used", "committed_cost_usd", "deadline_at", "policy"):
        assert current[key] == s.factory_before[key]
    assert controls.status()["state"] == "enabled"
    assert not current["cancellation_requested"]
    assert not current["task_paused"]


def test_operator_stop_fences_real_executor_settles_exact_proof_and_continues_same_task(
    running,
):
    s = running
    body = request_body(s)
    post_stop(s, body)
    fenced = snapshot(s.engine, s.sid)
    assert fenced["pending"] == []
    assert len(fenced["turns"]) == 1
    assert fenced["turns"][0]["stop_reason"] == UNKNOWN_INVOCATION
    assert fenced["turns"][0]["cost_usd"] is None
    assert fenced["turns"][0]["result_text"] == s.original["pending"][0]["partial_text"]
    assert fenced["permits"][0]["state"] == "uncertain"
    assert fenced["session"]["ember_session_id"] == "s-original"
    assert s.ember.deletes == []
    assert_limits_preserved(s)
    post_stop(s, body)
    assert len(audits(s, "attempt_stop_requested")) == 1
    assert snapshot(s.engine, s.sid) == fenced
    s.heartbeat.release()
    assert s.ember.exited.wait(3), (
        "the matching original executor did not stop at heartbeat"
    )
    tick_until(s, lambda: s.ember.view["stop_intent"] is not None)
    assert s.dbos.cancelled == [(s.pin["workflow_id"], False)]
    assert s.ember.deletes == [s.ember.precondition]
    assert snapshot(s.engine, s.sid)["permits"][0]["state"] == "uncertain"
    s.ember.complete_stop()
    tick_until(s, lambda: graph.node_runs(s.task_id)[0]["status"] == "failed")
    settled = snapshot(s.engine, s.sid)
    assert settled["turns"] == fenced["turns"]
    assert settled["permits"][0]["state"] == "settled"
    assert settled["permits"][0]["outcome"] == "guest_cessation_confirmed"
    assert settled["session"]["ember_session_id"] is None
    assert settled["session"]["prior_ember_lineage_id"] == "lineage-preserved"
    original_run = graph.node_runs(s.task_id)[0]
    assert original_run["pin"] == s.pin
    assert original_run["head_sha"] == s.run["head_sha"]
    assert_limits_preserved(s)
    tick_until(s, lambda: bool(s.dbos.started))
    assert all(pin["task_id"] == s.task_id for pin in s.dbos.started)
    assert all(pin["workflow_id"] != s.pin["workflow_id"] for pin in s.dbos.started)
    assert all(pin["hydration_branch"] == s.pin["branch"] for pin in s.dbos.started)
    assert len(s.ember.posts) == 1
    assert snapshot(s.engine, s.sid)["turns"] == fenced["turns"]


def test_lost_stop_ack_and_fresh_conductor_reuse_original_durable_operation(running):
    s = running
    post_stop(s)
    s.ember.lose_delete_ack = True
    tick_until(s, lambda: s.ember.view["stop_intent"] is not None)
    original_intent = copy.deepcopy(s.ember.view["stop_intent"])
    # A replacement observer has only durable DBOS state and the database.
    restarted = Workflows()
    restarted.states = dict(s.dbos.states)
    s.dbos = restarted
    conductor.reconcile_task(s.task_id, s.policy, s.dbos)
    s.ember.complete_stop()
    tick_until(s, lambda: graph.node_runs(s.task_id)[0]["status"] == "failed")
    assert s.ember.view["stop_intent"] == original_intent
    assert s.ember.deletes == [s.ember.precondition]
    assert s.dbos.cancelled == []
    assert len(audits(s, "stop_settled")) == 1
    assert len(s.ember.posts) == 1
    assert_limits_preserved(s)


@pytest.mark.parametrize("request_after_empty_stop_read", [False, True])
def test_exact_stop_reaches_real_executor_after_its_lease_observer_loses_claim(
    running, request_after_empty_stop_read
):
    s = running
    body = request_body(s)
    if request_after_empty_stop_read:
        # Wait until the real heartbeat has read no stop request and releases
        # that read transaction. Commit through the public API before its next
        # lease refresh, with no owner helpers replaced or database locks held.
        armed = True
        requested = Event()
        errors = []

        def observe_stop_read(connection, _cursor, _sql, _parameters, context, _many):
            nonlocal armed
            if armed and any(
                "attempt_stop_requested" in parameters.values()
                for parameters in context.compiled_parameters
            ):
                armed = False
                connection.info["request_after_stop_read"] = True

        def request_after_read(_connection, record):
            if not record.info.pop("request_after_stop_read", False):
                return
            try:
                post_stop(s, body)
            except Exception as exc:
                errors.append(exc)
            finally:
                requested.set()

        event.listen(s.engine, "after_cursor_execute", observe_stop_read)
        event.listen(s.engine, "checkin", request_after_read)
        try:
            s.heartbeat.release()
            assert requested.wait(3), "the heartbeat never read the empty stop ledger"
            assert errors == []
            assert s.heartbeat.cycles[1].wait(3), (
                "the observer exited after its failed lease refresh"
            )
        finally:
            event.remove(s.engine, "after_cursor_execute", observe_stop_read)
            event.remove(s.engine, "checkin", request_after_read)
    else:
        with Session(s.engine) as db, db.begin():
            pending = store.get_pending_message(db, s.sid, 1)
            pending.claimed_at = datetime.now(timezone.utc) - timedelta(seconds=60)
            db.add(pending)
        assert store.reclaim_stale_claims_sync() == 1
        unknown = snapshot(s.engine, s.sid)
        assert unknown["turns"][0]["stop_reason"] == UNKNOWN_INVOCATION
        s.heartbeat.release()
        assert s.heartbeat.cycles[1].wait(3), (
            "the observer exited before a later exact stop could arrive"
        )
        post_stop(s, body)
        assert snapshot(s.engine, s.sid)["turns"] == unknown["turns"]
    fenced = snapshot(s.engine, s.sid)
    assert not s.ember.exited.is_set()
    s.heartbeat.release(cycle=2)
    assert s.ember.exited.wait(3), "the original held POST survived its exact stop"
    with pytest.raises(asyncio.CancelledError):
        s.executor.result(timeout=3)
    assert snapshot(s.engine, s.sid) == fenced
    assert len(audits(s, "attempt_stop_requested")) == 1
    assert len(s.ember.posts) == 1
    assert s.ember.deletes == s.dbos.cancelled == []
    assert_limits_preserved(s)


@pytest.mark.parametrize("change_during_get", [False, True])
def test_stale_request_cannot_terminalize_or_stop_a_new_claim_owner(
    running, change_during_get
):
    s = running
    body = request_body(s)
    expected = {}

    def steal():
        with Session(s.engine) as db, db.begin():
            pending = store.get_pending_message(db, s.sid, 1)
            pending.claimed_by_replica = "new-owner"
            pending.dispatch_count += 1
            permit = db.exec(
                select(AgentCapacityReservation).where(
                    AgentCapacityReservation.session_id == s.sid
                )
            ).one()
            permit.owner = "new-owner"
            db.add_all([pending, permit])
        expected.update(snapshot(s.engine, s.sid))

    if change_during_get:
        s.ember.on_get = steal
    else:
        steal()
    response = s.client.post("/api/swarm/factory/control", json=body)
    assert response.status_code == 409, response.text
    assert expected
    assert snapshot(s.engine, s.sid) == expected
    assert audits(s, "attempt_stop_requested") == []
    assert s.ember.deletes == s.dbos.cancelled == []


@pytest.mark.parametrize("complete_during_get", [False, True])
def test_native_completion_before_stop_fence_wins_without_history_rewrite(
    running, complete_during_get
):
    s = running
    body = request_body(s)
    completed = {}

    def finish_native_response():
        s.ember.finish_post(
            {
                "result": "Native result received normally",
                "session_id": "native-cli",
                "terminal_reason": "end_turn",
                "stop_reason": "end_turn",
                "is_error": False,
                "total_cost_usd": 0.25,
                "usage": {"input_tokens": 10},
                "num_turns": 1,
            }
        )
        s.executor.result(timeout=5)
        completed.update(snapshot(s.engine, s.sid))

    if complete_during_get:
        s.ember.on_get = finish_native_response
    else:
        finish_native_response()
    response = s.client.post("/api/swarm/factory/control", json=body)
    assert response.status_code == 409, response.text
    assert completed["turns"][0]["terminal_reason"] == "end_turn"
    assert completed["turns"][0]["cost_usd"] == 0.25
    assert snapshot(s.engine, s.sid) == completed
    assert audits(s, "attempt_stop_requested") == []
    assert s.ember.deletes == s.dbos.cancelled == []


def test_disabled_supervision_refuses_attempt_stop_without_mutation(
    running, monkeypatch
):
    s = running
    body = request_body(s)
    before = snapshot(s.engine, s.sid)
    monkeypatch.setenv("FACTORY_STOP_SUPERVISION_ENABLED", "false")
    response = s.client.post("/api/swarm/factory/control", json=body)
    assert response.status_code == 409, response.text
    assert snapshot(s.engine, s.sid) == before
    assert audits(s, "attempt_stop_requested") == []
    assert s.ember.deletes == s.dbos.cancelled == []


def test_stop_request_rolls_back_intent_and_terminalization_together(running):
    s = running
    body = request_body(s)
    before = snapshot(s.engine, s.sid)

    def fail_unknown_turn(_conn, _cursor, _statement, _parameters, context, _many):
        statement = context.compiled.statement if context.compiled is not None else None
        if (
            getattr(statement, "is_insert", False)
            and statement.table.name == "agent_turns"
        ):
            raise sqlite3.OperationalError("injected terminalization write failure")

    event.listen(s.engine, "before_cursor_execute", fail_unknown_turn)
    try:
        response = s.client.post("/api/swarm/factory/control", json=body)
        assert response.status_code == 500, response.text
    finally:
        event.remove(s.engine, "before_cursor_execute", fail_unknown_turn)
    assert snapshot(s.engine, s.sid) == before
    assert audits(s, "attempt_stop_requested") == []
    assert s.ember.deletes == s.dbos.cancelled == []
    # Retrying the exact request after a confirmed rollback needs no repair.
    post_stop(s, body)
    assert len(audits(s, "attempt_stop_requested")) == 1


def test_final_audit_failure_rolls_back_both_ledgers_and_preserves_retry(running):
    s = running
    post_stop(s)
    tick_until(s, lambda: s.ember.view["stop_intent"] is not None)
    before = snapshot(s.engine, s.sid)
    runs_before = graph.node_runs(s.task_id)
    starts_before = controls.task_snapshot(s.task_id)["starts"]
    s.ember.complete_stop()

    def fail_settlement_audit(_conn, _cursor, _statement, _parameters, context, _many):
        if any(
            parameters.get("action") == "stop_settled"
            for parameters in context.compiled_parameters
        ):
            raise sqlite3.OperationalError("injected final settlement audit failure")

    event.listen(s.engine, "before_cursor_execute", fail_settlement_audit)
    try:
        with pytest.raises(
            sqlite3.OperationalError, match="final settlement audit failure"
        ):
            conductor.reconcile_task(s.task_id, s.policy, s.dbos)
    finally:
        event.remove(s.engine, "before_cursor_execute", fail_settlement_audit)
    assert snapshot(s.engine, s.sid) == before
    assert graph.node_runs(s.task_id) == runs_before
    assert controls.task_snapshot(s.task_id)["starts"] == starts_before
    assert audits(s, "stop_settled") == []
    tick_until(s, lambda: graph.node_runs(s.task_id)[0]["status"] == "failed")
    assert len(audits(s, "stop_settled")) == 1
    assert s.ember.deletes == [s.ember.precondition]
    assert_limits_preserved(s)


@pytest.mark.parametrize(
    "field,value",
    [("operation_id", "another-operation"), ("vm_id", "vm-new"), ("generation", False)],
)
def test_wrong_exact_stop_proof_keeps_attempt_and_capacity_held(running, field, value):
    s = running
    post_stop(s)
    tick_until(s, lambda: s.ember.view["stop_intent"] is not None)
    before = snapshot(s.engine, s.sid)
    s.ember.complete_stop()
    s.ember.view["stop_completion"][field] = value
    for _ in range(3):
        conductor.reconcile_task(s.task_id, s.policy, s.dbos)
    assert snapshot(s.engine, s.sid) == before
    assert graph.node_runs(s.task_id)[0]["status"] == "uncertain"
    assert audits(s, "stop_settled") == []
    assert s.ember.deletes == [s.ember.precondition]
    assert_limits_preserved(s)


def test_exact_attempt_stop_preserves_other_task_and_its_claimed_session(running):
    s = running
    with Session(s.engine) as db:
        db.add(
            SwarmTask(
                id="unrelated-task",
                task_text="Unrelated work",
                conductor_model="opus",
                budget_usd=5,
            )
        )
        db.commit()
    other_sid = execution_api.start_session_for_swarm(
        "unrelated-task-session",
        "Other work",
        "luna",
        REPO,
        "main",
        workflow_id="unrelated-task-workflow",
    )
    with Session(s.engine) as db:
        store.set_ember_session(db, other_sid, "s-unrelated", "other-guest-token", None)
    assert store.claim_pending_message_for_session_sync(other_sid, "other-owner") == 1
    assert admission.recheck(other_sid, 1, "other-owner")
    other_before = snapshot(s.engine, other_sid)
    with Session(s.engine) as db:
        task_before = db.get(SwarmTask, "unrelated-task").model_dump()
        queued_before = (
            db.exec(select(FactoryReceipt).where(FactoryReceipt.issue_number == 8))
            .one()
            .model_dump()
        )
    body = request_body(s)
    post_stop(s, body)
    tick_until(s, lambda: s.ember.view["stop_intent"] is not None)
    s.ember.complete_stop()
    tick_until(s, lambda: graph.node_runs(s.task_id)[0]["status"] == "failed")
    post_stop(s, body)
    assert snapshot(s.engine, other_sid) == other_before
    with Session(s.engine) as db:
        assert db.get(SwarmTask, "unrelated-task").model_dump() == task_before
        assert (
            db.exec(select(FactoryReceipt).where(FactoryReceipt.issue_number == 8))
            .one()
            .model_dump()
            == queued_before
        )
    assert s.dbos.cancelled == [(s.pin["workflow_id"], False)]
    assert s.ember.deletes == [s.ember.precondition]
    assert (
        len(audits(s, "attempt_stop_requested")) == len(audits(s, "stop_settled")) == 1
    )
    assert_limits_preserved(s)
