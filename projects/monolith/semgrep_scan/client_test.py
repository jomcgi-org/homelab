"""Tests for the EmberVM Semgrep client."""

from __future__ import annotations

import asyncio
from typing import ClassVar

import pytest

from semgrep_scan import client


class _Resp:
    def __init__(self, data, status_code=200, headers=None):
        self._data = data
        self.status_code = status_code
        self.headers = headers or {}

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
        return _Resp({"findings": [], "errors": [], "raw_cli_output": {"results": []}})


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


@pytest.mark.asyncio
async def test_pending_scan_polls_same_task_through_retry_until_result(monkeypatch):
    gets = []
    replies = iter(
        [
            {"state": "running"},
            {"state": "failed_retryable"},
            {"state": "queued"},
            {"state": "succeeded"},
            {"findings": [], "raw_cli_output": {"results": []}},
        ]
    )

    class PendingClient(_FakeClient):
        async def post(self, url, json=None, headers=None):
            await super().post(url, json=json, headers=headers)
            return _Resp({"task_id": "scan-1", "state": "queued"}, 202)

        async def get(self, url, headers=None):
            gets.append(url)
            return _Resp(next(replies))

    monkeypatch.setattr(client.httpx, "AsyncClient", PendingClient)
    monkeypatch.setattr(client, "SEMGREP_POLL_INTERVAL", 0)
    result = await client.scan_files([{"path": "a.py", "content": "pass"}])
    assert result["raw_cli_output"] == {"results": []}
    assert len(_FakeClient.posts) == 1
    assert gets == ["http://ev/v1/tasks/scan-1"] * 4 + [
        "http://ev/v1/tasks/scan-1/result"
    ]


@pytest.mark.asyncio
async def test_observation_timeout_retains_task_without_resubmitting(monkeypatch):
    class PendingClient(_FakeClient):
        async def post(self, url, json=None, headers=None):
            await super().post(url, json=json, headers=headers)
            return _Resp({"task_id": "scan-1"}, 202)

        async def get(self, url, headers=None):
            await asyncio.sleep(1)

    monkeypatch.setattr(client.httpx, "AsyncClient", PendingClient)
    monkeypatch.setattr(client, "SEMGREP_COMPLETION_TIMEOUT", 0.01)
    result = await client.scan_files([{"path": "a.py", "content": "pass"}])
    assert result == {
        "error": "timed out waiting for scan completion",
        "task_id": "scan-1",
    }
    assert len(_FakeClient.posts) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["failed_permanent", "dead_lettered"])
async def test_terminal_scan_failure_never_fetches_or_reports_results(
    monkeypatch, state
):
    gets = []

    class FailedClient(_FakeClient):
        async def post(self, url, json=None, headers=None):
            return _Resp({"task_id": "scan-1"}, 202)

        async def get(self, url, headers=None):
            gets.append(url)
            return _Resp({"state": state})

    monkeypatch.setattr(client.httpx, "AsyncClient", FailedClient)
    result = await client.scan_files([{"path": "a.py", "content": "pass"}])
    assert result["error"] == f"embervm scan ended in state {state}"
    assert gets == ["http://ev/v1/tasks/scan-1"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "data,headers",
    [
        ({"findings": [], "errors": ["EOF"]}, {}),
        ({"raw_cli_output": {"results": []}}, {"x-ember-truncated": "true"}),
    ],
)
async def test_incomplete_scan_is_not_a_reportable_success(monkeypatch, data, headers):
    class IncompleteClient(_FakeClient):
        async def post(self, url, json=None, headers=None):
            return _Resp(data, headers=response_headers)

    response_headers = headers
    monkeypatch.setattr(client.httpx, "AsyncClient", IncompleteClient)
    result = await client.scan_files([{"path": "a.py", "content": "pass"}])
    assert "error" in result
    assert "raw_cli_output" not in result
