"""Tests for moving viewer identity resolution."""

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from sqlmodel import Session

from core.db import get_session
from moving.models import Viewer
from moving.viewer import get_viewer

_EMAIL = "a@example.test"


@pytest.fixture(name="client")
def client_fixture(session: Session):
    app = FastAPI()

    @app.get("/viewer")
    async def viewer_name(viewer: str = Depends(get_viewer)) -> dict[str, str]:
        return {"viewer": viewer}

    app.dependency_overrides[get_session] = lambda: session
    yield TestClient(app)
    app.dependency_overrides.clear()


def test_missing_header_is_forbidden(client: TestClient):
    response = client.get("/viewer")
    assert response.status_code == 403
    assert response.json() == {"detail": "Missing X-Auth-Email header"}


def test_unknown_email_is_forbidden(client: TestClient):
    response = client.get("/viewer", headers={"X-Auth-Email": "unknown"})
    assert response.status_code == 403
    assert response.json() == {"detail": "Unknown viewer"}


@pytest.mark.parametrize("name", ["joe", "anna"])
def test_resolves_each_valid_viewer(client: TestClient, session: Session, name: str):
    row = session.get(Viewer, _EMAIL)
    if row is None:
        row = Viewer(email=_EMAIL, name=name)
    else:
        row.name = name
    session.add(row)
    session.commit()

    response = client.get("/viewer", headers={"X-Auth-Email": _EMAIL})
    assert response.status_code == 200
    assert response.json() == {"viewer": name}
