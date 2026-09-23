from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlmodel import Session, SQLModel, create_engine

from core.db import get_session
from observability import public_router
from observability.factory_goals import FactoryGoal
from observability.merged_prs import MergedPR

_NOW = datetime(2026, 9, 7, 12, tzinfo=timezone.utc)


@pytest.fixture(name="session")
def session_fixture(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'merged-pr-router.db'}")
    tables = [MergedPR.__table__, FactoryGoal.__table__]
    original_schemas = [table.schema for table in tables]
    for table in tables:
        table.schema = None
    try:
        SQLModel.metadata.create_all(engine, tables=tables)
        with Session(engine) as session:
            yield session
    finally:
        for table, schema in zip(tables, original_schemas):
            table.schema = schema
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
        "ci": 0,
        "build": 0,
        "perf": 0,
        "style": 0,
        "revert": 0,
        "wip": 0,
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
    assert body["snapshotted_at"] == "2026-09-07T12:00:00Z"
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


def test_public_merges_has_null_snapshot_when_there_are_no_rows(client):
    response = client.get("/api/agents/public/merges")

    assert response.status_code == 200
    assert response.json()["snapshotted_at"] is None


def test_public_merges_counts_unknown_types_without_failing(client, session):
    session.add(_row(123, timedelta(hours=1), type="future-type"))
    session.commit()

    response = client.get("/api/agents/public/merges")

    assert response.status_code == 200
    assert response.json()["daily"][-1]["future-type"] == 1


def test_public_merges_etag_changes_when_seven_day_cutoff_hour_changes(
    client, monkeypatch
):
    first = client.get("/api/agents/public/merges")
    monkeypatch.setattr(public_router, "_now_utc", lambda: _NOW + timedelta(hours=1))

    second = client.get("/api/agents/public/merges")

    assert second.headers["ETag"] != first.headers["ETag"]


def test_public_factory_goals_scores_declared_goals(client, session):
    session.add(
        FactoryGoal(
            statement="Land declared goals",
            issue_numbers=[5927],
            declared_by="opus",
            declared_at=_NOW - timedelta(days=1),
            active=True,
        )
    )
    session.add(_row(123, timedelta(hours=1), title="feat(factory): goals for #5927"))
    session.commit()

    response = client.get("/api/agents/public/factory/goals")

    assert response.status_code == 200
    body = response.json()
    assert body["declared_at"] == "2026-09-06T12:00:00Z"
    assert body["stale"] is False
    assert len(body["goals"]) == 1
    goal = body["goals"][0]
    assert goal["statement"] == "Land declared goals"
    assert goal["issue_numbers"] == [5927]
    assert goal["merged_refs"] == 1
    assert goal["last_activity"] == "2026-09-07T11:00:00Z"
    assert goal["stale"] is False
    assert "public" in response.headers["Cache-Control"]
    assert response.headers["ETag"]


def test_public_factory_goals_counts_refs_older_than_seven_days(client, session):
    # Regression for a goal declared beyond the 7-day merge window this
    # endpoint used to load: merges referencing it at 8, 11, and 20 days old
    # must still count even though the goal itself (declared 12 days ago) is
    # not yet stale (STALE_AFTER_DAYS is 14).
    session.add(
        FactoryGoal(
            statement="Land declared goals",
            issue_numbers=[5927],
            declared_by="opus",
            declared_at=_NOW - timedelta(days=12),
            active=True,
        )
    )
    session.add(_row(123, timedelta(days=8), title="feat(factory): goals for #5927"))
    session.add(_row(122, timedelta(days=11), title="fix(factory): tuneup #5927"))
    session.add(_row(121, timedelta(days=20), title="docs(factory): notes #5927"))
    session.commit()

    response = client.get("/api/agents/public/factory/goals")

    assert response.status_code == 200
    body = response.json()
    assert body["stale"] is False
    goal = body["goals"][0]
    assert goal["stale"] is False
    assert goal["merged_refs"] == 3
    assert goal["last_activity"] == "2026-08-30T12:00:00Z"


def test_public_factory_goals_is_stale_with_no_declarations(client):
    response = client.get("/api/agents/public/factory/goals")

    assert response.status_code == 200
    assert response.json()["goals"] == []
    assert response.json()["stale"] is True


def test_public_factory_goals_supports_conditional_get(client):
    first = client.get("/api/agents/public/factory/goals")
    second = client.get(
        "/api/agents/public/factory/goals",
        headers={"If-None-Match": first.headers["ETag"]},
    )

    assert second.status_code == 304
    assert second.headers["ETag"] == first.headers["ETag"]
