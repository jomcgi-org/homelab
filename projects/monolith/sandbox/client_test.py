"""Tests for the EmberVM sandbox client.

Hermetic: `httpx.AsyncClient` is replaced by a fake that records each POST, so we
assert the EmberVM routing and Idempotency-Key.
"""

from __future__ import annotations

import hashlib
import json

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
    assert post["json"]["timeout_seconds"] == 25


@pytest.mark.asyncio
async def test_guest_timeout_response_is_returned_verbatim(monkeypatch):
    response = {
        "exit_code": -1,
        "error": "timed out after 25s",
        "stdout": "partial output\n",
        "stderr": "partial diagnostics\n",
        "duration_ms": 25000,
        "truncated": False,
        "files": [{"path": "partial.txt", "content_b64": "cGFydGlhbA=="}],
    }

    async def timeout_post(self, url, json=None, headers=None):
        return _Resp(response)

    monkeypatch.setattr(_FakeClient, "post", timeout_post)

    result = await client.run_code_in_sandbox("source")

    assert result is response


@pytest.mark.parametrize("language", client.SUPPORTED_LANGUAGES)
@pytest.mark.parametrize(
    "files",
    [None, [{"path": "input.txt", "content_b64": "aW5wdXQ="}]],
)
@pytest.mark.asyncio
async def test_guest_timeout_does_not_change_idempotency_key(language, files):
    code = "same source"
    file_parts = sorted(
        json.dumps(item, sort_keys=True, ensure_ascii=True) for item in (files or [])
    )
    original_material = "\0".join([language, code, *file_parts]).encode()
    expected_key = hashlib.sha256(original_material).hexdigest()

    await client.run_code_in_sandbox(code, language=language, files=files)

    assert _FakeClient.posts[0]["headers"]["Idempotency-Key"] == expected_key


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
async def test_idempotency_key_includes_files():
    await client.run_code_in_sandbox(
        "same source", files=[{"name": "input.txt", "content": "first"}]
    )
    await client.run_code_in_sandbox(
        "same source", files=[{"name": "input.txt", "content": "second"}]
    )

    first_key = _FakeClient.posts[0]["headers"]["Idempotency-Key"]
    second_key = _FakeClient.posts[1]["headers"]["Idempotency-Key"]
    assert first_key != second_key


@pytest.mark.asyncio
async def test_idempotency_key_canonicalizes_file_order():
    files = [
        {"name": "first.txt", "content": "first"},
        {"name": "second.txt", "content": "second"},
    ]
    await client.run_code_in_sandbox("same source", files=files)
    await client.run_code_in_sandbox("same source", files=list(reversed(files)))

    first_key = _FakeClient.posts[0]["headers"]["Idempotency-Key"]
    second_key = _FakeClient.posts[1]["headers"]["Idempotency-Key"]
    assert first_key == second_key


@pytest.mark.asyncio
async def test_idempotency_key_differs_with_and_without_files():
    await client.run_code_in_sandbox("same source")
    await client.run_code_in_sandbox("same source")
    await client.run_code_in_sandbox(
        "same source", files=[{"name": "input.txt", "content": "contents"}]
    )

    first_key = _FakeClient.posts[0]["headers"]["Idempotency-Key"]
    second_key = _FakeClient.posts[1]["headers"]["Idempotency-Key"]
    files_key = _FakeClient.posts[2]["headers"]["Idempotency-Key"]
    assert first_key == second_key
    assert first_key != files_key


@pytest.mark.asyncio
async def test_empty_code_short_circuits():
    result = await client.run_code_in_sandbox("   ")
    assert "error" in result
    assert _FakeClient.posts == []
