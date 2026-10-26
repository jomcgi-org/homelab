from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest
from sqlalchemy import event
from sqlmodel import Session, SQLModel, create_engine, select

from auth.api import Authority, Principal, PrincipalKind, get_principal
from factory.orchestration import factory_controls, factory_router, work_items
from factory.orchestration.factory_models import (
    FactoryAudit,
    FactoryControl,
    FactoryReceipt,
    WorkItem,
    WorkItemEvent,
)
from factory.orchestration.factory_router import router
from factory.orchestration.models import SwarmTask

DECISION_ID = "decision:" + "a" * 64


@pytest.fixture
def receipt_db(tmp_path, monkeypatch):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'router-receipt.db'}",
        connect_args={"check_same_thread": False, "timeout": 5},
        execution_options={"schema_translate_map": {"swarm": None}},
    )

    @event.listens_for(engine, "connect")
    def foreign_keys(connection, _record):
        connection.execute("PRAGMA foreign_keys=ON")

    SQLModel.metadata.create_all(
        engine,
        tables=[
            model.__table__
            for model in (
                SwarmTask,
                FactoryControl,
                FactoryAudit,
                WorkItem,
                WorkItemEvent,
                FactoryReceipt,
            )
        ],
    )
    with Session(engine) as session:
        session.add(FactoryControl(id="factory", actor="test"))
        session.commit()
    monkeypatch.setattr(factory_controls, "get_engine", lambda: engine)
    monkeypatch.setattr(work_items, "get_engine", lambda: engine)
    yield engine
    engine.dispose()


@pytest.mark.parametrize(
    "authority,kind,groups",
    [
        (Authority.ANONYMOUS, PrincipalKind.HUMAN, ("operators",)),
        (Authority.DELEGATED, PrincipalKind.HUMAN, ("operators",)),
        (Authority.STANDING, PrincipalKind.WORKLOAD, ("operators",)),
        (Authority.STANDING, PrincipalKind.HUMAN, ()),
    ],
)
def test_factory_controls_require_verified_standing_operator(authority, kind, groups):
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_principal] = lambda: Principal(
        subject="test",
        actor=(),
        scope=(),
        groups=groups,
        email=None,
        kind=kind,
        authority=authority,
    )
    client = TestClient(app)
    assert client.get("/api/swarm/factory/improvements/context").status_code == 403
    assert client.post("/api/swarm/factory/improvements", json={}).status_code == 403
    assert client.post("/api/swarm/factory/synthetics", json={}).status_code == 403
    response = client.post(
        "/api/swarm/factory/control",
        json={"action": "enable"},
        headers={"Cf-Access-Authenticated-User-Email": "spoof@example.test"},
    )
    assert response.status_code == 403
    assert (
        client.get(
            "/api/swarm/factory/attempt-stop",
            params={
                "task_id": "task",
                "node_key": "node",
                "attempt": 1,
                "session_id": 1,
            },
        ).status_code
        == 403
    )
    assert (
        client.post(
            "/api/swarm/factory/control",
            json={
                "action": "stop_attempt",
                "task_id": "task",
                "node_key": "node",
                "attempt": 1,
                "session_id": 1,
                "request_key": "request",
                "expected_identity_sha256": "a" * 64,
                "reason": "Exact attempt response lost",
            },
        ).status_code
        == 403
    )


@pytest.mark.parametrize(
    "field,value",
    [("attempt", True), ("attempt", "1"), ("session_id", True), ("session_id", "1")],
)
def test_attempt_stop_identity_rejects_coerced_numbers(field, value):
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_principal] = lambda: Principal(
        subject="operator:test",
        actor=(),
        scope=(),
        groups=("operators",),
        email=None,
        kind=PrincipalKind.HUMAN,
        authority=Authority.STANDING,
    )
    body = {
        "action": "stop_attempt",
        "task_id": "task",
        "node_key": "node",
        "attempt": 1,
        "session_id": 1,
        "request_key": "request",
        "expected_identity_sha256": "a" * 64,
        "reason": "Exact attempt response lost",
    }
    body[field] = value
    assert (
        TestClient(app).post("/api/swarm/factory/control", json=body).status_code == 422
    )


