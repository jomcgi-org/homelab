"""Unit tests for the public factory routes, their headers and their 404s.

The routes read three public_api snapshot tables and nothing else, so a fake
session that refuses any other query is the whole contract: if one of these ever
reaches for agent_sessions.* or swarm.*, the test fails rather than the public
tier 503-ing in prod.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.exc import SQLAlchemyError

from agent_sessions.public_router import router
from core.db import get_session

_ACTIVITY = {
    "snapshotted_at": "2026-09-11T10:00:00+00:00",
    "state": "enabled",
    "policy": {"conductor_model": "opus", "max_review_rounds": 2},
    "active": [{"issue_number": 6014, "state": "in flight"}],
    "queued": [],
    "recent": [],
}
_TASK = {
    "snapshotted_at": "2026-09-11T10:00:00+00:00",
    "policy": {"conductor_model": "opus"},
    "task": {"issue_number": 6014, "brief": ["publish the board"], "nodes": []},
}
_SESSION_KEY = "factory:task-abc:implement_fix:2"
_SESSION = {
    "snapshotted_at": "2026-09-11T10:00:00+00:00",
    "session": {"key": _SESSION_KEY, "issue_number": 6014, "turn_count": 2},
    "turns": [],
}


class _Result:
    def __init__(self, row):
        self._row = row

    def first(self):
        return self._row


class _FakeSession:
    """Answers only the three snapshot queries; anything else is a test failure."""

    def __init__(self, *, activity=_ACTIVITY, tasks=None, sessions=None):
        self.activity = activity
        self.tasks = {6014: _TASK} if tasks is None else tasks
        self.sessions = {_SESSION_KEY: _SESSION} if sessions is None else sessions
        self.statements: list[str] = []

    def execute(self, statement, params=None):
        sql = str(statement)
        self.statements.append(sql)
        params = params or {}
        if "public_api.factory_activity_snapshot" in sql:
            return _Result(self._row(self.activity))
        if "public_api.factory_task_snapshot" in sql:
            return _Result(self._row(self.tasks.get(params["issue_number"])))
        if "public_api.factory_session_snapshot" in sql:
            return _Result(self._row(self.sessions.get(params["session_key"])))
        raise AssertionError(f"unexpected query: {sql}")

    @staticmethod
    def _row(payload):
        if payload is None:
            return None
        return {"payload": payload, "snapshotted_at": payload["snapshotted_at"]}


def _client(fake_session):
    app = FastAPI()
    app.include_router(router)

    def override_session():
        yield fake_session

    app.dependency_overrides[get_session] = override_session
    return TestClient(app, raise_server_exceptions=False)


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("/api/agents/public/factory/activity", _ACTIVITY),
        ("/api/agents/public/factory/tasks/6014", _TASK),
        (f"/api/agents/public/factory/sessions/{_SESSION_KEY}", _SESSION),
    ],
)
def test_each_route_serves_its_payload_with_a_stable_cached_etag(path, expected):
    fake_session = _FakeSession()
    with _client(fake_session) as client:
        first = client.get(path)
        second = client.get(path)

        assert first.status_code == 200
        assert first.json() == expected
        assert first.headers["cache-control"] == "public, max-age=60, s-maxage=60"
        assert first.headers["etag"] == second.headers["etag"]

        unchanged = client.get(path, headers={"If-None-Match": first.headers["etag"]})
        assert unchanged.status_code == 304
        assert unchanged.content == b""
        assert unchanged.headers["etag"] == first.headers["etag"]
        assert unchanged.headers["cache-control"] == first.headers["cache-control"]

    assert fake_session.statements
    assert all("public_api.factory_" in sql for sql in fake_session.statements)
    assert all("agent_sessions." not in sql for sql in fake_session.statements)
    assert all("swarm." not in sql for sql in fake_session.statements)


def test_the_three_payload_kinds_do_not_share_an_etag():
    fake_session = _FakeSession()
    with _client(fake_session) as client:
        tags = {
            client.get("/api/agents/public/factory/activity").headers["etag"],
            client.get("/api/agents/public/factory/tasks/6014").headers["etag"],
            client.get(f"/api/agents/public/factory/sessions/{_SESSION_KEY}").headers[
                "etag"
            ],
        }
    assert len(tags) == 3


def test_activity_is_404_until_the_job_has_written_a_snapshot():
    with _client(_FakeSession(activity=None)) as client:
        response = client.get("/api/agents/public/factory/activity")
    assert response.status_code == 404
    assert response.json() == {"detail": "no snapshot"}


def test_an_unpublished_task_or_session_is_404():
    with _client(_FakeSession(tasks={}, sessions={})) as client:
        task = client.get("/api/agents/public/factory/tasks/9999")
        session = client.get("/api/agents/public/factory/sessions/factory:x:y:1")
    assert task.status_code == 404 and task.json() == {"detail": "unknown task"}
    assert session.status_code == 404
    assert session.json() == {"detail": "unknown session"}


def test_a_session_key_keeps_every_colon_of_a_node_key_that_has_one():
    # Node keys may contain colons, so the key is never parsed to recover one.
    key = "factory:task-abc:implement:fix:probe:3"
    fake_session = _FakeSession(sessions={key: _SESSION})
    with _client(fake_session) as client:
        response = client.get(f"/api/agents/public/factory/sessions/{key}")
    assert response.status_code == 200


def test_a_percent_encoded_session_key_still_matches():
    # The frontend proxy sends the key through encodeURIComponent, so every
    # colon arrives as %3A and the server has to resolve it back to the key.
    from urllib.parse import quote

    fake_session = _FakeSession()
    with _client(fake_session) as client:
        response = client.get(
            "/api/agents/public/factory/sessions/" + quote(_SESSION_KEY, safe="")
        )
    assert response.status_code == 200
    assert response.json() == _SESSION


def test_a_broken_database_is_a_500_not_a_partial_board():
    class _BrokenSession:
        def execute(self, statement, params=None):
            raise SQLAlchemyError("factory snapshot table is unavailable")

    with _client(_BrokenSession()) as client:
        response = client.get("/api/agents/public/factory/activity")

    assert response.status_code == 500
    assert response.json() == {"detail": "factory unavailable"}
