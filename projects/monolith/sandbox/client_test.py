"""Tests for the EmberVM sandbox client.

Hermetic: `httpx.AsyncClient` is replaced by a fake that records each POST, so we
assert the EmberVM routing and Idempotency-Key.
"""

from __future__ import annotations

import hashlib
import json
import runpy

import httpx
import pytest

from sandbox import client

_REAL_CLIENT = httpx.AsyncClient


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
    # Captured by executing sandbox/client.py at baseline 4fa27937f before refactor.
    assert (
        first_key == "fa79c8feda49470e43f0ea30b3a2e1b9927dafb6d2219a03b043bcce44c2e912"
    )


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


def _mock_transport(monkeypatch, handler):
    monkeypatch.setattr(httpx, "AsyncClient", _REAL_CLIENT)
    real_init = _REAL_CLIENT.__init__

    def init(self, *args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        real_init(self, *args, **kwargs)

    monkeypatch.setattr(_REAL_CLIENT, "__init__", init)


@pytest.mark.asyncio
async def test_wire_contract_url_timing_and_rotating_auth(monkeypatch, tmp_path):
    token = tmp_path / "token"
    monkeypatch.setenv("K8S_AUTH_TOKEN_FILE", str(token))
    monkeypatch.setenv("EMBERVM_URL", "http://import-time")
    monkeypatch.setenv("SCRATCH_POSTGRES_DSN", "")
    monkeypatch.setenv("SANDBOX_WORKLOAD_PREFIX", "sandbox-")
    module = runpy.run_module("sandbox.client")
    monkeypatch.setenv("EMBERVM_URL", "http://later")
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, json={"exit_code": 0})

    _mock_transport(monkeypatch, handler)
    for value, timeout in (("first", 1), ("rotated", 25), (None, 99)):
        if value is None:
            token.unlink()
        else:
            token.write_text(value)
        await module["_run_embervm"](
            {"code": "same source", "timeout_seconds": timeout}, "python"
        )
    assert [r.headers.get("Authorization") for r in requests] == [
        "Bearer first",
        "Bearer rotated",
        None,
    ]
    assert len({r.headers["Idempotency-Key"] for r in requests}) == 1
    for request in requests:
        assert (
            str(request.url)
            == "http://import-time/v1/workloads/sandbox-python/tasks?wait=true"
        )
        assert request.headers["content-type"] == "application/json"
        assert json.loads(request.content)["code"] == "same source"
        assert request.extensions["timeout"] == {
            "connect": 5,
            "read": 35,
            "write": 35,
            "pool": 35,
        }


@pytest.mark.asyncio
async def test_unconfigured_url_returns_structured_error(monkeypatch):
    monkeypatch.setattr(client, "EMBERVM_URL", "")
    assert await client.run_code_in_sandbox("source") == {
        "error": "EMBERVM_URL is not configured"
    }
    assert _FakeClient.posts == []


@pytest.mark.parametrize(
    ("error", "message"),
    [
        (httpx.ConnectError("refused"), "could not reach embervm: refused"),
        (httpx.ReadTimeout("timeout"), "sandbox execution failed: timeout"),
        (httpx.RemoteProtocolError("broken"), "sandbox execution failed: broken"),
    ],
)
@pytest.mark.asyncio
async def test_transport_failures_are_structured(monkeypatch, error, message):
    def handler(request):
        raise error

    _mock_transport(monkeypatch, handler)
    assert await client.run_code_in_sandbox("source") == {"error": message}


@pytest.mark.asyncio
async def test_http_failure_is_structured_and_truncated(monkeypatch):
    _mock_transport(monkeypatch, lambda request: httpx.Response(503, text="x" * 600))
    assert await client.run_code_in_sandbox("source") == {
        "error": "embervm returned HTTP 503: " + "x" * 500
    }
