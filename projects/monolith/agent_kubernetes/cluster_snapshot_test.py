"""The agents snapshot reader needs only a SELECT and a worker-local session."""

from __future__ import annotations

import asyncio
import importlib
import json
import threading
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest
from sqlalchemy.exc import OperationalError

snapshot = importlib.import_module("agent_kubernetes.cluster_snapshot")
NOW = datetime(2026, 10, 2, 12, 0, tzinfo=timezone.utc)


class _Clock(datetime):
    @classmethod
    def now(cls, tz=None):
        return NOW if tz is not None else NOW.replace(tzinfo=None)


@pytest.fixture(autouse=True)
def clock(monkeypatch):
    monkeypatch.setattr(snapshot, "datetime", _Clock)


@pytest.fixture
def payload():
    return {
        "schema_version": 1,
        "complete": True,
        "errors": {},
        "scanned": {"applications": 2, "pods": 1},
        "applications": [
            {
                "name": name,
                "namespace": "argocd",
                "sync": "Synced",
                "health": "Healthy",
                "revision": "0.123.0",
                "target_revision": "0.124.0",
            }
            for name in ("monolith", "monolith-agents")
        ],
        "unhealthy": {"pods": [{"name": "failing-pod", "namespace": "monolith"}]},
        "unhealthy_truncated": {"pods": 2},
        "applications_truncated": 3,
    }


def _fake_session(monkeypatch, row=None, error=None):
    session = MagicMock()
    session.__enter__.return_value = session
    session.execute.return_value.first.return_value = row
    session.execute.side_effect = error
    monkeypatch.setattr(snapshot, "Session", lambda engine: session)
    monkeypatch.setattr(snapshot, "get_engine", lambda: object())
    return session


def test_fresh_snapshot_is_read_only(monkeypatch, payload):
    stamp = NOW - timedelta(seconds=120)
    session = _fake_session(monkeypatch, (payload, stamp))
    result = asyncio.run(snapshot.cluster_snapshot())
    assert result == {
        "ok": True,
        "snapshot_at": stamp.isoformat(),
        "age_seconds": 120.0,
        "stale": False,
        "complete": True,
        "snapshot": payload,
    }
    assert str(session.execute.call_args.args[0]) == (
        "SELECT payload, snapshot_at FROM agent_view.cluster_snapshot WHERE id = 1"
    )
    session.execute.assert_called_once()
    session.commit.assert_not_called()
    session.rollback.assert_not_called()


@pytest.mark.parametrize("age,stale", [(600, False), (601, True), (3600, True)])
def test_stale_snapshot_is_still_available(monkeypatch, payload, age, stale):
    _fake_session(monkeypatch, (payload, NOW - timedelta(seconds=age)))
    result = asyncio.run(snapshot.cluster_snapshot())
    assert result["ok"] is True
    assert result["stale"] is stale
    assert result["age_seconds"] == float(age)
    assert result["snapshot"] == payload


def test_incomplete_snapshot_is_unknown_even_when_fresh(monkeypatch, payload):
    payload["complete"] = False
    payload["errors"] = {"deployments": "TimeoutError: listing timed out"}
    _fake_session(monkeypatch, (payload, NOW))
    result = asyncio.run(snapshot.cluster_snapshot())
    assert result["ok"] is True
    assert result["stale"] is False
    assert result["complete"] is False
    assert result["snapshot"]["errors"] == payload["errors"]


@pytest.mark.parametrize(
    "application,names",
    [("monolith", ["monolith"]), ("missing", []), ("Monolith", [])],
)
def test_application_filter_is_exact_and_preserves_other_fields(
    monkeypatch, payload, application, names
):
    _fake_session(monkeypatch, (payload, NOW))
    result = asyncio.run(snapshot.cluster_snapshot(application))
    assert result["ok"] is True
    assert [row["name"] for row in result["snapshot"]["applications"]] == names
    assert len(payload["applications"]) == 2
    assert {
        key: value for key, value in result["snapshot"].items() if key != "applications"
    } == {key: value for key, value in payload.items() if key != "applications"}


def test_missing_row(monkeypatch):
    _fake_session(monkeypatch)
    result = asyncio.run(snapshot.cluster_snapshot())
    assert result["ok"] is False
    assert result["error"]["code"] == "not_found"


def test_database_failure_rolls_back_without_leaking_connection_details(monkeypatch):
    private = "postgresql://user:password@private-host/database"
    error = OperationalError("SELECT payload", {}, RuntimeError(private))
    session = _fake_session(monkeypatch, error=error)
    result = asyncio.run(snapshot.cluster_snapshot())
    session.rollback.assert_called_once()
    assert result["ok"] is False
    assert result["error"]["code"] == "unavailable"
    assert private not in json.dumps(result)
    assert "password" not in json.dumps(result)
    assert "private-host" not in json.dumps(result)


def test_engine_failure_is_unavailable(monkeypatch):
    def broken_engine():
        raise OperationalError("connect", {}, RuntimeError("private DSN"))

    monkeypatch.setattr(snapshot, "get_engine", broken_engine)
    result = asyncio.run(snapshot.cluster_snapshot())
    assert result["error"]["code"] == "unavailable"
    assert "private DSN" not in json.dumps(result)


@pytest.mark.parametrize("as_string", [False, True])
def test_string_json_and_naive_timestamp_are_parsed(monkeypatch, payload, as_string):
    stamp = (NOW - timedelta(seconds=60)).replace(tzinfo=None)
    _fake_session(
        monkeypatch,
        (json.dumps(payload), stamp.isoformat() if as_string else stamp),
    )
    result = asyncio.run(snapshot.cluster_snapshot("monolith"))
    assert result["ok"] is True
    assert result["snapshot_at"] == stamp.replace(tzinfo=timezone.utc).isoformat()
    assert result["age_seconds"] == 60.0
    assert result["stale"] is False
    assert result["snapshot"]["applications"] == payload["applications"][:1]


def test_session_is_created_and_used_off_the_event_loop(monkeypatch, payload):
    session = _fake_session(monkeypatch, (payload, NOW))
    main_thread = threading.get_ident()
    worker_threads = []

    def engine():
        worker_threads.append(threading.get_ident())
        return object()

    def session_factory(engine):
        worker_threads.append(threading.get_ident())
        return session

    def execute(statement):
        worker_threads.append(threading.get_ident())
        result = MagicMock()
        result.first.return_value = (payload, NOW)
        return result

    monkeypatch.setattr(snapshot, "get_engine", engine)
    monkeypatch.setattr(snapshot, "Session", session_factory)
    session.execute.side_effect = execute
    assert asyncio.run(snapshot.cluster_snapshot())["ok"] is True
    assert len(worker_threads) == 3
    assert len(set(worker_threads)) == 1
    assert main_thread not in worker_threads
