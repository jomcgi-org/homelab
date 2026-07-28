"""Tests for the EmberVM Semgrep client."""

from __future__ import annotations

import httpx
import pytest

from semgrep_scan import client


class _Resp:
    def __init__(self, data):
        self._data = data

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
        self.posts.append({"url": url, "json": json, "headers": headers or {}})
        return _Resp({"findings": [], "errors": []})


@pytest.fixture(autouse=True)
def _fake(monkeypatch):
    _FakeClient.posts = []
    monkeypatch.setattr(client.httpx, "AsyncClient", _FakeClient)
    monkeypatch.setattr(client, "EMBERVM_URL", "http://ev")


@pytest.mark.asyncio
async def test_posts_to_embervm_with_idempotency_key():
    await client.scan_files([{"path": "a.py", "content": "print(1)"}])
    post = _FakeClient.posts[0]
    assert post["url"] == "http://ev/v1/workloads/semgrep/tasks?wait=true"
    assert "Idempotency-Key" in post["headers"]


@pytest.mark.asyncio
async def test_dedupe_false_omits_idempotency_key():
    await client.scan_files([{"path": "a.py", "content": "print(1)"}], dedupe=False)
    assert "Idempotency-Key" not in _FakeClient.posts[0]["headers"]


def test_content_key_is_order_independent():
    a = [{"path": "a.py", "content": "x"}, {"path": "b.py", "content": "y"}]
    b = [{"path": "b.py", "content": "y"}, {"path": "a.py", "content": "x"}]
    assert client._content_key(a) == client._content_key(b)
