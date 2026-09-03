"""Tests for POST /api/knowledge/raws."""

import dataclasses
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, SQLModel, create_engine, select

import knowledge.module
from core.db import get_session
from framework import PRIVATE_PROFILE, build_app
from knowledge.models import RawInput


@pytest.fixture(name="session")
def session_fixture(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'raws.db'}")
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
    app = build_app(
        dataclasses.replace(PRIVATE_PROFILE, otel_enabled=False),
        (knowledge.module.MODULE,),
    )
    app.dependency_overrides[get_session] = lambda: session
    with patch("knowledge.ingest_queue.upload_raw"):
        yield TestClient(app, raise_server_exceptions=False)
    app.dependency_overrides.clear()


def test_create_raw_returns_created_then_deduplicated(client):
    payload = {"content": "raw evidence", "source": "test-source"}

    first = client.post("/api/knowledge/raws", json=payload)
    second = client.post("/api/knowledge/raws", json=payload)

    assert first.status_code == 201
    assert first.json()["created"] is True
    assert second.status_code == 201
    assert second.json() == {"raw_id": first.json()["raw_id"], "created": False}


def test_create_raw_rejects_content_over_two_mib(client):
    response = client.post(
        "/api/knowledge/raws",
        json={"content": "x" * (2 * 1024 * 1024 + 1), "source": "test-source"},
    )

    assert response.status_code == 413
    assert response.json() == {"error": "content exceeds the 2 MiB limit"}


def test_create_raw_rejects_bad_source(client):
    response = client.post(
        "/api/knowledge/raws",
        json={"content": "evidence", "source": "Bad source!"},
    )

    assert response.status_code == 422


def test_create_raw_persists_extra(client, session):
    response = client.post(
        "/api/knowledge/raws",
        json={
            "content": "evidence with context",
            "source": "test-source",
            "original_url": "https://example.com/evidence",
            "extra": {"collector": "unit-test", "sequence": 3},
        },
    )

    assert response.status_code == 201
    raw = session.exec(
        select(RawInput).where(RawInput.raw_id == response.json()["raw_id"])
    ).one()
    assert raw.extra == {"collector": "unit-test", "sequence": 3}
    assert raw.original_path == "https://example.com/evidence"
