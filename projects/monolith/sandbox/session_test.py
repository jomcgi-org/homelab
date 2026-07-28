"""Tests for sessioned run_python (client.py session surface, EmberVM R2).

Hermetic: httpx.AsyncClient is a fake that records each request and returns a
scripted response, and the sandbox.session table is an in-memory SQLite engine
(SQLModel.metadata.create_all), so create-reuse-close, the 410 transparent
re-create + session_reset flag, and "the token is never logged" are all asserted
without a cluster.
"""

from __future__ import annotations

import contextlib
import logging

import pytest
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

from sandbox import client
from sandbox.models import SandboxSession  # noqa: F401  (registers the table)

# A fake token used only to prove the client never logs it (see
# test_token_never_logged); not a real credential.
SECRET_TOKEN = "tok-super-secret-value-do-not-log"  # nosemgrep: no-hardcoded-secret


class _Resp:
    def __init__(self, status_code=200, data=None, text=""):
        self.status_code = status_code
        self._data = data or {}
        self.text = text

    def raise_for_status(self):
        # No scripted test drives a non-410 error status through
        # raise_for_status, so a plain exception is enough and avoids
        # constructing a real httpx.Response for the error path.
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return self._data


class _FakeClient:
    # Queue of (method, response) the fake replays in order; each entry also
    # records the request it saw for assertions.
    script: list = []
    seen: list = []

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def _record(self, method, url, json=None, headers=None):
        _FakeClient.seen.append(
            {"method": method, "url": url, "json": json, "headers": headers or {}}
        )
        if not _FakeClient.script:
            raise AssertionError(f"unexpected {method} {url}; script exhausted")
        return _FakeClient.script.pop(0)

    async def post(self, url, json=None, headers=None):
        return await self._record("POST", url, json=json, headers=headers)

    async def delete(self, url, headers=None):
        return await self._record("DELETE", url, headers=headers)


@pytest.fixture(autouse=True)
def _wired(monkeypatch):
    _FakeClient.script = []
    _FakeClient.seen = []
    monkeypatch.setattr(client.httpx, "AsyncClient", _FakeClient)
    monkeypatch.setattr(client, "EMBERVM_URL", "http://ev")
    monkeypatch.setattr(client, "SANDBOX_SESSION_WORKLOAD", "sandbox-session")

    # StaticPool + a single shared connection keeps the in-memory DB alive
    # across the several _db_session() opens one invoke makes (each open is a
    # fresh connection; without StaticPool it would be a fresh empty DB).
    # SQLite has no schemas, so strip the Postgres schema qualifier before
    # create_all and restore it after (mirrors faas/models_test.py).
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    original_schemas = {}
    for table in SQLModel.metadata.tables.values():
        if table.schema is not None:
            original_schemas[table.name] = table.schema
            table.schema = None
    try:
        SQLModel.metadata.create_all(engine)

        @contextlib.contextmanager
        def fake_db():
            with Session(engine) as s:
                yield s

        monkeypatch.setattr(client, "_db_session", fake_db)
        yield
    finally:
        for table in SQLModel.metadata.tables.values():
            if table.name in original_schemas:
                table.schema = original_schemas[table.name]


def _create_resp(session_id="s-abc", token=SECRET_TOKEN):
    return _Resp(
        status_code=201,
        data={
            "session_id": session_id,
            "session_token": token,
            "expires_at": 1700000000000,
        },
    )


def _exec_resp(stdout, session_reset=False):
    return _Resp(
        status_code=200,
        data={"stdout": stdout, "exit_code": 0, "session_reset": session_reset},
    )


@pytest.mark.asyncio
async def test_first_use_creates_then_invokes():
    _FakeClient.script = [_create_resp(), _exec_resp("42\n")]
    result = await client.invoke_session("h1", "print(6*7)")
    assert result["stdout"] == "42\n"
    # A fresh session has empty state, so the first use reports a reset.
    assert result["session_reset"] is True
    # First call created, second invoked with the session token as bearer.
    create_req, invoke_req = _FakeClient.seen
    assert create_req["url"] == "http://ev/v1/workloads/sandbox-session/sessions"
    assert invoke_req["url"] == "http://ev/v1/sessions/s-abc/invoke"
    assert invoke_req["headers"]["Authorization"] == f"Bearer {SECRET_TOKEN}"
    assert invoke_req["json"]["mode"] == "session"


