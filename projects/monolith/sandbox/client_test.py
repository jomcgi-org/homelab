"""Tests for the sandbox dual-path dispatch (client.py, R0 cutover).

Hermetic: `httpx.AsyncClient` is replaced by a fake that records each POST, so we
assert fc-invoke (default) vs embervm routing and the EmberVM Idempotency-Key.
"""

from __future__ import annotations

import httpx
import pytest

from sandbox import client


class _Resp:
    def __init__(self, data):
        self._data = data
        self.status_code = 200
        self.text = ""

    def raise_for_status(self):
        return None

    def json(self):
        return self._data


class _FakeClient:
    posts: list[dict] = []

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def post(self, url, json=None, headers=None):
        _FakeClient.posts.append({"url": url, "json": json, "headers": headers or {}})
        return _Resp({"stdout": "42\n", "exit_code": 0})


@pytest.fixture(autouse=True)
def _fake(monkeypatch):
    _FakeClient.posts = []
    original = client.SANDBOX_DISPATCH
    monkeypatch.setattr(client.httpx, "AsyncClient", _FakeClient)
    monkeypatch.setattr(client, "FC_INVOKE_URL", "http://fc")
    monkeypatch.setattr(client, "EMBERVM_URL", "http://ev")
    yield
    client.SANDBOX_DISPATCH = original


@pytest.mark.asyncio
async def test_fc_invoke_is_the_default():
    client.SANDBOX_DISPATCH = "fc-invoke"
    result = await client.run_python_in_sandbox("print(6*7)")
    assert _FakeClient.posts[0]["url"] == "http://fc/invoke/sandbox"
    assert result["exit_code"] == 0


@pytest.mark.asyncio
async def test_embervm_mode_posts_to_submit_api_with_idempotency_key():
    client.SANDBOX_DISPATCH = "embervm"
    await client.run_python_in_sandbox("print(6*7)")
    post = _FakeClient.posts[0]
    assert post["url"] == "http://ev/v1/workloads/sandbox/tasks?wait=true"
    assert "Idempotency-Key" in post["headers"]
    assert post["json"]["code"] == "print(6*7)"


@pytest.mark.asyncio
async def test_empty_code_short_circuits():
    client.SANDBOX_DISPATCH = "embervm"
    result = await client.run_python_in_sandbox("   ")
    assert "error" in result
    assert _FakeClient.posts == []
