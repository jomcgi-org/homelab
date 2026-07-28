"""Tests for the PUBLIC faas invocation router (Task 13).

The public router (``invoke_router_public``) differs from the private one in
exactly one way: it resolves via ``get_public_function`` (smoke-passed AND
``visibility=public``). The load-bearing property under test is the SECURITY one:
a private function, even a smoke-passed one, must 404 on the public origin and
never reach EmberVM. The marshaling/relay is shared with the private router
(``relay_to_function``) and covered by ``invoke_router_test.py``; here we only
assert the visibility gate and that a public function still serves.

``embervm_client.submit`` is patched on ``faas.invoke_router`` (where
``relay_to_function`` lives, so the public router's relay goes through it) to a
synthetic response. A fresh FastAPI app mounts only the public router, mirroring
what ``register_public`` does on the public tier.
"""

from __future__ import annotations

import httpx
import pytest

from datetime import datetime, timezone

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlmodel import Session, SQLModel, create_engine

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


def _add_function(
    session: Session, name: str, *, visibility: str, smoked: bool
) -> None:
    now = datetime.now(timezone.utc)
    session.add(
        Function(
            name=name,
            visibility=visibility,
            runtime="python312",
            handler="app.handle",
            zip_sha256="deadbeef",
            code_uri=f"http://s3/faas/{name}/deadbeef.zip",
            created_by="api",
            created_at=now,
            updated_at=now,
            last_smoke_at=now if smoked else None,
        )
    )
    session.commit()


@pytest.fixture
def submitted(monkeypatch):
    """Patch ``submit`` on faas.invoke_router (where relay_to_function lives)."""
    from faas import invoke_router as mod

    state: dict = {
        "calls": [],
        "response": httpx.Response(
            200, headers={"content-type": "image/png"}, content=b"\x89PNG..."
        ),
    }

    async def _submit(name, *, body, guest_path, extra_guest_headers, read_timeout):
        state["calls"].append({"name": name, "guest_path": guest_path})
        return state["response"]

    monkeypatch.setattr(mod.embervm_client, "submit", _submit)
    return state


@pytest.fixture
def client(session):
    from core.db import get_session
    from faas.invoke_router_public import router

    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_session] = lambda: session
    yield TestClient(app)
    app.dependency_overrides.clear()


def test_public_function_serves(client, session, submitted):
    _add_function(session, "og-image", visibility="public", smoked=True)
    resp = client.get("/functions/og-image?title=hi")
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "image/png"
    assert submitted["calls"][0]["name"] == "og-image"
    assert submitted["calls"][0]["guest_path"] == "/invoke?title=hi"


def test_private_function_is_404_on_public_and_never_submits(
    client, session, submitted
):
    # SECURITY: a smoke-passed PRIVATE function must not be invokable publicly.
    _add_function(session, "secret", visibility="private", smoked=True)
    resp = client.get("/functions/secret")
    assert resp.status_code == 404
    assert resp.json() == {"error": "function not found"}
    assert submitted["calls"] == []  # never reached EmberVM


def test_unsmoked_public_function_is_404(client, session, submitted):
    # A public function that has not passed its smoke gate is not yet visible.
    _add_function(session, "pending", visibility="public", smoked=False)
    resp = client.get("/functions/pending")
    assert resp.status_code == 404
    assert submitted["calls"] == []


def test_unknown_function_is_404(client, session, submitted):
    resp = client.get("/functions/nope")
    assert resp.status_code == 404
    assert submitted["calls"] == []
