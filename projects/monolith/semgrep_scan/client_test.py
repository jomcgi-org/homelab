"""Tests for the dual-path semgrep dispatch (client.py, Task 15).

Hermetic: no live fc-invoke or EmberVM. `httpx.AsyncClient` is replaced by a fake
that records each POST and returns a scripted response routed by URL, so we assert
which backend each dispatch mode calls, that the EmberVM path carries an
Idempotency-Key, and that shadow mode serves fc-invoke while mirroring to EmberVM
and tallying divergence.
"""

from __future__ import annotations

import asyncio

import httpx
import pytest

from semgrep_scan import client


class _Resp:
    def __init__(self, data, status=200):
        self._data = data
        self.status_code = status
        self.text = "err"

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError(
                "error",
                request=httpx.Request("POST", "http://x"),
                response=httpx.Response(self.status_code),
            )

    def json(self):
        return self._data


class _FakeClient:
    posts: list[dict] = []
    routes: dict[str, _Resp] = {}

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def post(self, url, json=None, headers=None):
        _FakeClient.posts.append({"url": url, "json": json, "headers": headers or {}})
        for fragment, resp in _FakeClient.routes.items():
            if fragment in url:
                return resp
        return _Resp({"findings": [], "errors": []})


@pytest.fixture(autouse=True)
def _fake(monkeypatch):
    _FakeClient.posts = []
    _FakeClient.routes = {}
    original_dispatch = client.SEMGREP_DISPATCH
    monkeypatch.setattr(client.httpx, "AsyncClient", _FakeClient)
    monkeypatch.setattr(client, "FC_INVOKE_URL", "http://fc")
    monkeypatch.setattr(client, "EMBERVM_URL", "http://ev")
    client.shadow_stats.update(total=0, match=0, diverged=0, embervm_error=0)
    yield
    # SEMGREP_DISPATCH is set directly by tests (below); restore it so a mode
    # never leaks into another test.
    client.SEMGREP_DISPATCH = original_dispatch


_FILES = [{"path": "a.py", "content": "import subprocess\n"}]


async def _drain():
    """Await any fire-and-forget shadow task scheduled on the loop."""
    pending = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
    if pending:
        await asyncio.gather(*pending)


@pytest.mark.asyncio
async def test_fc_invoke_is_the_default():
    _FakeClient.routes = {
        "/invoke/semgrep": _Resp({"findings": [{"rule_id": "r"}], "errors": []})
    }
    monkeypatch_dispatch("fc-invoke")

    result = await client.scan_files(_FILES)

    assert [p["url"] for p in _FakeClient.posts] == ["http://fc/invoke/semgrep"]
    assert len(result["findings"]) == 1


@pytest.mark.asyncio
async def test_embervm_mode_posts_to_submit_api_with_idempotency_key():
    _FakeClient.routes = {
        "/v1/workloads/semgrep/tasks": _Resp({"findings": [], "errors": []})
    }
    monkeypatch_dispatch("embervm")

    await client.scan_files(_FILES)

    assert len(_FakeClient.posts) == 1
    post = _FakeClient.posts[0]
    assert post["url"] == "http://ev/v1/workloads/semgrep/tasks?wait=true"
    assert "Idempotency-Key" in post["headers"]


@pytest.mark.asyncio
async def test_shadow_serves_fc_invoke_and_mirrors_to_embervm():
    _FakeClient.routes = {
        "/invoke/semgrep": _Resp({"findings": [{"rule_id": "r"}], "errors": []}),
        "/v1/workloads/semgrep/tasks": _Resp(
            {"findings": [{"rule_id": "r"}], "errors": []}
        ),
    }
    monkeypatch_dispatch("shadow")

    served = await client.scan_files(_FILES)
    await _drain()

    # Served result is the fc-invoke one.
    assert len(served["findings"]) == 1
    # Both backends were hit (serving + shadow mirror).
    urls = [p["url"] for p in _FakeClient.posts]
    assert "http://fc/invoke/semgrep" in urls
    assert "http://ev/v1/workloads/semgrep/tasks?wait=true" in urls
    # Equal finding counts => a match, no divergence.
    assert client.shadow_stats["total"] == 1
    assert client.shadow_stats["match"] == 1
    assert client.shadow_stats["diverged"] == 0


@pytest.mark.asyncio
async def test_shadow_tallies_divergence_when_counts_differ():
    _FakeClient.routes = {
        "/invoke/semgrep": _Resp({"findings": [{"rule_id": "r"}], "errors": []}),
        "/v1/workloads/semgrep/tasks": _Resp({"findings": [], "errors": []}),
    }
    monkeypatch_dispatch("shadow")

    await client.scan_files(_FILES)
    await _drain()

    assert client.shadow_stats["diverged"] == 1
    assert client.shadow_stats["match"] == 0


@pytest.mark.asyncio
async def test_shadow_embervm_error_does_not_affect_served_result():
    _FakeClient.routes = {
        "/invoke/semgrep": _Resp({"findings": [{"rule_id": "r"}], "errors": []}),
        "/v1/workloads/semgrep/tasks": _Resp({"error": "boom"}, status=502),
    }
    monkeypatch_dispatch("shadow")

    served = await client.scan_files(_FILES)
    await _drain()

    assert len(served["findings"]) == 1
    assert client.shadow_stats["embervm_error"] == 1


def test_content_key_is_order_independent():
    a = [{"path": "a.py", "content": "x"}, {"path": "b.py", "content": "y"}]
    b = [{"path": "b.py", "content": "y"}, {"path": "a.py", "content": "x"}]
    assert client._content_key(a) == client._content_key(b)
    c = [{"path": "a.py", "content": "z"}, {"path": "b.py", "content": "y"}]
    assert client._content_key(a) != client._content_key(c)


# -- helper: set the module-level dispatch constant (restored by the fixture) --


def monkeypatch_dispatch(mode: str):
    client.SEMGREP_DISPATCH = mode
