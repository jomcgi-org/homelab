"""End-to-end tests for the private moving HTTP router."""

from datetime import date, datetime, timezone
from decimal import Decimal

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlmodel import Session

from core.db import get_session
from moving.models import Milestone, Role, Span, Task, Viewer
from moving.router import router

_EMAIL = "a@example.test"
_HEADERS = {"X-Auth-Email": _EMAIL}


@pytest.fixture(name="client")
def client_fixture(session: Session):
    session.add(Viewer(email=_EMAIL, name="joe"))
    session.commit()

    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_session] = lambda: session
    yield TestClient(app)
    app.dependency_overrides.clear()


def test_state_returns_full_dashboard(client: TestClient, session: Session):
    span = Span(
        kind="move",
        label="Moving week",
        starts_on=date(2026, 9, 10),
        ends_on=date(2026, 9, 12),
    )
    session.add(span)
    session.flush()
    session.add_all(
        [
            Task(
                track="admin",
                title="Submit forms",
                owner="both",
                due_on=date(2026, 9, 10),
                done_at=datetime.now(timezone.utc),
            ),
            Task(track="ship", title="Pack books", owner="anna"),
            Milestone(
                title="Keys collected",
                occurs_on=date(2026, 9, 10),
                owner="both",
            ),
            Role(
                company="Acme",
                title="Engineer",
                stage="screen",
                span_id=span.id,
            ),
        ]
    )
    session.commit()

    response = client.get("/api/moving/state", headers=_HEADERS)
    assert response.status_code == 200
    body = response.json()
    assert set(body) == {
        "tasks",
        "milestones",
        "spans",
        "roles",
        "collisions",
        "progress",
        "viewer",
    }
    assert body["viewer"] == "joe"
    assert body["progress"] == 0.5
    assert len(body["tasks"]) == 2
    assert body["milestones"][0]["title"] == "Keys collected"
    assert body["spans"][0]["label"] == "Moving week"
    assert body["roles"][0]["company"] == "Acme"
    assert body["collisions"] == [
        {
            "type": "task_span",
            "item1_id": body["tasks"][0]["id"],
            "item2_id": body["spans"][0]["id"],
            "overlaps_from": "2026-09-10",
            "overlaps_to": "2026-09-10",
        }
    ]


def test_state_has_zero_progress_without_tasks(client: TestClient):
    response = client.get("/api/moving/state", headers=_HEADERS)
    assert response.status_code == 200
    assert response.json()["progress"] == 0.0


def test_create_and_patch_task(client: TestClient, session: Session):
    created = client.post(
        "/api/moving/tasks",
        headers=_HEADERS,
        json={
            "track": "sell",
            "title": "Sell car",
            "owner": "joe",
            "due_on": "2026-09-01",
            "value_cad": "125.50",
        },
    )
    assert created.status_code == 201
    task_id = created.json()["id"]
    assert created.json()["done_at"] is None
    assert session.get(Task, task_id).title == "Sell car"

    patched = client.patch(
        f"/api/moving/tasks/{task_id}",
        headers=_HEADERS,
        json={"title": "Sell the car", "note": "Photograph it", "due_on": None},
    )
    assert patched.status_code == 200
    assert patched.json()["title"] == "Sell the car"
    assert patched.json()["note"] == "Photograph it"
    assert patched.json()["due_on"] is None
    session.expire_all()
    assert session.get(Task, task_id).value_cad == Decimal("125.50")


def test_patch_rejects_null_title_and_unknown_task(client: TestClient):
    created = client.post(
        "/api/moving/tasks", headers=_HEADERS, json={"title": "Keep title"}
    )
    task_id = created.json()["id"]

    null_title = client.patch(
        f"/api/moving/tasks/{task_id}", headers=_HEADERS, json={"title": None}
    )
    assert null_title.status_code == 422

    missing = client.patch(
        "/api/moving/tasks/missing", headers=_HEADERS, json={"note": "none"}
    )
    assert missing.status_code == 404


def test_delete_task(client: TestClient, session: Session):
    created = client.post(
        "/api/moving/tasks", headers=_HEADERS, json={"title": "Discard boxes"}
    )
    task_id = created.json()["id"]

    deleted = client.delete(f"/api/moving/tasks/{task_id}", headers=_HEADERS)
    assert deleted.status_code == 204
    assert session.get(Task, task_id) is None

    missing = client.delete(f"/api/moving/tasks/{task_id}", headers=_HEADERS)
    assert missing.status_code == 404


def test_done_and_undone_are_idempotent(client: TestClient):
    created = client.post(
        "/api/moving/tasks", headers=_HEADERS, json={"title": "Finish me"}
    )
    task_id = created.json()["id"]

    first_done = client.post(f"/api/moving/tasks/{task_id}/done", headers=_HEADERS)
    second_done = client.post(f"/api/moving/tasks/{task_id}/done", headers=_HEADERS)
    assert first_done.status_code == 200
    assert first_done.json()["done_at"] is not None
    assert second_done.json()["done_at"] == first_done.json()["done_at"]

    first_undone = client.post(f"/api/moving/tasks/{task_id}/undone", headers=_HEADERS)
    second_undone = client.post(f"/api/moving/tasks/{task_id}/undone", headers=_HEADERS)
    assert first_undone.status_code == 200
    assert first_undone.json()["done_at"] is None
    assert second_undone.json()["done_at"] is None
