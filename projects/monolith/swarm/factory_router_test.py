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
