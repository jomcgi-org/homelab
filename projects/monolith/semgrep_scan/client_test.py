"""Tests for the EmberVM Semgrep client."""

from __future__ import annotations

import asyncio
from typing import ClassVar

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
    posts: ClassVar[list[dict]] = []

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
    await client.scan_files(
        [{"path": "a.py", "content": "print(1)"}], correlation_id="scan-123"
    )
    post = _FakeClient.posts[0]
    assert post["url"] == "http://ev/v1/workloads/semgrep/tasks?wait=true"
    assert "Idempotency-Key" in post["headers"]
    assert post["json"]["correlation_id"] == "scan-123"


@pytest.mark.asyncio
async def test_dedupe_false_omits_idempotency_key():
    await client.scan_files([{"path": "a.py", "content": "print(1)"}], dedupe=False)
    assert "Idempotency-Key" not in _FakeClient.posts[0]["headers"]


def test_content_key_is_order_independent():
    a = [{"path": "a.py", "content": "x"}, {"path": "b.py", "content": "y"}]
    b = [{"path": "b.py", "content": "y"}, {"path": "a.py", "content": "x"}]
    assert client._content_key(a) == client._content_key(b)


@pytest.mark.asyncio
async def test_successive_distinct_ids_and_omitted_id_do_not_share_task_identity():
    files = [{"path": "a.py", "content": "print(1)"}]
    await client.scan_files(files, correlation_id="first-id")
    await client.scan_files(files, correlation_id="second-id")
    await client.scan_files(files)

    first, second, omitted = _FakeClient.posts
    assert first["json"]["correlation_id"] == "first-id"
    assert second["json"]["correlation_id"] == "second-id"
    assert "correlation_id" not in omitted["json"]
    keys = {post["headers"]["Idempotency-Key"] for post in _FakeClient.posts}
    assert len(keys) == 3
    assert omitted["headers"]["Idempotency-Key"] == client._content_key(files)


@pytest.mark.asyncio
async def test_invalid_correlation_id_is_not_sent_or_returned():
    secret = "do not expose this secret"
    result = await client.scan_files(
        [{"path": "a.py", "content": "print(1)"}], correlation_id=secret
    )
    assert result == {"error": "invalid correlation_id"}
    assert secret not in result["error"]
    assert _FakeClient.posts == []


@pytest.mark.asyncio
async def test_overlapping_scans_keep_distinct_correlation_ids():
    files = [{"path": "a.py", "content": "print(1)"}]
    await asyncio.gather(
        client.scan_files(files, correlation_id="overlap-one"),
        client.scan_files(files, correlation_id="overlap-two"),
    )
    assert {post["json"]["correlation_id"] for post in _FakeClient.posts} == {
        "overlap-one",
        "overlap-two",
    }
