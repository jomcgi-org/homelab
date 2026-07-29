"""Tests for the faas invocation router (Task 11): the /functions/<name> surface.

``embervm_client.submit`` is monkeypatched to return synthetic ``httpx.Response``
objects (built directly, not over the wire) so we exercise the marshal/relay
logic, not the real EmberVM. ``get_session`` is overridden with an in-memory
SQLite session (same shape as router_test.py). Functions are inserted directly
via the repository so the visibility gate (``last_smoke_at``) is under test.
"""

from __future__ import annotations

import httpx
import pytest

from datetime import datetime, timezone

from fastapi.testclient import TestClient
from sqlmodel import Session, SQLModel, create_engine

from faas import embervm_client
from faas.models import Function


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


def _add_function(session: Session, name: str, *, visible: bool) -> None:
    now = datetime.now(timezone.utc)
    session.add(
        Function(
            name=name,
            visibility="private",
            runtime="python312",
            handler="app.handle",
            zip_sha256="deadbeef",
            code_uri=f"http://s3/faas/{name}/deadbeef.zip",
            created_by="api",
            created_at=now,
            updated_at=now,
            last_smoke_at=now if visible else None,
        )
    )
    session.commit()


@pytest.fixture
def submitted(monkeypatch):
    """Patch ``submit`` to record its call and return a configurable response.

    The test tweaks ``state["response"]`` (an httpx.Response) or sets
    ``state["raises"]`` to an exception instance; it reads back
    ``state["calls"]`` to assert the marshaling.
    """
    from faas import invoke_router as mod

    state: dict = {
        "calls": [],
        "response": httpx.Response(
            200, headers={"content-type": "application/json"}, content=b'{"ok":true}'
        ),
        "raises": None,
    }

    async def _submit(name, *, body, guest_path, extra_guest_headers, read_timeout):
        state["calls"].append(
            {
                "name": name,
                "body": body,
                "guest_path": guest_path,
                "extra_guest_headers": extra_guest_headers,
                "read_timeout": read_timeout,
            }
        )
        if state["raises"] is not None:
            raise state["raises"]
        return state["response"]

    monkeypatch.setattr(mod.embervm_client, "submit", _submit)
    return state


@pytest.fixture
def client(session):
    import dataclasses
    import faas.module
    from framework import PRIVATE_PROFILE, build_app

    # Compose only the faas domain instead of the whole monolith: the
    # same framework wiring the production app gets, without depending on
    # the app composition root, which imports every other domain.
    app = build_app(
        dataclasses.replace(PRIVATE_PROFILE, otel_enabled=False),
        (faas.module.MODULE,),
    )
    from core.db import get_session

    app.dependency_overrides[get_session] = lambda: session
    yield TestClient(app)
    app.dependency_overrides.clear()


# --------------------------------------------------------------------------- #
# Visibility / not-found
# --------------------------------------------------------------------------- #


def test_unknown_function_is_404(client, session, submitted):
    resp = client.get("/functions/nope")
    assert resp.status_code == 404
    assert resp.json() == {"error": "function not found"}
    assert submitted["calls"] == []  # never submitted


def test_invisible_function_is_404(client, session, submitted):
    # Registered but not yet smoke-passed (last_smoke_at is None) -> 404.
    _add_function(session, "pending", visible=False)
    resp = client.get("/functions/pending")
    assert resp.status_code == 404
    assert submitted["calls"] == []


# --------------------------------------------------------------------------- #
# Happy path marshaling + relay
# --------------------------------------------------------------------------- #


def test_happy_path_marshals_and_relays(client, session, submitted):
    _add_function(session, "echo", visible=True)
    resp = client.get("/functions/echo?title=hi")

    assert resp.status_code == 200
    assert resp.json() == {"ok": True}
    assert resp.headers["content-type"] == "application/json"

    call = submitted["calls"][0]
    assert call["name"] == "echo"
    # Raw query appended to the invoke path (so the shim parses queryStringParameters).
    assert call["guest_path"] == "/invoke?title=hi"
    # The caller's real method is forwarded out of band (event.httpMethod is always POST).
    assert call["extra_guest_headers"]["X-Forwarded-Method"] == "GET"
    assert call["extra_guest_headers"]["X-Forwarded-Path"] == ""
    assert call["read_timeout"] == 35.0


def test_subpath_forwarded_as_header(client, session, submitted):
    _add_function(session, "echo", visible=True)
    resp = client.post("/functions/echo/a/b/c", content=b"x")
    assert resp.status_code == 200
    call = submitted["calls"][0]
    assert call["extra_guest_headers"]["X-Forwarded-Method"] == "POST"
    assert call["extra_guest_headers"]["X-Forwarded-Path"] == "a/b/c"


def test_auth_headers_not_forwarded_to_guest(client, session, submitted):
    _add_function(session, "echo", visible=True)
    client.get(
        "/functions/echo",
        headers={"Authorization": "Bearer secret", "Cookie": "s=1"},
    )
    fwd = submitted["calls"][0]["extra_guest_headers"]
    assert "Authorization" not in fwd
    assert "Cookie" not in fwd


# --------------------------------------------------------------------------- #
# Binary body round-trip (both directions)
# --------------------------------------------------------------------------- #


def test_binary_body_round_trip(client, session, submitted):
    _add_function(session, "img", visible=True)
    png = b"\x89PNG\r\n\x1a\n\x00\x01\x02\x03\xff\xfe"
    submitted["response"] = httpx.Response(
        200, headers={"content-type": "image/png"}, content=png
    )

    resp = client.post(
        "/functions/img",
        content=b"\x00\x01\xff\xfeBINARY",
        headers={"content-type": "application/octet-stream"},
    )

    # Request body forwarded verbatim (content=), not re-encoded.
    assert submitted["calls"][0]["body"] == b"\x00\x01\xff\xfeBINARY"
    assert submitted["calls"][0]["extra_guest_headers"]["Content-Type"] == (
        "application/octet-stream"
    )
    # Response bytes and content-type relayed exactly.
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "image/png"
    assert resp.content == png


# --------------------------------------------------------------------------- #
# Response-class mapping
# --------------------------------------------------------------------------- #


def test_embervm_202_maps_to_504(client, session, submitted):
    _add_function(session, "slow", visible=True)
    submitted["response"] = httpx.Response(
        202,
        headers={"content-type": "application/json"},
        content=b'{"task_id":"t","state":"running"}',
    )
    resp = client.get("/functions/slow")
    assert resp.status_code == 504
    assert resp.json() == {"error": "function invocation timed out"}


def test_read_timeout_maps_to_504(client, session, submitted):
    _add_function(session, "echo", visible=True)
    submitted["raises"] = embervm_client.EmberVMTimeout("read timed out")
    resp = client.get("/functions/echo")
    assert resp.status_code == 504
    assert resp.json() == {"error": "function invocation timed out"}


def test_connect_error_maps_to_502(client, session, submitted):
    _add_function(session, "echo", visible=True)
    submitted["raises"] = embervm_client.EmberVMTransportError("refused")
    resp = client.get("/functions/echo")
    assert resp.status_code == 502
    assert resp.json() == {"error": "could not reach the function runtime"}


def test_denial_429_relayed_verbatim(client, session, submitted):
    _add_function(session, "echo", visible=True)
    submitted["response"] = httpx.Response(
        429,
        headers={"content-type": "application/json"},
        content=b'{"error":"quota exceeded","retryable":true}',
    )
    resp = client.get("/functions/echo")
    assert resp.status_code == 429
    assert resp.json() == {"error": "quota exceeded", "retryable": True}
