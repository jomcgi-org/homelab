from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from auth.api import Authority, Principal, PrincipalKind, get_principal
from swarm.factory_router import router


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
            json={"option_key": "close"},
            headers={"Cf-Access-Authenticated-User-Email": "spoof@example.test"},
        ).status_code
        == 403
    )
    assert client.get("/api/swarm/factory/escalations").status_code == 403


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"option_key": "close", "action": "chat", "note": "hm"},
        {"action": "resolve"},
        {"action": "chat"},
        {"action": "chat", "note": "   "},
        {"option_key": "close", "unexpected": 1},
    ],
)
def test_a_decision_names_exactly_one_of_an_option_or_a_chat(body):
    assert (
        operator_client().post("/api/swarm/factory/decisions/1", json=body).status_code
        == 422
    )


def test_a_decision_carries_the_operator_subject_as_the_actor(monkeypatch):
    from swarm import factory_decisions

    seen = {}

    def apply_decision(receipt_id, option_key, actor, note=None):
        seen.update(
            receipt_id=receipt_id, option_key=option_key, actor=actor, note=note
        )
        return {"ok": True, "applied": True, "resolution": {}}

    monkeypatch.setattr(factory_decisions, "apply_decision", apply_decision)
    response = operator_client("operator:joe").post(
        "/api/swarm/factory/decisions/12",
        json={"option_key": "close", "note": "agreed"},
    )
    assert response.status_code == 200
    assert seen == {
        "receipt_id": 12,
        "option_key": "close",
        "actor": "operator:joe",
        "note": "agreed",
    }


def test_a_decision_error_becomes_its_own_status(monkeypatch):
    from swarm import factory_decisions

    def apply_decision(*_args, **_kwargs):
        raise factory_decisions.DecisionError(409, "already decided as close")

    monkeypatch.setattr(factory_decisions, "apply_decision", apply_decision)
    response = operator_client().post(
        "/api/swarm/factory/decisions/12", json={"option_key": "hold"}
    )
    assert response.status_code == 409
    assert response.json()["detail"] == "already decided as close"