def operator_client(subject="operator:test"):
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_principal] = lambda: Principal(
        subject=subject,
        actor=(),
        scope=(),
        groups=("operators",),
        email=None,
        kind=PrincipalKind.HUMAN,
        authority=Authority.STANDING,
    )
    return TestClient(app)


def test_synthetics_are_default_off_and_reject_unbounded_input(monkeypatch):
    from factory.orchestration import synthetic_actions

    client = operator_client()
    body = {
        "run_id": "00000000-0000-0000-0000-000000000001",
        "chart_version": "1.2.3",
        "image": "example@sha256:" + "b" * 64,
        "source_commit": "a" * 40,
        "promotion": "prod.synthetic",
    }
    monkeypatch.delenv("FACTORY_SYNTHETICS_ENABLED", raising=False)
    monkeypatch.setattr(
        synthetic_actions, "verify", lambda *a, **kw: pytest.fail("ran while disabled")
    )
    assert client.post("/api/swarm/factory/synthetics", json=body).status_code == 404
    monkeypatch.setenv("FACTORY_SYNTHETICS_ENABLED", "true")
    assert (
        client.post(
            "/api/swarm/factory/synthetics", json={**body, "samples": 10000}
        ).status_code
        == 422
    )
    seen = {}

    def verify(_engine, **kwargs):
        seen.update(kwargs)
        return {"verdict": "pass"}

    monkeypatch.setattr(synthetic_actions, "verify", verify)
    assert client.post("/api/swarm/factory/synthetics", json=body).json() == {
        "verdict": "pass"
    }
    assert seen == body


def test_synthetic_database_errors_do_not_expose_sql_or_credentials(monkeypatch):
    from sqlalchemy.exc import SQLAlchemyError
    from factory.orchestration import synthetic_actions

    def failed(*args, **kwargs):
        raise SQLAlchemyError("connection secret and SQL parameters")

    monkeypatch.setenv("FACTORY_SYNTHETICS_ENABLED", "true")
    monkeypatch.setattr(synthetic_actions, "verify", failed)
    response = operator_client().post(
        "/api/swarm/factory/synthetics",
        json={
            "run_id": "00000000-0000-0000-0000-000000000001",
            "chart_version": "1.2.3",
            "image": "example",
            "source_commit": "a" * 40,
            "promotion": "prod.synthetic",
        },
    )
    assert response.json() == {"verdict": "inconclusive", "reason": "SQLAlchemyError"}


def test_http_controls_use_the_versioned_request_owner(monkeypatch):
    seen = {}

    def request_control(action, actor, **kwargs):
        seen.update(action=action, actor=actor, **kwargs)
        return {
            "ok": True,
            "state": "paused",
            "version": 8,
            "request_key": kwargs["request_key"],
        }

    monkeypatch.setattr(factory_controls, "request_control", request_control)
    response = operator_client("operator:joe").post(
        "/api/swarm/factory/control",
        json={
            "action": "pause_task",
            "task_id": "task-7",
            "request_key": "pause-task-7",
            "expected_version": 7,
        },
    )

    assert response.status_code == 200
    assert seen == {
        "action": "pause_task",
        "actor": "operator:joe",
        "request_key": "pause-task-7",
        "expected_version": 7,
        "task_id": "task-7",
    }


def test_http_repo_actions_take_only_a_repo(monkeypatch):
    seen = {}

    def set_control(action, actor, **kwargs):
        seen.update(action=action, actor=actor, **kwargs)
        return {"ok": True, "state": "enabled", "version": 3}

    monkeypatch.setattr(factory_controls, "set_control", set_control)
    monkeypatch.setattr(factory_router, "REPO_CATALOG", {"owner/repo": {}})
    response = operator_client("operator:joe").post(
        "/api/swarm/factory/control",
        json={"action": "pause_repo", "repo": "owner/repo"},
    )
    assert response.status_code == 200
    assert seen == {
        "action": "pause_repo",
        "actor": "operator:joe",
        "repo": "owner/repo",
    }
    refused = operator_client().post(
        "/api/swarm/factory/control",
        json={"action": "pause_repo", "repo": "owner/repo", "task_id": "task-7"},
    )
    assert refused.status_code == 422
    missing = operator_client().post(
        "/api/swarm/factory/control", json={"action": "pause_repo"}
    )
    assert missing.status_code == 422


