"""Tests for the faas ingestion API (Task 10): the register-with-test-run gate.

The K8s CR client (faas.workload), the S3 storage (faas.storage), and the
EmberVM submit client (faas.embervm_client) are all monkeypatched: this exercises
the orchestration and rollback logic, not the real backends. respx is not a repo
pip dep, so httpx is faked with a small stub Response and a monkeypatched submit.
"""

from __future__ import annotations

import pytest

from fastapi.testclient import TestClient
from sqlmodel import Session, SQLModel, create_engine

from faas import embervm_client
from faas.repository import get_function


def _detail(resp) -> str:
    """Safely read the FastAPI error detail as a string (satisfies the
    unsafe-json-field-access rule; a missing key yields "")."""
    try:
        return str(resp.json().get("detail", ""))
    except (KeyError, ValueError):
        return ""


class _FakeResponse:
    """Minimal stand-in for httpx.Response (status_code + text)."""

    def __init__(self, status_code: int, text: str = "") -> None:
        self.status_code = status_code
        self.text = text


@pytest.fixture
def session():
    from sqlmodel.pool import StaticPool

    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    original_schemas = {}
    for table in SQLModel.metadata.tables.values():
        if table.schema is not None:
            original_schemas[table.name] = table.schema
            table.schema = None
    try:
        SQLModel.metadata.create_all(engine)
        with Session(engine) as s:
            yield s
    finally:
        for table in SQLModel.metadata.tables.values():
            if table.name in original_schemas:
                table.schema = original_schemas[table.name]


@pytest.fixture
def fakes(monkeypatch):
    """Patch storage / workload / embervm_client with in-memory fakes.

    Returns a mutable dict the test can tweak (ready result, smoke response) and
    inspect (recorded calls). Defaults are the happy path.
    """
    from faas import router as router_mod

    state = {
        "put_calls": [],
        "delete_archive_calls": [],
        "upsert_calls": [],
        "delete_workload_calls": [],
        "submit_calls": [],
        "ready": (True, ""),
        "smoke": _FakeResponse(200, '{"ok": true}'),
        "smoke_raises": 0,  # number of leading transport errors before smoke returns
    }

    monkeypatch.setattr(
        router_mod.storage,
        "put_archive",
        lambda name, sha, data: state["put_calls"].append((name, sha, len(data))),
    )
    monkeypatch.setattr(
        router_mod.storage,
        "code_uri",
        lambda name, sha: f"http://s3/faas/{name}/{sha}.zip",
    )
    monkeypatch.setattr(
        router_mod.storage,
        "delete_archive",
        lambda name, sha: state["delete_archive_calls"].append((name, sha)),
    )

    async def _upsert(name, spec):
        state["upsert_calls"].append((name, spec))

    async def _wait_ready(name, timeout_s=180):
        return state["ready"]

    async def _delete_workload(name):
        state["delete_workload_calls"].append(name)

    monkeypatch.setattr(router_mod.workload, "upsert_workload", _upsert)
    monkeypatch.setattr(router_mod.workload, "wait_ready", _wait_ready)
    monkeypatch.setattr(router_mod.workload, "delete_workload", _delete_workload)

    async def _submit(name, *, body, guest_path, extra_guest_headers, read_timeout):
        state["submit_calls"].append(
            {"name": name, "body": body, "guest_path": guest_path}
        )
        if state["smoke_raises"] > 0:
            state["smoke_raises"] -= 1
            raise embervm_client.EmberVMTransportError("boom")
        return state["smoke"]

    monkeypatch.setattr(router_mod.embervm_client, "submit", _submit)
    return state


@pytest.fixture
def client(session):
    from app.main import app
    from app.db import get_session

    app.dependency_overrides[get_session] = lambda: session
    yield TestClient(app)
    app.dependency_overrides.clear()


def _zip_bytes(size: int = 32) -> bytes:
    return b"PK\x03\x04" + b"z" * (size - 4)


def _post(client, **overrides):
    data = {
        "name": overrides.get("name", "echo-fn"),
        "visibility": overrides.get("visibility", "private"),
        "runtime": overrides.get("runtime", "python312"),
        "handler": overrides.get("handler", "app.handle"),
    }
    if "requirements" in overrides:
        data["requirements"] = overrides["requirements"]
    files = {"zip": ("fn.zip", overrides.get("zip", _zip_bytes()), "application/zip")}
    return client.post("/api/functions", data=data, files=files)


# --------------------------------------------------------------------------- #
# Happy path
# --------------------------------------------------------------------------- #