@pytest.mark.asyncio
async def test_second_use_reuses_without_recreating():
    _FakeClient.script = [_create_resp(), _exec_resp("first\n")]
    await client.invoke_session("h2", "x = 1")
    # Second use: only an invoke, no new create, and no reset.
    _FakeClient.seen = []
    _FakeClient.script = [_exec_resp("second\n")]
    result = await client.invoke_session("h2", "print(x)")
    assert result["stdout"] == "second\n"
    assert result["session_reset"] is False
    assert len(_FakeClient.seen) == 1
    assert _FakeClient.seen[0]["url"] == "http://ev/v1/sessions/s-abc/invoke"


@pytest.mark.asyncio
async def test_410_transparently_recreates_and_flags_reset():
    # Bind the handle first.
    _FakeClient.script = [_create_resp(session_id="s-old"), _exec_resp("ok\n")]
    await client.invoke_session("h3", "y = 1")

    # Next invoke 410s; the client re-creates (new id) and invokes again.
    _FakeClient.seen = []
    _FakeClient.script = [
        _Resp(status_code=410, data={"reason": "expired"}),
        _create_resp(session_id="s-new"),
        _exec_resp("recovered\n"),
    ]
    result = await client.invoke_session("h3", "print('after')")
    assert result["stdout"] == "recovered\n"
    assert result["session_reset"] is True
    urls = [r["url"] for r in _FakeClient.seen]
    assert urls == [
        "http://ev/v1/sessions/s-old/invoke",
        "http://ev/v1/workloads/sandbox-session/sessions",
        "http://ev/v1/sessions/s-new/invoke",
    ]


@pytest.mark.asyncio
async def test_close_destroys_and_drops_row():
    _FakeClient.script = [_create_resp(session_id="s-close"), _exec_resp("ok\n")]
    await client.invoke_session("h4", "z = 1")

    _FakeClient.seen = []
    _FakeClient.script = [_Resp(status_code=200)]
    result = await client.close_session("h4")
    assert result == {"closed": True}
    assert _FakeClient.seen[0]["method"] == "DELETE"
    assert _FakeClient.seen[0]["url"] == "http://ev/v1/sessions/s-close"

    # The row is gone: the next invoke must create a fresh session.
    _FakeClient.seen = []
    _FakeClient.script = [_create_resp(session_id="s-fresh"), _exec_resp("new\n")]
    again = await client.invoke_session("h4", "print(1)")
    assert again["session_reset"] is True
    assert (
        _FakeClient.seen[0]["url"] == "http://ev/v1/workloads/sandbox-session/sessions"
    )


@pytest.mark.asyncio
async def test_close_unknown_handle_is_idempotent():
    result = await client.close_session("never-created")
    assert result == {"closed": True}
    # Nothing was sent to EmberVM for an unknown handle.
    assert _FakeClient.seen == []


@pytest.mark.asyncio
async def test_token_never_logged(caplog):
    _FakeClient.script = [_create_resp(), _exec_resp("ok\n")]
    with caplog.at_level(logging.DEBUG, logger="sandbox.client"):
        await client.invoke_session("h5", "print(1)")
    for record in caplog.records:
        assert SECRET_TOKEN not in record.getMessage()


@pytest.mark.asyncio
async def test_one_shot_path_untouched_when_session_absent(monkeypatch):
    # With no session handle, run_python_in_sandbox must NOT touch the session
    # surface: it routes to the EmberVM one-shot path exactly as before.
    called = {"fc": False}

    async def fake_fc(payload):
        called["fc"] = True
        return {"stdout": "one-shot\n", "exit_code": 0}

    monkeypatch.setattr(client, "_run_embervm", fake_fc)
    result = await client.run_python_in_sandbox("print(1)")
    assert result["stdout"] == "one-shot\n"
    assert called["fc"] is True
    assert "session_reset" not in result
