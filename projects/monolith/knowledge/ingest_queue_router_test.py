"""Tests for the ingest queue API endpoint."""

import pytest

from fastapi.testclient import TestClient
from sqlmodel import Session, SQLModel, create_engine

from knowledge.ingest_queue import IngestQueueItem


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
def client(session):
    import dataclasses
    import knowledge.module
    from framework import PRIVATE_PROFILE, build_app

    # Compose only the knowledge domain instead of the whole monolith: the
    # same framework wiring the production app gets, without depending on
    # the app composition root, which imports every other domain.
    app = build_app(
        dataclasses.replace(PRIVATE_PROFILE, otel_enabled=False),
        (knowledge.module.MODULE,),
    )
    from core.db import get_session

    app.dependency_overrides[get_session] = lambda: session
    yield TestClient(app)
    app.dependency_overrides.clear()


def test_queue_ingest_youtube(client, session):
    resp = client.post(
        "/api/knowledge/ingest",
        json={
            "url": "https://www.youtube.com/watch?v=abc123",
            "source_type": "youtube",
        },
    )
    assert resp.status_code == 201
    assert resp.json() == {"queued": True}

    items = session.query(IngestQueueItem).all()
    assert len(items) == 1
    assert items[0].url == "https://www.youtube.com/watch?v=abc123"
    assert items[0].source_type == "youtube"
    assert items[0].status == "pending"


def test_queue_ingest_webpage(client, session):
    resp = client.post(
        "/api/knowledge/ingest",
        json={"url": "https://example.com/blog/post", "source_type": "webpage"},
    )
    assert resp.status_code == 201


def test_queue_ingest_empty_url_rejected(client):
    resp = client.post(
        "/api/knowledge/ingest",
        json={"url": "", "source_type": "youtube"},
    )
    assert resp.status_code == 400


def test_queue_ingest_invalid_source_type(client):
    resp = client.post(
        "/api/knowledge/ingest",
        json={"url": "https://example.com", "source_type": "pdf"},
    )
    assert resp.status_code == 422
