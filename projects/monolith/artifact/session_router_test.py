"""Tests for the artifact session HTTP endpoints (ADR 026 Phase 2).

Mirrors artifact/router_test.py: S3 is faked via monkeypatch so the tests
exercise the real handler logic without a live SeaweedFS endpoint.

All session endpoints live on write_router (full monolith only). The fixture
mounts only write_router so these tests also confirm the endpoints are reachable
on write_router. A separate test verifies read_router does NOT expose them.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from artifact import s3
from artifact.router import read_router, write_router


@pytest.fixture
def client(monkeypatch) -> TestClient:
    """TestClient with a faked in-memory session store."""
    store: dict[str, bytes] = {}

    def fake_put(artifact_id: str, db: bytes) -> str:
        store[artifact_id] = db
        return f"etag-{len(db)}"

    def fake_get(artifact_id: str) -> bytes | None:
        return store.get(artifact_id)

    def fake_head(artifact_id: str) -> str | None:
        if artifact_id not in store:
            return None
        return f"etag-{len(store[artifact_id])}"

    monkeypatch.setattr(s3, "put_session", fake_put)
    monkeypatch.setattr(s3, "get_session", fake_get)
    monkeypatch.setattr(s3, "head_session", fake_head)

    app = FastAPI()
    app.include_router(write_router)
    return TestClient(app)


@pytest.fixture
def read_only_client(monkeypatch) -> TestClient:
    """TestClient with ONLY read_router mounted (mirrors the public binary)."""
    app = FastAPI()
    app.include_router(read_router)
    return TestClient(app)


# ---------------------------------------------------------------------------
# POST /internal/artifact/{id}/session
# ---------------------------------------------------------------------------


def test_put_session_stores_db_and_returns_version(client: TestClient):
    db = b"SQLite format 3\x00" + b"\x00" * 100
    resp = client.post(
        "/internal/artifact/demo/session",
        content=db,
        headers={"Content-Type": "application/octet-stream"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["id"] == "demo"
    assert body["version"]


def test_put_session_returns_updated_version_on_overwrite(client: TestClient):
    db1 = b"v1" * 10
    db2 = b"v2" * 20
    r1 = client.post(
        "/internal/artifact/demo/session",
        content=db1,
        headers={"Content-Type": "application/octet-stream"},
    )
    r2 = client.post(
        "/internal/artifact/demo/session",
        content=db2,
        headers={"Content-Type": "application/octet-stream"},
    )
    assert r1.status_code == 200
    assert r2.status_code == 200
    # etag encodes len in our fake, so they should differ
    assert r1.json().get("version") != r2.json().get("version")


def test_put_session_rejects_empty_body(client: TestClient):
    resp = client.post(
        "/internal/artifact/demo/session",
        content=b"",
        headers={"Content-Type": "application/octet-stream"},
    )
    assert resp.status_code == 422


def test_put_session_rejects_oversized_body(client: TestClient):
    big = b"x" * (32 * 1024 * 1024 + 1)
    resp = client.post(
        "/internal/artifact/demo/session",
        content=big,
        headers={"Content-Type": "application/octet-stream"},
    )
    assert resp.status_code == 413


def test_put_session_rejects_invalid_id(client: TestClient):
    resp = client.post(
        "/internal/artifact/../etc/session",
        content=b"x",
        headers={"Content-Type": "application/octet-stream"},
    )
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# GET /internal/artifact/{id}/session
# ---------------------------------------------------------------------------


def test_get_session_returns_raw_bytes(client: TestClient):
    db = b"SQLite format 3\x00raw"
    client.post(
        "/internal/artifact/demo/session",
        content=db,
        headers={"Content-Type": "application/octet-stream"},
    )
    resp = client.get("/internal/artifact/demo/session")
    assert resp.status_code == 200
    assert resp.content == db
    assert resp.headers["content-type"] == "application/octet-stream"
    assert resp.headers["cache-control"] == "no-store"


def test_get_session_missing_is_404(client: TestClient):
    assert client.get("/internal/artifact/nope/session").status_code == 404


def test_get_session_invalid_id_is_404(client: TestClient):
    assert client.get("/internal/artifact/..%2f..%2fx/session").status_code == 404


# ---------------------------------------------------------------------------
# GET /internal/artifact/{id}/session/exists
# ---------------------------------------------------------------------------


def test_session_exists_false_when_absent(client: TestClient):
    resp = client.get("/internal/artifact/nope/session/exists")
    assert resp.status_code == 200
    assert resp.json() == {"exists": False}
    assert resp.headers["cache-control"] == "no-store"


def test_session_exists_true_after_put(client: TestClient):
    db = b"SQLite format 3\x00"
    client.post(
        "/internal/artifact/demo/session",
        content=db,
        headers={"Content-Type": "application/octet-stream"},
    )
    resp = client.get("/internal/artifact/demo/session/exists")
    assert resp.status_code == 200
    assert resp.json() == {"exists": True}


def test_session_exists_invalid_id_is_404(client: TestClient):
    assert (
        client.get("/internal/artifact/..%2f..%2fx/session/exists").status_code == 404
    )


# ---------------------------------------------------------------------------
# Security: session endpoints must NOT be on the public read_router
# ---------------------------------------------------------------------------


def test_session_endpoints_absent_from_read_router(read_only_client: TestClient):
    """Session routes are write_router-only; the public read_router has none."""
    # POST
    resp = read_only_client.post(
        "/internal/artifact/demo/session",
        content=b"x",
        headers={"Content-Type": "application/octet-stream"},
    )
    assert resp.status_code == 405, "POST /session must not be present on read_router"
    # GET
    assert read_only_client.get("/internal/artifact/demo/session").status_code == 405, (
        "GET /session must not be present on read_router"
    )
    # exists
    assert (
        read_only_client.get("/internal/artifact/demo/session/exists").status_code
        == 405
    ), "GET /session/exists must not be present on read_router"
