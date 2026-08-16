"""Tests for the EmberVM sandbox client.

Hermetic: `httpx.AsyncClient` is replaced by a fake that records each POST, so we
assert the EmberVM routing and Idempotency-Key.
"""

from __future__ import annotations

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
    monkeypatch.setattr(client.httpx, "AsyncClient", _FakeClient)
    monkeypatch.setattr(client, "EMBERVM_URL", "http://ev")
    monkeypatch.setattr(client, "SANDBOX_WORKLOAD_PREFIX", "sandbox-")
    monkeypatch.setattr(client, "SCRATCH_POSTGRES_DSN", "")
    yield


@pytest.mark.parametrize(
    ("language", "workload"),
    [
        ("python", "sandbox-python"),
        ("go", "sandbox-go"),
        ("rust", "sandbox-rust"),
        ("elixir", "sandbox-elixir"),
        ("ocaml", "sandbox-ocaml"),
        ("javascript", "sandbox-javascript"),
    ],
)
@pytest.mark.asyncio
async def test_supported_language_routes_to_its_workload(language, workload):
    await client.run_code_in_sandbox("source", language=language)

    post = _FakeClient.posts[0]
    assert post["url"] == f"http://ev/v1/workloads/{workload}/tasks?wait=true"
    assert "Idempotency-Key" in post["headers"]
    assert post["json"]["code"] == "source"


@pytest.mark.asyncio
async def test_unsupported_language_short_circuits_before_http():
    result = await client.run_code_in_sandbox("puts 42", language="ruby")

    assert "unsupported language 'ruby'" in result["error"]
    for language in client.SUPPORTED_LANGUAGES:
        assert language in result["error"]
    assert _FakeClient.posts == []


@pytest.mark.asyncio
async def test_idempotency_key_includes_language():
    await client.run_code_in_sandbox("same source", language="python")
    await client.run_code_in_sandbox("same source", language="javascript")

    python_key = _FakeClient.posts[0]["headers"]["Idempotency-Key"]
    javascript_key = _FakeClient.posts[1]["headers"]["Idempotency-Key"]
    assert python_key != javascript_key


@pytest.mark.asyncio
async def test_empty_code_short_circuits():
    result = await client.run_code_in_sandbox("   ")
    assert "error" in result
    assert _FakeClient.posts == []
