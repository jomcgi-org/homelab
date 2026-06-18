"""Phase 2 unit tests for the public-chat backend (ADR 005).

Covers the admission + abuse controls added in Phase 2:

1. Turnstile siteverify failure rejects session creation with 403
   ``turnstile_failed`` (real verify path: a non-empty TURNSTILE_SECRET_KEY plus
   a mocked Cloudflare response of ``{"success": false}``).
2. The global circuit breaker sheds a turn with the 200 ``busy`` SSE event when
   the in-flight ceiling is occupied (and persists nothing).
3. The forwarded client IP is stored only as a salted sha256 ``ip_hash``, never
   the raw IP.

Fast in-memory SQLite + TestClient, mirroring router_test.py (the chat_public
models live on the Postgres-only ``chat_public`` schema, which SQLite cannot
span, so the schema= overrides are stripped for the fixture).
"""

from __future__ import annotations

import hashlib
import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlmodel import Session, SQLModel, create_engine, select
from sqlmodel.pool import StaticPool

from chat_public import limits, sessions, turnstile
from chat_public.db import get_chat_session
from chat_public.models import ChatMessage, ChatSession
from chat_public.router import router


@pytest.fixture(name="session")
def session_fixture():
    """In-memory SQLite session (schema-stripped for SQLite compat)."""
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
        with Session(engine) as session:
            yield session
    finally:
        for table in SQLModel.metadata.tables.values():
            if table.name in original_schemas:
                table.schema = original_schemas[table.name]


@pytest.fixture(name="client")
def client_fixture(session):
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_chat_session] = lambda: session
    yield TestClient(app, raise_server_exceptions=False)
    app.dependency_overrides.clear()


def _parse_sse(text: str) -> list[dict]:
    frames = []
    for line in text.splitlines():
        if line.startswith("data: "):
            frames.append(json.loads(line[len("data: ") :]))
    return frames


class _FakeResponse:
    """A minimal stand-in for an httpx.Response from Cloudflare siteverify."""

    def __init__(self, payload: dict) -> None:
        self._payload = payload

    def raise_for_status(self) -> None:  # 2xx, nothing to raise
        return None

    def json(self) -> dict:
        return self._payload


class _FakeAsyncClient:
    """Async-context-manager stand-in for httpx.AsyncClient.

    Returns a fixed siteverify payload from ``post`` so the verify path runs
    without any network IO (the egress TLSRoute is not exercised in unit tests).
    """

    payload: dict = {"success": False, "error-codes": ["invalid-input-response"]}

    def __init__(self, *args, **kwargs) -> None:
        pass

    async def __aenter__(self) -> "_FakeAsyncClient":
        return self

    async def __aexit__(self, *args) -> bool:
        return False

    async def post(self, url, data=None) -> _FakeResponse:
        return _FakeResponse(self.payload)


# ---------------------------------------------------------------------------
# 1. Turnstile siteverify failure -> 403 turnstile_failed
# ---------------------------------------------------------------------------


def test_siteverify_failure_rejects_session_with_403(client, session, monkeypatch):
    # Force the real verify path: a non-empty secret skips the dev stub-accept.
    monkeypatch.setattr(turnstile, "SECRET_KEY", "test-secret-key")
    # Cloudflare returns success:false -> the challenge failed.
    monkeypatch.setattr(turnstile.httpx, "AsyncClient", _FakeAsyncClient)

    resp = client.post(
        "/internal/chat/session",
        json={"turnstile_token": "a-token-cloudflare-will-reject"},
        headers={"CF-Connecting-IP": "203.0.113.7"},
    )

    assert resp.status_code == 403
    body = resp.json()
    assert body["detail"]["code"] == "turnstile_failed"
    # No session row is opened on a failed challenge.
    assert session.exec(select(ChatSession)).all() == []


# ---------------------------------------------------------------------------
# 2. Global circuit breaker sheds with the "busy" SSE event
# ---------------------------------------------------------------------------


def test_global_ceiling_sheds_with_busy_sse_event(client, session, monkeypatch):
    # Occupy the entire ceiling: a breaker sized 0 sheds every turn.
    monkeypatch.setattr(limits, "_breaker", limits._CircuitBreaker(0))

    row = sessions.create_session(session)
    resp = client.post(
        "/internal/chat/message",
        json={"session_id": row.id, "message": "hello"},
    )

    # Shed as a 200 SSE "busy" event (so it relays through the SSR passthrough),
    # not a 4xx/5xx status.
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/event-stream")
    frames = _parse_sse(resp.text)
    busy = next(f for f in frames if f["type"] == "busy")
    assert busy["data"]["code"] == "busy"

    # A shed turn is free: nothing persisted, counters untouched.
    assert session.exec(select(ChatMessage)).all() == []
    session.refresh(row)
    assert row.turn_count == 0
    assert row.total_tokens == 0


# ---------------------------------------------------------------------------
# 3. ip_hash is stored salted + hashed, never the raw IP
# ---------------------------------------------------------------------------


def test_ip_hash_stored_salted_not_raw(session, monkeypatch):
    monkeypatch.setattr(sessions, "IP_HASH_SALT", "test-salt")
    raw_ip = "203.0.113.7"

    row = sessions.create_session(session, ip=raw_ip)

    assert row.ip_hash is not None
    # Never the raw IP, and not the unsalted digest either.
    assert row.ip_hash != raw_ip
    assert row.ip_hash != hashlib.sha256(raw_ip.encode("utf-8")).hexdigest()
    # It is exactly the salted sha256 hex digest (64 lowercase hex chars).
    expected = hashlib.sha256(("test-salt" + raw_ip).encode("utf-8")).hexdigest()
    assert row.ip_hash == expected
    assert len(row.ip_hash) == 64
    assert all(c in "0123456789abcdef" for c in row.ip_hash)