@pytest.mark.parametrize(
    "body",
    [
        {"action": "stop"},
        {"action": "stop", "request_key": "stop"},
        {"action": "stop", "expected_version": 1},
        {"action": "stop", "request_key": "stop", "expected_version": "1"},
        {
            "action": "configure",
            "policy": {},
            "request_key": "policy",
            "expected_version": 1,
        },
    ],
)
def test_http_controls_refuse_missing_or_ambiguous_request_identity(body):
    assert (
        operator_client().post("/api/swarm/factory/control", json=body).status_code
        == 422
    )


def test_http_stale_control_refusal_is_durable_after_version_advances(receipt_db):
    body = {
        "action": "stop",
        "request_key": "stale-stop",
        "expected_version": 1,
    }
    first = operator_client("operator:joe").post(
        "/api/swarm/factory/control", json=body
    )
    assert first.status_code == 409
    assert first.json()["detail"]["reason"] == "control_version_changed"

    with Session(receipt_db) as db, db.begin():
        control = db.get(FactoryControl, "factory")
        control.version = 1
        db.add(control)

    replay = operator_client("operator:joe").post(
        "/api/swarm/factory/control", json=body
    )
    assert replay.status_code == 409
    assert replay.json() == first.json()
    with Session(receipt_db) as db:
        records = db.exec(
            select(FactoryAudit).where(
                FactoryAudit.action == "control_request",
                FactoryAudit.actor == "operator:joe",
            )
        ).all()
        assert len(records) == 1


def test_issue_endpoint_links_receipt_to_fetched_work_item(receipt_db, monkeypatch):
    from factory.orchestration import factory_conductor

    issue = {
        "number": 12,
        "title": "Issue 12",
        "body": "body",
        "html_url": "https://github.com/owner/repo/issues/12",
        "state": "open",
        "labels": [],
        "user": {"login": "jomcgi", "type": "User"},
        "created_at": "2026-09-19T12:00:00Z",
    }
    monkeypatch.setattr(factory_router, "REPO_CATALOG", {"owner/repo": {}})
    monkeypatch.setattr(factory_conductor, "github_get", lambda *_args: issue)
    monkeypatch.setattr(factory_conductor, "github_list", lambda *_args: [])
    response = operator_client().post(
        "/api/swarm/factory/issues",
        json={"repo": "owner/repo", "issue_number": 12},
    )
    assert response.status_code == 200
    with Session(receipt_db) as session:
        receipt = session.exec(select(FactoryReceipt)).one()
        assert receipt.work_item_id == session.exec(select(WorkItem.id)).one()


@pytest.mark.parametrize(
    "authority,kind,groups",
    [
        (Authority.ANONYMOUS, PrincipalKind.HUMAN, ("operators",)),
        (Authority.DELEGATED, PrincipalKind.HUMAN, ("operators",)),
        (Authority.STANDING, PrincipalKind.WORKLOAD, ("operators",)),
        (Authority.STANDING, PrincipalKind.HUMAN, ()),
    ],
)
def test_decisions_require_verified_standing_operator(authority, kind, groups):
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_principal] = lambda: Principal(
        subject="test",
        actor=(),
        scope=(),
        groups=groups,
        email=None,
        kind=kind,
        authority=authority,
    )
    client = TestClient(app)
    assert (
        client.post(
            "/api/swarm/factory/decisions/1",
            json={
                "option_key": "close",
                "decision_id": DECISION_ID,
                "request_key": "close-1",
            },
            headers={"Cf-Access-Authenticated-User-Email": "spoof@example.test"},
        ).status_code
        == 403
    )
    assert client.get("/api/swarm/factory/escalations").status_code == 403


@pytest.mark.parametrize(
    "body",
    [
        {},
        {
            "option_key": "close",
            "action": "chat",
            "note": "hm",
            "decision_id": DECISION_ID,
            "request_key": "both",
        },
        {"action": "resolve", "decision_id": DECISION_ID, "request_key": "bad"},
        {"action": "chat", "decision_id": DECISION_ID, "request_key": "empty"},
        {
            "action": "chat",
            "note": "   ",
            "decision_id": DECISION_ID,
            "request_key": "blank",
        },
        {
            "option_key": "close",
            "decision_id": DECISION_ID,
            "request_key": "extra",
            "unexpected": 1,
        },
        {"option_key": "close", "decision_id": "not-an-identity", "request_key": "bad"},
        {"option_key": "close", "decision_id": DECISION_ID},
    ],
)
def test_a_decision_names_exactly_one_of_an_option_or_a_chat(body):
    assert (
        operator_client().post("/api/swarm/factory/decisions/1", json=body).status_code
        == 422
    )


