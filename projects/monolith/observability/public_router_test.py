from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlmodel import Session, SQLModel, create_engine

from core.db import get_session
from observability import public_router
from observability.merged_prs import MergedPR

_NOW = datetime(2026, 9, 7, 12, tzinfo=timezone.utc)


@pytest.fixture(name="session")
def session_fixture(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'merged-pr-router.db'}")
    table = MergedPR.__table__
    original_schema = table.schema
    table.schema = None
    try:
        SQLModel.metadata.create_all(engine, tables=[table])
        with Session(engine) as session:
            yield session
    finally:
        table.schema = original_schema
        engine.dispose()


@pytest.fixture(name="client")
def client_fixture(session, monkeypatch):
    monkeypatch.setattr(public_router, "_now_utc", lambda: _NOW)
    app = FastAPI()
    app.include_router(public_router.router)
    app.dependency_overrides[get_session] = lambda: session
    yield TestClient(app)
    app.dependency_overrides.clear()


def _row(number: int, age: timedelta, **overrides) -> MergedPR:
    values = {
        "number": number,
        "title": "feat(monolith): snapshot merges",
        "merged_at": _NOW - age,
        "additions": 45,
        "deletions": 12,
        "changed_files": 3,
        "type": "feat",
        "scope": "monolith",
        "agent_authored": True,
        "snapshotted_at": _NOW,
    }
    values.update(overrides)
    return MergedPR(**values)


def test_public_merges_returns_daily_week_and_totals(client, session):
    session.add(_row(123, timedelta(hours=1)))
    session.add(
        _row(
            122,
            timedelta(days=8),
            title="fix: older fix",
            type="fix",
            scope=None,
            agent_authored=False,
            additions=5,
            deletions=2,
        )
    )
    session.add(_row(121, timedelta(days=40)))
    session.commit()

    response = client.get("/api/agents/public/merges")

    assert response.status_code == 200
    body = response.json()
    assert len(body["daily"]) == 30
    assert body["daily"][-1] == {
        "d": "2026-09-07",
        "feat": 1,
        "fix": 0,
        "docs": 0,
        "chore": 0,
        "test": 0,
        "refactor": 0,
        "other": 0,
    }
    assert [row["number"] for row in body["week"]] == [123]
    assert body["week"][0]["merged_at"] == "2026-09-07T11:00:00Z"
    assert body["totals"] == {
        "n_7d": 1,
        "agent_7d": 1,
        "add_7d": 45,
        "del_7d": 12,
        "n_30d": 2,
    }
    assert "public" in response.headers["Cache-Control"]
    assert response.headers["ETag"]


def test_public_merges_supports_conditional_get(client):
    first = client.get("/api/agents/public/merges")
    second = client.get(
        "/api/agents/public/merges",
        headers={"If-None-Match": first.headers["ETag"]},
    )

    assert second.status_code == 304
    assert second.headers["ETag"] == first.headers["ETag"]
