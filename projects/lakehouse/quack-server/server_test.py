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
from fastapi import HTTPException

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
    """Yields each preloaded batch once, then signals stop and idles.

    Once the batches are exhausted it sets ``stop`` (so the consumer's
    ``while not stop.is_set()`` exits deterministically on the next check) and
    raises ``TimeoutError`` to exercise the consumer's idle/re-poll branch. This
    keeps the test free of sleep-based timing races.
    """

    def __init__(self, batches: list[list[_FakeMsg]], *, stop: asyncio.Event):
        self._batches = list(batches)
        self._stop = stop

    async def fetch(self, batch=None, *, timeout=5.0):
        if self._batches:
            return self._batches.pop(0)
        self._stop.set()
        raise TimeoutError


class _FakeClient:
    """NatsClient stand-in returning a preloaded :class:`_FakeSub`."""

    def __init__(self, sub: _FakeSub):
        self._sub = sub
        self.subscribed: tuple[str, str] | None = None
        self.subscribe_kwargs: dict = {}

    async def pull_subscribe(
        self,
        subject,
        durable,
        *,
        batch=10,
        deliver_last_per_subject=False,
        inactive_threshold=None,
    ):
        self.subscribed = (subject, durable)
        self.subscribe_kwargs = {
            "deliver_last_per_subject": deliver_last_per_subject,
            "inactive_threshold": inactive_threshold,
        }
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


def _run_consumer_once(state, msgs: list[_FakeMsg]) -> _FakeClient:
    """Run the swap consumer over a single preloaded batch, deterministically.

    The fake subscription sets ``stop`` once the batch is drained, so the loop
    exits on its next condition check. ``asyncio.wait_for`` bounds the run so a
    regression that re-introduces an event-loop-starving busy spin fails fast
    instead of hanging the whole test target to its 300s timeout.
    """
    stop = asyncio.Event()
    sub = _FakeSub([msgs], stop=stop)
    client = _FakeClient(sub)

    async def drive():
        await asyncio.wait_for(
            server.run_swap_consumer(state, client, stop=stop, poll_timeout=0.01),
            timeout=5.0,
        )

    asyncio.run(drive())
    return client


def test_consumer_issues_attach_or_replace_and_acks():
    state, executed = _recording_state()
    path = "s3://warehouse/serving/notes-v9.duckdb"
    msg = _FakeMsg(
        json.dumps({"payload": {"artifact_url": path, "version": "v9"}}).encode()
    )

    client = _run_consumer_once(state, [msg])

    # Subscribed to the right subject + durable.
    assert client.subscribed == (server.ARTIFACT_READY_SUBJECT, server.SWAP_DURABLE)
    # Fan-out config: every Quack pod must hot-swap, so the swap consumer starts
    # at LAST_PER_SUBJECT (picks up the current artifact on (re)start without
    # replaying since-deleted history) with an inactive_threshold so orphaned
    # per-pod durables self-clean. A shared durable would make this a queue group
    # where only ONE pod swaps — the bug this asserts against.
    assert client.subscribe_kwargs["deliver_last_per_subject"] is True
    assert (
        client.subscribe_kwargs["inactive_threshold"]
        == server.SWAP_CONSUMER_INACTIVE_THRESHOLD_S
    )
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

    _run_consumer_once(state, [msg])

    assert msg.termed is True
    assert msg.acked is False


def test_swap_durable_is_per_pod_unique():
    # The swap durable must be per-pod (host-suffixed), NOT the bare shared name.
    # A shared durable forms a JetStream queue group where only one pod receives
    # each artifact-ready message, so only one of N Quack replicas hot-swaps and
    # the rest serve a stale snapshot. The host suffix makes every pod its own
    # fan-out consumer.
    assert server.SWAP_DURABLE.startswith("quack-serving-swap-")
    assert server.SWAP_DURABLE != "quack-serving-swap"


# --------------------------------------------------------------------------- #
# HTTP API (module-level handlers — no HTTP client, no httpx/TestClient)
# --------------------------------------------------------------------------- #


def test_healthz_returns_ok_and_version():
    con = duckdb.connect(":memory:")
    state = server.ServingState(con, version="v3")
    assert server.healthz_payload(state) == {"status": "ok", "artifact_version": "v3"}


def test_create_app_registers_healthz_and_search_routes():
    con = duckdb.connect(":memory:")
    state = server.ServingState(con, version="v3")
    app = server.create_app(state)
    paths = {route.path for route in app.routes}
    assert {"/healthz", "/search"}.issubset(paths)


def test_require_query_token_rejects_missing_token():
    # When a token is configured, a missing/invalid Authorization header -> 401.
    with pytest.raises(HTTPException) as exc:
        server.require_query_token(None, "sekret")
    assert exc.value.status_code == 401


def test_require_query_token_accepts_matching_bearer():
    # Correct bearer token returns without raising.
    server.require_query_token("Bearer sekret", "sekret")


def test_require_query_token_open_when_unset():
    # No configured token -> endpoint is open (returns without raising).
    server.require_query_token(None, None)


def test_search_rejects_bad_k():
    con = duckdb.connect(":memory:")
    state = server.ServingState(con, version="v1")
    with pytest.raises(HTTPException) as exc:
        server.do_search(state, server.SearchRequest(query=[0.1, 0.2], k=0))
    assert exc.value.status_code == 400

    with pytest.raises(HTTPException) as exc:
        server.do_search(
            state, server.SearchRequest(query=[0.1, 0.2], k=server._MAX_SEARCH_K + 1)
        )
    assert exc.value.status_code == 400