def test_happy_path_registers_and_makes_visible(client, session, fakes):
    resp = _post(client)
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["name"] == "echo-fn"
    assert body["ready"] is True
    assert body["zip_sha256"]

    # Archive put, CR upserted, smoke ran through the invoke path.
    assert len(fakes["put_calls"]) == 1
    assert len(fakes["upsert_calls"]) == 1
    assert fakes["submit_calls"][0]["guest_path"] == "/invoke"

    # Row exists and is visible (smoke passed -> last_smoke_at set).
    fn = get_function(session, "echo-fn")
    assert fn is not None
    assert fn.last_smoke_at is not None


def test_name_conflict_is_last_write_wins_not_409(client, session, fakes):
    assert _post(client, handler="app.handle").status_code == 201
    # Re-register the same name with a new handler: allowed, overwrites.
    resp = _post(client, handler="app.other")
    assert resp.status_code == 201
    fn = get_function(session, "echo-fn")
    assert fn.handler == "app.other"


# --------------------------------------------------------------------------- #
# Validation rejections (no row persisted)
# --------------------------------------------------------------------------- #


def test_requirements_outside_baked_set_rejected(client, session, fakes):
    resp = _post(client, requirements="requests, numpy")
    assert resp.status_code == 400
    assert "requests" in _detail(resp)
    # numpy is baked, so it must NOT appear in the missing list.
    assert "numpy" not in _detail(resp).split("requests")[0]
    assert get_function(session, "echo-fn") is None
    assert fakes["put_calls"] == []


def test_baked_requirements_accepted(client, session, fakes):
    resp = _post(client, requirements="numpy\nPIL\nyaml")
    assert resp.status_code == 201


def test_unknown_runtime_rejected(client, session, fakes):
    resp = _post(client, runtime="node20")
    assert resp.status_code == 400
    assert "runtime" in _detail(resp)
    assert get_function(session, "echo-fn") is None


def test_bad_name_rejected(client, session, fakes):
    resp = _post(client, name="Echo_FN")
    assert resp.status_code == 400
    assert get_function(session, "Echo_FN") is None


def test_zip_over_cap_rejected(client, session, fakes):
    big = _zip_bytes(8 * 1024 * 1024 + 1)
    resp = _post(client, zip=big)
    assert resp.status_code == 413
    assert get_function(session, "echo-fn") is None
    assert fakes["put_calls"] == []


# --------------------------------------------------------------------------- #
# Ready timeout and smoke failure -> rollback, nothing visible
# --------------------------------------------------------------------------- #


def test_ready_timeout_rolls_back(client, session, fakes):
    fakes["ready"] = (False, "timed out waiting for Ready")
    resp = _post(client)
    assert resp.status_code == 502
    assert "did not become ready" in _detail(resp)
    # CR deleted, row deleted, nothing visible.
    assert fakes["delete_workload_calls"] == ["echo-fn"]
    assert get_function(session, "echo-fn") is None


def test_smoke_5xx_rolls_back_and_does_not_retry(client, session, fakes):
    fakes["smoke"] = _FakeResponse(500, "ImportError: no module named app")
    resp = _post(client)
    assert resp.status_code == 502
    # A guest 5xx is NOT retried: exactly one submit call.
    assert len(fakes["submit_calls"]) == 1
    assert fakes["delete_workload_calls"] == ["echo-fn"]
    assert get_function(session, "echo-fn") is None


def test_smoke_transport_error_retries_once_then_succeeds(client, session, fakes):
    # First submit raises a transport error, the retry returns 200.
    fakes["smoke_raises"] = 1
    resp = _post(client)
    assert resp.status_code == 201
    assert len(fakes["submit_calls"]) == 2  # one failed transport + one success
    fn = get_function(session, "echo-fn")
    assert fn is not None and fn.last_smoke_at is not None


def test_smoke_transport_error_both_attempts_rolls_back(client, session, fakes):
    fakes["smoke_raises"] = 2  # both transport attempts fail
    resp = _post(client)
    assert resp.status_code == 502
    assert len(fakes["submit_calls"]) == 2
    assert fakes["delete_workload_calls"] == ["echo-fn"]
    assert get_function(session, "echo-fn") is None


# --------------------------------------------------------------------------- #
# DELETE
# --------------------------------------------------------------------------- #


def test_delete_removes_cr_and_row(client, session, fakes):
    assert _post(client).status_code == 201
    resp = client.delete("/api/functions/echo-fn")
    assert resp.status_code == 204
    assert "echo-fn" in fakes["delete_workload_calls"]
    assert get_function(session, "echo-fn") is None


def test_delete_unknown_is_404(client, session, fakes):
    resp = client.delete("/api/functions/nope")
    assert resp.status_code == 404
