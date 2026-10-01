"""Unit tests for the public agent activity response and cache contract.

The route serves the one public_api.agent_activity_snapshot row as written;
the aggregation and its windows are tested where they are built, in
factory/publication_test.py.
"""

from __future__ import annotations

import json

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.exc import SQLAlchemyError

from factory.public_view import router
from core.db import get_session


class _Result:
    def __init__(self, row):
        self._row = row

    def first(self):
        return self._row


class _FakeSession:
    def __init__(self, row):
        self.row = row
        self.statements: list[str] = []

    def execute(self, statement):
        sql = str(statement)
        self.statements.append(sql)
        if "public_api.agent_activity_snapshot" in sql:
            return _Result(self.row)
        raise AssertionError(f"unexpected query: {sql}")


def _client(fake_session):
    app = FastAPI()
    app.include_router(router)

    def override_session():
        yield fake_session

    app.dependency_overrides[get_session] = override_session
    return TestClient(app, raise_server_exceptions=False)


_PAYLOAD = {
    "snapshotted_at": "2026-10-01T12:00:00+00:00",
    "cost_basis": "list",
    "now": {
        "active_last_hour": 3,
        "sessions_today": 5,
        "running": 2,
        "last_turn_at": None,
    },
    "daily": [],
    "local_daily": [],
    "spend_daily": [{"day": "2026-10-01", "spend_usd": 1.5}],
    "totals_7d": {"combined": {"spend_usd": 1.5}},
    "totals_30d": {"combined": {"spend_usd": 4.0}},
}


def test_activity_serves_the_snapshot_row_with_cache_headers_and_stable_etag():
    fake_session = _FakeSession(
        {"payload": dict(_PAYLOAD), "snapshotted_at": "2026-10-01T12:00:00Z"}
    )

    with _client(fake_session) as client:
        first = client.get("/api/agents/public/activity")
        second = client.get("/api/agents/public/activity")

        assert first.status_code == 200
        assert first.json() == _PAYLOAD
        assert first.headers["cache-control"] == "public, max-age=300, s-maxage=300"
        assert first.headers["etag"] == second.headers["etag"]

        unchanged = client.get(
            "/api/agents/public/activity",
            headers={"If-None-Match": first.headers["etag"]},
        )
        assert unchanged.status_code == 304
        assert unchanged.headers["etag"] == first.headers["etag"]
        assert unchanged.headers["cache-control"] == first.headers["cache-control"]

        fake_session.row["payload"]["totals_7d"] = {"combined": {"spend_usd": 2.0}}
        changed = client.get(
            "/api/agents/public/activity",
            headers={"If-None-Match": first.headers["etag"]},
        )
        assert changed.status_code == 200
        assert changed.headers["etag"] != first.headers["etag"]

    # One single-row read of the public snapshot, never the private tables.
    assert fake_session.statements
    assert all(
        "public_api.agent_activity_snapshot" in sql for sql in fake_session.statements
    )
    assert all("agent_sessions." not in sql for sql in fake_session.statements)


def test_activity_decodes_a_text_payload():
    fake_session = _FakeSession(
        {"payload": json.dumps(_PAYLOAD), "snapshotted_at": None}
    )
    with _client(fake_session) as client:
        response = client.get("/api/agents/public/activity")
    assert response.status_code == 200
    assert response.json()["cost_basis"] == "list"


def test_activity_returns_503_before_the_first_snapshot():
    with _client(_FakeSession(None)) as client:
        response = client.get("/api/agents/public/activity")

    assert response.status_code == 503
    assert response.json() == {"detail": "agent activity not snapshotted"}


def test_activity_returns_500_when_the_snapshot_is_unavailable():
    class _BrokenSession:
        def execute(self, statement):
            raise SQLAlchemyError("public activity snapshot is unavailable")

    with _client(_BrokenSession()) as client:
        response = client.get("/api/agents/public/activity")

    assert response.status_code == 500
    assert response.json() == {"detail": "agent activity unavailable"}
