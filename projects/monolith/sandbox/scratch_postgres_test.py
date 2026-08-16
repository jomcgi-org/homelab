"""Tests for the EmberVM R4 scratch-postgres DSN plumbing (client.py).

Hermetic: no network. Asserts that when SCRATCH_POSTGRES_DSN is set, the DSN is
injected into submitted Python code (so a guest snippet can psycopg-
connect), the value is repr-escaped, and that an unset DSN leaves the code
unchanged. Non-Python code must remain byte-identical. Also asserts the injected
DSN reaches the POSTed payload end to end via the same fake-client seam
client_test uses.
"""

from __future__ import annotations

import httpx
import pytest

from sandbox import client


class _Resp:
    status_code = 200
    text = ""

    def raise_for_status(self):
        return None

    def json(self):
        return {"stdout": "", "exit_code": 0}


class _FakeClient:
    posts: list[dict] = []

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def post(self, url, json=None, headers=None):
        _FakeClient.posts.append({"url": url, "json": json})
        return _Resp()


def test_with_scratch_env_unset_returns_code_unchanged(monkeypatch):
    monkeypatch.setattr(client, "SCRATCH_POSTGRES_DSN", "")
    code = "print('hi')"
    assert client._with_scratch_env(code) == code


def test_with_scratch_env_injects_env_assignment(monkeypatch):
    dsn = "postgresql://postgres:pw@embervm-serving.embervm.svc:5400/scratch"
    monkeypatch.setattr(client, "SCRATCH_POSTGRES_DSN", dsn)
    out = client._with_scratch_env("print('hi')")
    # The user code is preserved and the DSN is set in the guest process env
    # before it runs.
    assert out.endswith("print('hi')")
    assert "os.environ['SCRATCH_POSTGRES_DSN']" in out.replace("_os", "os")
    assert dsn in out


def test_with_scratch_env_repr_escapes_special_chars(monkeypatch):
    # A password with a quote/backslash must not break out of the literal.
    dsn = "postgresql://postgres:p'w\\x@host:5400/scratch"
    monkeypatch.setattr(client, "SCRATCH_POSTGRES_DSN", dsn)
    out = client._with_scratch_env("pass")
    # repr() escaping means the raw quote is not left unescaped in the assignment.
    assert repr(dsn) in out


def test_with_scratch_env_leaves_rust_byte_identical(monkeypatch):
    monkeypatch.setattr(client, "SCRATCH_POSTGRES_DSN", "postgresql://secret")
    code = 'fn main() { println!("hi"); }\n'

    assert client._with_scratch_env(code, language="rust") == code


@pytest.mark.asyncio
async def test_dsn_reaches_posted_payload(monkeypatch):
    _FakeClient.posts = []
    dsn = "postgresql://postgres:pw@embervm-serving.embervm.svc:5400/scratch"
    monkeypatch.setattr(client, "SCRATCH_POSTGRES_DSN", dsn)
    monkeypatch.setattr(client.httpx, "AsyncClient", _FakeClient)
    monkeypatch.setattr(client, "EMBERVM_URL", "http://embervm")

    await client.run_code_in_sandbox("print('hi')")

    assert _FakeClient.posts, "expected a POST"
    posted_code = _FakeClient.posts[0]["json"]["code"]
    assert dsn in posted_code
    assert posted_code.endswith("print('hi')")


@pytest.mark.asyncio
async def test_no_dsn_leaves_payload_code_untouched(monkeypatch):
    _FakeClient.posts = []
    monkeypatch.setattr(client, "SCRATCH_POSTGRES_DSN", "")
    monkeypatch.setattr(client.httpx, "AsyncClient", _FakeClient)
    monkeypatch.setattr(client, "EMBERVM_URL", "http://embervm")

    await client.run_code_in_sandbox("print('hi')")

    posted_code = _FakeClient.posts[0]["json"]["code"]
    assert posted_code == "print('hi')"


@pytest.mark.asyncio
async def test_rust_payload_is_byte_identical_when_dsn_is_set(monkeypatch):
    _FakeClient.posts = []
    monkeypatch.setattr(client, "SCRATCH_POSTGRES_DSN", "postgresql://secret")
    monkeypatch.setattr(client.httpx, "AsyncClient", _FakeClient)
    monkeypatch.setattr(client, "EMBERVM_URL", "http://embervm")
    code = 'fn main() { println!("hi"); }\n'

    await client.run_code_in_sandbox(code, language="rust")

    assert _FakeClient.posts[0]["json"]["code"] == code


# httpx is imported so the fake-client seam matches client_test's dependency set.
_ = httpx