def test_a_decision_carries_the_operator_subject_as_the_actor(monkeypatch):
    from factory.orchestration import conductor_context, factory_decisions

    seen = {}

    def request_decision(
        receipt_id,
        decision_id,
        option_key,
        actor,
        *,
        request_key,
        note=None,
        action="decide",
    ):
        seen.update(
            receipt_id=receipt_id,
            decision_id=decision_id,
            option_key=option_key,
            actor=actor,
            request_key=request_key,
            note=note,
            action=action,
        )
        return {"ok": True, "state": "completed", "resolution": {}}

    monkeypatch.setattr(factory_decisions, "request_decision", request_decision)
    monkeypatch.setattr(
        conductor_context,
        "report_with_deadline",
        lambda actor, key: {"status": "queued", "actor": actor, "key": key},
    )
    response = operator_client("operator:joe").post(
        "/api/swarm/factory/decisions/12",
        json={
            "option_key": "close",
            "note": "agreed",
            "decision_id": DECISION_ID,
            "request_key": "decision-12",
        },
    )
    assert response.status_code == 200
    assert seen == {
        "receipt_id": 12,
        "decision_id": DECISION_ID,
        "option_key": "close",
        "actor": "operator:joe",
        "request_key": "decision-12",
        "note": "agreed",
        "action": "decide",
    }
    assert response.json()["knowledge"] == {
        "status": "queued",
        "actor": "operator:joe",
        "key": "decision-12",
    }


def test_a_decision_error_becomes_its_own_status(monkeypatch):
    from factory.orchestration import factory_decisions

    def request_decision(*_args, **_kwargs):
        return {
            "ok": False,
            "state": "refused",
            "status": 409,
            "reason": "already decided as close",
        }

    monkeypatch.setattr(factory_decisions, "request_decision", request_decision)
    response = operator_client().post(
        "/api/swarm/factory/decisions/12",
        json={
            "option_key": "hold",
            "decision_id": DECISION_ID,
            "request_key": "hold-12",
        },
    )
    assert response.status_code == 409
    assert response.json()["detail"] == "already decided as close"


def test_improvement_routes_use_existing_operator_and_bounded_request(monkeypatch):
    from factory.orchestration import factory_improvements

    seen = {}
    monkeypatch.setattr(
        factory_improvements, "context", lambda: {"review_digest": "a" * 64}
    )

    def submit(batch, *, actor):
        seen.update(batch=batch, actor=actor)
        return {"mode": "audit_candidate", "reason": "no_actionable_findings"}

    monkeypatch.setattr(factory_improvements, "submit", submit)
    client = operator_client("operator:joe")
    assert client.get("/api/swarm/factory/improvements/context").json() == {
        "review_digest": "a" * 64
    }
    body = {
        "review_digest": "a" * 64,
        "rules_reviewed": True,
        "dedup_reviewed": True,
        "subscription_caps": {},
        "findings": [],
    }
    assert client.post("/api/swarm/factory/improvements", json=body).status_code == 200
    assert seen == {"batch": body, "actor": "operator:joe"}
    assert (
        client.post(
            "/api/swarm/factory/improvements", json={**body, "findings": [{}] * 21}
        ).status_code
        == 422
    )
    assert (
        client.post(
            "/api/swarm/factory/improvements", json={**body, "history": []}
        ).status_code
        == 422
    )


def test_improvement_source_refusal_is_observable(monkeypatch):
    from factory.orchestration import factory_improvements

    def refused(*args, **kwargs):
        raise ValueError("improvement_source_disabled")

    monkeypatch.setattr(factory_improvements, "submit", refused)
    response = operator_client().post(
        "/api/swarm/factory/improvements",
        json={
            "review_digest": "a" * 64,
            "rules_reviewed": True,
            "dedup_reviewed": True,
            "subscription_caps": {},
            "findings": [],
        },
    )
    assert response.status_code == 409
    assert response.json()["detail"] == "improvement_source_disabled"
