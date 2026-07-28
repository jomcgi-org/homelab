"""Unit tests for the private trips ingestion endpoint.

FastAPI TestClient over an in-memory SQLite session (SQLModel.create_all, no
migrations), mirroring trips/models_test.py's schema-stripping fixture. The S3
put is monkeypatched so no real SeaweedFS is touched. The endpoint has no
app-level auth: it is protected by the Cloudflare Access policy at the gateway.
"""

from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlmodel import Session, SQLModel, create_engine, select
from sqlmodel.pool import StaticPool

from core.db import get_session
from trips import s3
from trips.ingest_router import router as ingest_router
from trips.models import TripPoint

_TESTDATA = Path(__file__).resolve().parent / "testdata"


@pytest.fixture(name="session")
def session_fixture():
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


@pytest.fixture(name="put_calls")
def put_calls_fixture(monkeypatch):
    """Record put_image calls instead of hitting SeaweedFS."""
    calls = []

    def _fake_put(image_key, data, content_type):
        calls.append((image_key, data, content_type))

    monkeypatch.setattr(s3, "put_image", _fake_put)
    return calls


@pytest.fixture(name="client")
def client_fixture(session, put_calls):
    app = FastAPI()
    app.include_router(ingest_router)
    app.dependency_overrides[get_session] = lambda: session
    with TestClient(app) as client:
        yield client


def _post_image(client, filename="geotagged.jpg", trip="2025-liard-hot-springs"):
    image_bytes = (_TESTDATA / filename).read_bytes()
    return client.post(
        "/api/trips/ingest",
        params={"trip": trip},
        files={"image": (filename, image_bytes, "image/jpeg")},
    )


def test_post_image_writes_point_and_uploads(client, session, put_calls):
    resp = _post_image(client)
    assert resp.status_code == 201, resp.text
    body = resp.json()
    point_id = body["id"]
    assert body["image"] == f"img_{point_id}.jpg"

    row = session.get(TripPoint, ("2025-liard-hot-springs", point_id))
    assert row is not None
    assert row.image == f"img_{point_id}.jpg"

    # The S3 put was called with the content-addressed image key.
    assert len(put_calls) == 1
    assert put_calls[0][0] == f"img_{point_id}.jpg"


def test_reingest_same_image_is_idempotent(client, session):
    first = _post_image(client)
    assert first.status_code == 201, first.text
    second = _post_image(client)
    assert second.status_code == 201, second.text
    first_body = first.json()
    second_body = second.json()
    assert first_body["id"] == second_body["id"]

    rows = session.exec(select(TripPoint)).all()
    assert len(rows) == 1


def test_post_image_without_gps_is_422(client):
    resp = _post_image(client, filename="no_gps.jpg")
    assert resp.status_code == 422


def test_post_corrupt_image_is_422(client, session, put_calls):
    # A non-image error body must be rejected with 422 before any S3/DB write.
    resp = client.post(
        "/api/trips/ingest",
        params={"trip": "2025-liard-hot-springs"},
        files={"image": ("bad.jpg", b"operation Lookup failed " * 64, "image/jpeg")},
    )
    assert resp.status_code == 422, resp.text
    assert "not a valid image" in resp.json().get("detail", "")
    # No object stored and no point row written for a rejected upload.
    assert put_calls == []
    assert session.exec(select(TripPoint)).all() == []


def test_post_tiny_image_is_422(client, put_calls):
    resp = client.post(
        "/api/trips/ingest",
        params={"trip": "2025-liard-hot-springs"},
        files={"image": ("tiny.jpg", b"tinybytes!", "image/jpeg")},
    )
    assert resp.status_code == 422, resp.text
    assert "too small" in resp.json().get("detail", "")
    assert put_calls == []
