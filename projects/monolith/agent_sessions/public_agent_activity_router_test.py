"""Unit tests for the public agent activity response and cache contract."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.exc import SQLAlchemyError

from agent_sessions.public_router import router
from core.db import get_session


class _Result:
    def __init__(self, *, one=None, rows=None):
        self._one = one
        self._rows = rows

    def one(self):
        return self._one

    def all(self):
        return self._rows


class _FakeSession:
    def __init__(self, now_row, daily_rows, local_daily_rows=None):
        self.now_row = now_row
        self.daily_rows = daily_rows
        self.local_daily_rows = local_daily_rows or []
        self.statements: list[str] = []

    def execute(self, statement):
        sql = str(statement)
        self.statements.append(sql)
        if "public_api.agent_activity_now" in sql:
            return _Result(one=self.now_row)
        if "public_api.agent_activity_daily" in sql:
            return _Result(rows=self.daily_rows)
        if "public_api.local_session_activity_daily" in sql:
            return _Result(rows=self.local_daily_rows)
        raise AssertionError(f"unexpected query: {sql}")


def _client(fake_session):
    app = FastAPI()
    app.include_router(router)

    def override_session():
        yield fake_session

    app.dependency_overrides[get_session] = override_session
    return TestClient(app, raise_server_exceptions=False)


def _daily_row(day_value, amount, *, cost=None, list_cost=None, model="luna"):
    return {
        "day": day_value,
        "model": model,
        "sessions": amount,
        "turns": amount * 2,
        "input_tokens": amount * 3,
        "output_tokens": amount * 4,
        "cache_read_tokens": amount * 5,
        "cost_usd": cost,
        "list_cost_usd": list_cost,
    }


def _local_row(day_value, amount, *, list_cost=None, model="gpt-5.6-luna"):
    return {
        "day": day_value,
        "model": model,
        "source": "codex-session",
        "sessions": amount,
        "input_tokens": amount * 7,
        "output_tokens": amount * 8,
        "cache_read_tokens": amount * 9,
        "list_cost_usd": list_cost,
    }


def test_activity_shape_windows_headers_and_stable_etag():
    today = datetime.now(timezone.utc).date()
    rows = [
        _daily_row(today - timedelta(days=30), 16, model="terra"),
        _daily_row(today - timedelta(days=7), 4, cost=4.0, model="sol"),
        _daily_row(today - timedelta(days=6), 2, cost=1.25, model="terra"),
        _daily_row(today, 1, list_cost=None, model="luna"),
        _daily_row(today - timedelta(days=29), 8, model="opus"),
    ]
    fake_session = _FakeSession(
        {
            "active_last_hour": 3,
            "sessions_today": 5,
            "running": 2,
            "last_turn_at": None,
        },
        rows,
        [
            _local_row(today, 3, list_cost=2.5),
            _local_row(today - timedelta(days=7), 9, list_cost=99),
        ],
    )

    with _client(fake_session) as client:
        first = client.get("/api/agents/public/activity")
        second = client.get("/api/agents/public/activity")

        assert first.status_code == 200
        payload = first.json()
        assert payload["now"] == {
            "active_last_hour": 3,
            "sessions_today": 5,
            "running": 2,
            "last_turn_at": None,
        }
        assert list(payload) == ["now", "daily", "local_daily", "totals_7d"]
        assert len(payload["daily"]) == 4
        assert set(payload["daily"][0]) == {
            "day",
            "model",
            "sessions",
            "turns",
            "input_tokens",
            "output_tokens",
            "cache_read_tokens",
            "cost_usd",
            "list_cost_usd",
        }
        assert [row["day"] for row in payload["daily"]] == [
            today.isoformat(),
            (today - timedelta(days=6)).isoformat(),
            (today - timedelta(days=7)).isoformat(),
            (today - timedelta(days=29)).isoformat(),
        ]
        assert payload["totals_7d"] == {
            "ember": {
                "sessions": 3,
                "turns": 6,
                "input_tokens": 9,
                "output_tokens": 12,
                "cache_read_tokens": 15,
                "cost_usd": 1.25,
                "list_cost_usd": None,
            },
            "local": {
                "sessions": 3,
                "turns": 0,
                "input_tokens": 21,
                "output_tokens": 24,
                "cache_read_tokens": 27,
                "cost_usd": None,
                "list_cost_usd": 2.5,
            },
        }
        assert payload["local_daily"][0]["source"] == "codex-session"
        assert first.headers["cache-control"] == ("public, max-age=300, s-maxage=300")
        assert first.headers["etag"] == second.headers["etag"]

        unchanged = client.get(
            "/api/agents/public/activity",
            headers={"If-None-Match": first.headers["etag"]},
        )
        assert unchanged.status_code == 304
        assert unchanged.headers["etag"] == first.headers["etag"]
        assert unchanged.headers["cache-control"] == first.headers["cache-control"]

    assert fake_session.statements
    assert all("public_api." in sql for sql in fake_session.statements)
    assert all("activity_" in sql for sql in fake_session.statements)
    assert all("agent_sessions.agent_" not in sql for sql in fake_session.statements)


def test_activity_serializes_last_turn_at_as_utc():
    fake_session = _FakeSession(
        {
            "active_last_hour": 0,
            "sessions_today": 0,
            "running": 0,
            "last_turn_at": datetime(2026, 9, 7, 12, 30),
        },
        [],
    )

    with _client(fake_session) as client:
        response = client.get("/api/agents/public/activity")

    assert response.status_code == 200
    assert response.json()["now"]["last_turn_at"] == "2026-09-07T12:30:00+00:00"


def test_activity_returns_500_when_views_are_unavailable():
    class _BrokenSession:
        def execute(self, statement):
            raise SQLAlchemyError("public activity view is unavailable")

    with _client(_BrokenSession()) as client:
        response = client.get("/api/agents/public/activity")

    assert response.status_code == 500
    assert response.json() == {"detail": "agent activity unavailable"}
