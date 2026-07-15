"""Tests for the EmberVM submit client (faas.embervm_client).

httpx.MockTransport captures the outgoing request so we can assert the guest
path header, the X-Ember-Guest-* header mapping, and that the body is forwarded
verbatim (content=, not json=). A transport error maps to EmberVMTransportError.
"""

from __future__ import annotations

import httpx
import pytest

from faas import embervm_client


@pytest.fixture(autouse=True)
def _url(monkeypatch):
    monkeypatch.setattr(embervm_client, "EMBERVM_URL", "http://embervm:8080")


def _mock_client(monkeypatch, handler):
    """Route the module's AsyncClient through a MockTransport running ``handler``."""
    real_init = httpx.AsyncClient.__init__

    def _init(self, *args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        real_init(self, *args, **kwargs)

    monkeypatch.setattr(httpx.AsyncClient, "__init__", _init)


@pytest.mark.asyncio
async def test_submit_sets_guest_path_and_forwards_body_verbatim(monkeypatch):
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["path_header"] = request.headers.get("X-Ember-Guest-Path")
        captured["ct_header"] = request.headers.get("X-Ember-Guest-Content-Type")
        captured["body"] = request.content
        return httpx.Response(200, text="ok")

    _mock_client(monkeypatch, handler)

    resp = await embervm_client.submit(
        "echo-fn",
        body=b'{"a":1}',
        guest_path="/invoke",
        extra_guest_headers={"Content-Type": "application/json"},
        read_timeout=5.0,
    )

    assert resp.status_code == 200
    assert captured["url"] == "http://embervm:8080/v1/workloads/echo-fn/tasks?wait=true"
    assert captured["path_header"] == "/invoke"
    assert captured["ct_header"] == "application/json"
    assert captured["body"] == b'{"a":1}'  # verbatim, not re-serialized JSON


@pytest.mark.asyncio
async def test_submit_returns_non_2xx_response_not_raise(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="ImportError")

    _mock_client(monkeypatch, handler)
    resp = await embervm_client.submit(
        "echo-fn",
        body=b"{}",
        guest_path="/invoke",
        extra_guest_headers=None,
        read_timeout=5.0,
    )
    assert resp.status_code == 500
    assert resp.text == "ImportError"


@pytest.mark.asyncio
async def test_submit_connect_error_raises_transport_error(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    _mock_client(monkeypatch, handler)
    with pytest.raises(embervm_client.EmberVMTransportError):
        await embervm_client.submit(
            "echo-fn",
            body=b"{}",
            guest_path="/invoke",
            extra_guest_headers=None,
            read_timeout=5.0,
        )


@pytest.mark.asyncio
async def test_submit_unconfigured_url_raises(monkeypatch):
    monkeypatch.setattr(embervm_client, "EMBERVM_URL", "")
    with pytest.raises(embervm_client.EmberVMTransportError):
        await embervm_client.submit(
            "echo-fn",
            body=b"{}",
            guest_path="/invoke",
            extra_guest_headers=None,
            read_timeout=5.0,
        )
