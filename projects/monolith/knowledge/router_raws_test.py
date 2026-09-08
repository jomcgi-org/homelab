"""Tests for POST /api/knowledge/raws."""

import dataclasses
import json
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
    content = "é" * (1024 * 1024 + 1)
    assert len(content) < 2 * 1024 * 1024
    assert len(content.encode("utf-8")) > 2 * 1024 * 1024
    response = client.post(
        "/api/knowledge/raws",
        json={"content": content, "source": "test-source"},
    )

    assert response.status_code == 413
    assert response.json() == {"detail": "content exceeds the 2 MiB limit"}


def test_create_raw_rejects_extra_over_64_kib(client):
    response = client.post(
        "/api/knowledge/raws",
        json={
            "content": "evidence",
            "source": "test-source",
            "extra": {"context": "x" * (64 * 1024)},
        },
    )

    assert response.status_code == 422


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


def test_create_raw_prices_local_session_usage(client, session):
    response = client.post(
        "/api/knowledge/raws",
        json={
            "content": "local session transcript",
            "source": "local-session",
            "extra": {
                "model": "luna",
                "usage": {"input_tokens": 1_000_000, "output_tokens": 0},
            },
        },
    )

    assert response.status_code == 201
    raw = session.exec(
        select(RawInput).where(RawInput.raw_id == response.json()["raw_id"])
    ).one()
    assert raw.extra["usage_cost_usd"] == pytest.approx(0.40)
    assert raw.extra["usage_cost_source"] == "list"


def test_create_raw_rechecks_extra_limit_after_pricing(client, session):
    extra = {
        "model": "luna",
        "usage": {"input_tokens": 1},
        "padding": "",
    }
    encoded = json.dumps(extra, ensure_ascii=False, separators=(",", ":")).encode(
        "utf-8"
    )
    extra["padding"] = "x" * (64 * 1024 - len(encoded))

    response = client.post(
        "/api/knowledge/raws",
        json={
            "content": "local session transcript",
            "source": "local-session",
            "extra": extra,
        },
    )

    assert response.status_code == 413
    assert session.exec(select(RawInput)).all() == []


def test_backfill_raw_usage_prices_and_preserves_existing_metadata(client, session):
    created = client.post(
        "/api/knowledge/raws",
        json={
            "content": "old local session transcript",
            "source": "claude-session",
            "extra": {"model": "claude-opus-5", "collector_version": "claude-v1"},
        },
    ).json()

    response = client.post(
        f"/api/knowledge/raws/{created['raw_id']}/usage",
        json={
            "usage": {
                "shape": "claude",
                "input_tokens": 1_000,
                "output_tokens": 100,
                "cache_read_tokens": 9_000,
                "cache_write_tokens": 0,
            },
            "models": ["claude-opus-5"],
            "model": "replacement-model",
        },
    )

    assert response.status_code == 200
    assert response.json()["updated"] is True
    raw = session.exec(
        select(RawInput).where(RawInput.raw_id == created["raw_id"])
    ).one()
    assert raw.extra["model"] == "claude-opus-5"
    assert raw.extra["models"] == ["claude-opus-5"]
    assert raw.extra["usage_cost_usd"] > 0
    assert raw.extra["usage_cost_source"] == "list"

    repeated = client.post(
        f"/api/knowledge/raws/{created['raw_id']}/usage",
        json={
            "usage": {"shape": "codex", "input_tokens": 999_999},
            "models": ["different"],
            "model": "different",
        },
    )
    assert repeated.status_code == 200
    assert repeated.json()["updated"] is False
    session.refresh(raw)
    assert raw.extra["usage"]["shape"] == "claude"


def test_backfill_raw_usage_validation_and_not_found(client):
    missing = client.post(
        "/api/knowledge/raws/missing/usage",
        json={"usage": {}, "models": []},
    )
    oversized = client.post(
        "/api/knowledge/raws/missing/usage",
        json={"usage": {"padding": "x" * (64 * 1024)}, "models": []},
    )

    assert missing.status_code == 404
    assert oversized.status_code == 422


def test_backfill_raw_usage_never_fails_on_pricing_error(client, session):
    created = client.post(
        "/api/knowledge/raws",
        json={"content": "pricing failure", "source": "codex-session"},
    ).json()

    with patch("knowledge.router.price_usage", side_effect=RuntimeError("price down")):
        response = client.post(
            f"/api/knowledge/raws/{created['raw_id']}/usage",
            json={
                "usage": {"shape": "codex", "input_tokens": 100},
                "models": ["gpt-5.6-luna"],
                "model": "gpt-5.6-luna",
            },
        )

    assert response.status_code == 200
    raw = session.exec(
        select(RawInput).where(RawInput.raw_id == created["raw_id"])
    ).one()
    assert raw.extra["usage"]["input_tokens"] == 100
    assert "usage_cost_usd" not in raw.extra


def test_create_extractable_raw_redacts_before_storage(client, session):
    token = "ghp_abcdefghijklmnopqrstuvwxyz123456"
    with patch("knowledge.ingest_queue.upload_raw") as upload:
        response = client.post(
            "/api/knowledge/raws",
            json={"content": f"reported token {token}", "source": "agent-report"},
        )

    assert response.status_code == 201
    stored_body = upload.call_args.args[1]
    assert token not in stored_body
    assert "[REDACTED:github_token]" in stored_body
    raw = session.exec(
        select(RawInput).where(RawInput.raw_id == response.json()["raw_id"])
    ).one()
    assert raw.extra["server_redactions"] == {"github_token": 1}


def test_create_capture_raw_does_not_redact(client, session):
    token = "ghp_abcdefghijklmnopqrstuvwxyz123456"
    with patch("knowledge.ingest_queue.upload_raw") as upload:
        response = client.post(
            "/api/knowledge/raws",
            json={"content": f"captured token {token}", "source": "capture"},
        )

    assert response.status_code == 201
    assert upload.call_args.args[1] == f"captured token {token}"
    raw = session.exec(
        select(RawInput).where(RawInput.raw_id == response.json()["raw_id"])
    ).one()
    assert "server_redactions" not in raw.extra
