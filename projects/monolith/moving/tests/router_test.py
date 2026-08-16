"""End-to-end tests for the private moving HTTP router."""

import uuid
from datetime import date, datetime, timezone
from decimal import Decimal

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlmodel import Session

from core.db import get_session
from moving.models import Milestone, Role, Span, Task, Viewer
from moving.router import router
from moving.viewer import get_viewer

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
                owner="both",
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
    assert body["progress"] == 1.0
    assert len(body["tasks"]) == 1
    assert body["milestones"][0]["title"] == "Keys collected"
    assert body["spans"][0]["label"] == "Moving week"
    assert body["roles"][0]["company"] == "Acme"
    tasks_by_title = {task["title"]: task for task in body["tasks"]}
    assert body["collisions"] == [
        {
            "type": "task_span",
            "item1_id": tasks_by_title["Submit forms"]["id"],
            "item2_id": body["spans"][0]["id"],
            "overlaps_from": "2026-09-10",
            "overlaps_to": "2026-09-10",
        }
    ]


def test_state_scope_filters_every_owned_resource_and_progress(
    client: TestClient, session: Session
):
    session.add_all(
        [
            Task(
                title="Joe done",
                owner="joe",
                done_at=datetime.now(timezone.utc),
            ),
            Task(title="Shared pending", owner="both"),
            Task(
                title="Anna done",
                owner="anna",
                done_at=datetime.now(timezone.utc),
            ),
            Milestone(title="Joe milestone", occurs_on=date(2026, 9, 1), owner="joe"),
            Milestone(
                title="Shared milestone", occurs_on=date(2026, 9, 2), owner="both"
            ),
            Milestone(title="Anna milestone", occurs_on=date(2026, 9, 3), owner="anna"),
            Span(
                kind="work",
                label="Joe span",
                starts_on=date(2026, 9, 4),
                ends_on=date(2026, 9, 5),
                owner="joe",
            ),
            Span(
                kind="trip",
                label="Shared span",
                starts_on=date(2026, 9, 6),
                ends_on=date(2026, 9, 7),
                owner="both",
            ),
            Span(
                kind="visitor",
                label="Anna span",
                starts_on=date(2026, 9, 8),
                ends_on=date(2026, 9, 9),
                owner="anna",
            ),
            Role(company="Joe Co", title="Engineer", owner="joe"),
            Role(company="Shared Co", title="Engineer", owner="both"),
            Role(company="Anna Co", title="Engineer", owner="anna"),
        ]
    )
    session.commit()

    mine = client.get("/api/moving/state", headers=_HEADERS)
    assert mine.status_code == 200
    mine_body = mine.json()
    assert {task["title"] for task in mine_body["tasks"]} == {
        "Joe done",
        "Shared pending",
    }
    assert {item["title"] for item in mine_body["milestones"]} == {
        "Joe milestone",
        "Shared milestone",
    }
    assert {span["label"] for span in mine_body["spans"]} == {
        "Joe span",
        "Shared span",
    }
    assert {role["company"] for role in mine_body["roles"]} == {
        "Joe Co",
        "Shared Co",
    }
    assert mine_body["progress"] == 0.5

    all_items = client.get("/api/moving/state?scope=all", headers=_HEADERS)
    assert all_items.status_code == 200
    all_body = all_items.json()
    assert len(all_body["tasks"]) == 3
    assert len(all_body["milestones"]) == 3
    assert len(all_body["spans"]) == 3
    assert len(all_body["roles"]) == 3
    assert all_body["progress"] == pytest.approx(2 / 3)


def test_state_rejects_unknown_scope(client: TestClient):
    response = client.get("/api/moving/state?scope=theirs", headers=_HEADERS)
    assert response.status_code == 422


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


def test_create_task_defaults_owner_to_viewer(client: TestClient):
    created = client.post(
        "/api/moving/tasks", headers=_HEADERS, json={"title": "Joe task"}
    )
    assert created.status_code == 201
    assert created.json()["owner"] == "joe"


def test_patch_rejects_null_title_and_unknown_task(client: TestClient):
    created = client.post(
        "/api/moving/tasks", headers=_HEADERS, json={"title": "Keep title"}
    )
    task_id = created.json()["id"]

    null_title = client.patch(
        f"/api/moving/tasks/{task_id}", headers=_HEADERS, json={"title": None}
    )
    assert null_title.status_code == 422

    malformed = client.patch(
        "/api/moving/tasks/missing", headers=_HEADERS, json={"note": "none"}
    )
    assert malformed.status_code == 422

    missing = client.patch(
        f"/api/moving/tasks/{uuid.uuid4()}",
        headers=_HEADERS,
        json={"note": "none"},
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


@pytest.mark.parametrize(
    ("method", "suffix", "payload"),
    [
        ("PATCH", "", {"title": "Forbidden patch"}),
        ("DELETE", "", None),
        ("POST", "/done", None),
        ("POST", "/undone", None),
    ],
)
def test_other_viewers_task_rejects_every_write(
    client: TestClient,
    session: Session,
    method: str,
    suffix: str,
    payload: dict | None,
):
    task = Task(
        title="Anna task",
        owner="anna",
        done_at=datetime.now(timezone.utc),
    )
    session.add(task)
    session.commit()
    task_id = task.id

    response = client.request(
        method,
        f"/api/moving/tasks/{task_id}{suffix}",
        headers=_HEADERS,
        json=payload,
    )
    assert response.status_code == 403
    session.expire_all()
    assert session.get(Task, task_id) is not None


def _dependency_calls(dependant) -> set[object]:
    calls = {dependant.call}
    for dependency in dependant.dependencies:
        calls.update(_dependency_calls(dependency))
    return calls


def test_every_route_depends_on_get_viewer():
    for route in router.routes:
        assert get_viewer in _dependency_calls(route.dependant), route.path
