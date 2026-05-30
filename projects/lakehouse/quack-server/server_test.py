"""Hermetic tests for the Quack serving pod (no network, no DuckDB extensions).

The hot-swap consumer is exercised with a fake NATS subscription and an in-memory
``duckdb.connect(':memory:')`` connection (no httpfs/iceberg/vss install). The
assertions target two things platform/004 cares about:

  * the ``artifact-ready`` handler issues exactly the ``ATTACH OR REPLACE`` SQL
    from :func:`duckdb_query.attach_or_replace_sql`, and acks the message;
  * ``/healthz`` returns ok and surfaces the current artifact version.
"""

from __future__ import annotations

import asyncio
import json

import duckdb
import pytest
from fastapi.testclient import TestClient

import server
from projects.lakehouse import duckdb_query


# --------------------------------------------------------------------------- #
# Fakes
# --------------------------------------------------------------------------- #


class _FakeMsg:
    """A fake JetStream message recording ack/term calls."""

    def __init__(self, data: bytes):
        self.data = data
        self.acked = False
        self.termed = False

    async def ack(self) -> None:
        self.acked = True

    async def term(self) -> None:
        self.termed = True


class _FakeSub:
    """Yields one batch of messages then raises TimeoutError to idle the loop."""

    def __init__(self, batches: list[list[_FakeMsg]]):
        self._batches = list(batches)

    async def fetch(self, batch=None, *, timeout=5.0):
        if self._batches:
            return self._batches.pop(0)
        raise TimeoutError


class _FakeClient:
    """NatsClient stand-in returning a preloaded :class:`_FakeSub`."""

    def __init__(self, sub: _FakeSub):
        self._sub = sub
        self.subscribed: tuple[str, str] | None = None

    async def pull_subscribe(self, subject, durable, *, batch=10):
        self.subscribed = (subject, durable)
        return self._sub


class _SpyConn:
    """Wraps a real :memory: DuckDB connection, recording executed SQL.

    DuckDB's ``execute`` is a C-extension method that cannot be monkey-patched,
    so the connection is wrapped rather than mutated. ``ATTACH OR REPLACE`` is
    recorded only (the remote artifact does not exist in the hermetic test); all
    other SQL is delegated to the real connection.
    """

    def __init__(self) -> None:
        self._con = duckdb.connect(":memory:")
        self.executed: list[str] = []

    def execute(self, sql, *args, **kwargs):
        self.executed.append(sql)
        if sql.startswith("ATTACH OR REPLACE"):
            return self
        return self._con.execute(sql, *args, **kwargs)


def _recording_state() -> tuple[server.ServingState, list[str]]:
    """A ServingState whose connection records every executed SQL string."""
    spy = _SpyConn()
    return server.ServingState(spy, version="v0"), spy.executed


# --------------------------------------------------------------------------- #
# parse_artifact_ready
# --------------------------------------------------------------------------- #


def test_parse_artifact_ready_accepts_artifact_url():
    body = json.dumps(
        {
            "payload": {
                "artifact_url": "s3://warehouse/serving/notes-v7.duckdb",
                "version": "v7",
            }
        }
    ).encode()
    path, version = server.parse_artifact_ready(body)
    assert path == "s3://warehouse/serving/notes-v7.duckdb"
    assert version == "v7"


def test_parse_artifact_ready_accepts_path_alias():
    body = json.dumps(
        {"payload": {"path": "s3://warehouse/serving/notes-v8.duckdb"}}
    ).encode()
    path, version = server.parse_artifact_ready(body)
    assert path == "s3://warehouse/serving/notes-v8.duckdb"
    assert version is None


def test_parse_artifact_ready_rejects_missing_path():
    with pytest.raises(ValueError):
        server.parse_artifact_ready(json.dumps({"payload": {}}).encode())


# --------------------------------------------------------------------------- #
# Hot-swap consumer
# --------------------------------------------------------------------------- #


def test_consumer_issues_attach_or_replace_and_acks():
    state, executed = _recording_state()
    path = "s3://warehouse/serving/notes-v9.duckdb"
    msg = _FakeMsg(
        json.dumps({"payload": {"artifact_url": path, "version": "v9"}}).encode()
    )
    sub = _FakeSub([[msg]])
    client = _FakeClient(sub)

    stop = asyncio.Event()

    async def drive():
        task = asyncio.create_task(
            server.run_swap_consumer(state, client, stop=stop, poll_timeout=0.01)
        )
        # Let the loop drain the one batch, then hit the idle TimeoutError path.
        await asyncio.sleep(0.05)
        stop.set()
        await task

    asyncio.run(drive())

    # Subscribed to the right subject + durable.
    assert client.subscribed == (server.ARTIFACT_READY_SUBJECT, server.SWAP_DURABLE)
    # Exactly the ATTACH OR REPLACE SQL the pure builder produces was executed.
    expected_sql = duckdb_query.attach_or_replace_sql(server.SERVING_ALIAS, path)
    assert expected_sql in executed
    # Version tracked + message acked.
    assert state.version == "v9"
    assert msg.acked is True
    assert msg.termed is False


def test_consumer_terminates_malformed_message():
    state, _ = _recording_state()
    msg = _FakeMsg(json.dumps({"payload": {}}).encode())
    sub = _FakeSub([[msg]])
    client = _FakeClient(sub)
    stop = asyncio.Event()

    async def drive():
        task = asyncio.create_task(
            server.run_swap_consumer(state, client, stop=stop, poll_timeout=0.01)
        )
        await asyncio.sleep(0.05)
        stop.set()
        await task

    asyncio.run(drive())

    assert msg.termed is True
    assert msg.acked is False


# --------------------------------------------------------------------------- #
# HTTP API
# --------------------------------------------------------------------------- #


def test_healthz_returns_ok_and_version():
    con = duckdb.connect(":memory:")
    state = server.ServingState(con, version="v3")
    app = server.create_app(state)
    client = TestClient(app)

    resp = client.get("/healthz")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok", "artifact_version": "v3"}


def test_search_requires_token_when_configured():
    con = duckdb.connect(":memory:")
    state = server.ServingState(con, version="v1")
    app = server.create_app(state, query_token="sekret")
    client = TestClient(app)

    # No token -> 401 (auth runs before the query).
    resp = client.post("/search", json={"query": [0.1, 0.2], "k": 5})
    assert resp.status_code == 401


def test_search_rejects_bad_k():
    con = duckdb.connect(":memory:")
    state = server.ServingState(con, version="v1")
    app = server.create_app(state)  # no token -> auth open
    client = TestClient(app)

    resp = client.post("/search", json={"query": [0.1, 0.2], "k": 0})
    assert resp.status_code == 400
