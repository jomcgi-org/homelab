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
