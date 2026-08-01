from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from agent_sessions import transport
from faas.embervm_client import EmberVMTransportError


class FakeAsyncClient:
    handler = None

    def __init__(self, **_kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    async def post(self, url, **kwargs):
        request = httpx.Request("POST", url, **kwargs)
        return await self.handler(request)


def _turn_response(request: httpx.Request, status_code: int = 200):
    return httpx.Response(
        status_code,
        json={
            "result": "ok",
            "terminal_reason": "completed",
            "session_id": "cli-2",
        },
        request=request,
    )


def _client(monkeypatch, handler):
    FakeAsyncClient.handler = staticmethod(handler)
    monkeypatch.setattr(transport.httpx, "AsyncClient", FakeAsyncClient)
    monkeypatch.setattr(transport, "EMBERVM_URL", "https://ember.test")
    monkeypatch.setattr(
        transport, "auth_headers", lambda: {"Authorization": "management"}
    )


def test_create_session_parses_cp_session_identity(monkeypatch):
    requests = []

    async def handler(request):
        requests.append(request)
        return httpx.Response(
            201,
            json={
                "session_id": "s1",
                "session_token": "t1",
                "expires_at": 1754035200000,
            },
            request=request,
        )

    _client(monkeypatch, handler)
    result = asyncio.run(transport.EmberVmShimTransport().create_session())

    assert result == transport.EmberSession("s1", "t1", 1754035200000)
    assert requests[0].headers["Authorization"] == "management"


@pytest.mark.parametrize("field", ["session_id", "session_token"])
@pytest.mark.parametrize("value", [None, ""])
def test_create_session_rejects_missing_or_empty_identity(monkeypatch, field, value):
    payload = {"session_id": "s1", "session_token": "t1"}
    payload[field] = value

    async def handler(request):
        return httpx.Response(201, json=payload, request=request)

    _client(monkeypatch, handler)
    with pytest.raises(EmberVMTransportError, match=field):
        asyncio.run(transport.EmberVmShimTransport().create_session())


def test_deliver_uses_ember_identity_and_cli_id_in_body(monkeypatch):
    requests = []

    async def handler(request):
        requests.append(request)
        return _turn_response(request)

    _client(monkeypatch, handler)
    ember = transport.EmberSession("s1", "t1", None)
    turn, used = asyncio.run(
        transport.EmberVmShimTransport().deliver(ember, "cli-1", "hello")
    )

    request = requests[0]
    assert str(request.url) == "https://ember.test/v1/sessions/s1/invoke"
    assert request.headers["Authorization"] == "Bearer t1"
    assert "management" not in request.headers.values()
    assert request.headers["X-Ember-Guest-Path"] == "/shim/turn"
    assert json.loads(request.content) == {"message": "hello", "session_id": "cli-1"}
    assert turn.result == "ok"
    assert used == ember


def test_deliver_recreates_reused_stale_session_once(monkeypatch):
    requests = []
    responses = [410, 200]
    fresh = transport.EmberSession("s2", "t2", 1754035200000)
    create_calls = 0

    async def handler(request):
        requests.append(request)
        return _turn_response(request, responses.pop(0))

    async def create_session():
        nonlocal create_calls
        create_calls += 1
        return fresh

    _client(monkeypatch, handler)
    client = transport.EmberVmShimTransport()
    monkeypatch.setattr(client, "create_session", create_session)
    turn, used = asyncio.run(
        client.deliver(transport.EmberSession("s1", "t1", None), "cli-1", "hello")
    )

    assert len(requests) == 2
    assert json.loads(requests[0].content)["session_id"] == "cli-1"
    assert json.loads(requests[1].content)["session_id"] is None
    assert str(requests[1].url).endswith("/v1/sessions/s2/invoke")
    assert create_calls == 1
    assert turn.result == "ok"
    assert used == fresh


def test_deliver_recreates_reused_session_on_403(monkeypatch):
    requests = []
    responses = [403, 200]
    fresh = transport.EmberSession("s2", "t2", None)
    create_calls = 0

    async def handler(request):
        requests.append(request)
        return _turn_response(request, responses.pop(0))

    async def create_session():
        nonlocal create_calls
        create_calls += 1
        return fresh

    _client(monkeypatch, handler)
    client = transport.EmberVmShimTransport()
    monkeypatch.setattr(client, "create_session", create_session)
    asyncio.run(
        client.deliver(transport.EmberSession("s1", "t1", None), "cli-1", "hello")
    )

    assert len(requests) == 2
    assert str(requests[1].url).endswith("/v1/sessions/s2/invoke")
    assert create_calls == 1


def test_deliver_reused_session_403_with_failing_retry_raises_session_gone(monkeypatch):
    requests = []
    responses = [403, 422]
    fresh = transport.EmberSession("s2", "t2", None)

    async def handler(request):
        requests.append(request)
        return _turn_response(request, responses.pop(0))

    async def create_session():
        return fresh

    _client(monkeypatch, handler)
    client = transport.EmberVmShimTransport()
    monkeypatch.setattr(client, "create_session", create_session)
    with pytest.raises(transport.EmberSessionGone) as exc_info:
        asyncio.run(
            client.deliver(transport.EmberSession("s1", "t1", None), "cli-1", "hello")
        )

    assert len(requests) == 2
    assert isinstance(exc_info.value.__cause__, httpx.HTTPStatusError)


def test_deliver_does_not_retry_reused_session_on_422(monkeypatch):
    requests = []
    create_calls = 0

    async def handler(request):
        requests.append(request)
        return _turn_response(request, 422)

    async def create_session():
        nonlocal create_calls
        create_calls += 1
        return transport.EmberSession("s2", "t2", None)

    _client(monkeypatch, handler)
    client = transport.EmberVmShimTransport()
    monkeypatch.setattr(client, "create_session", create_session)
    with pytest.raises(EmberVMTransportError):
        asyncio.run(
            client.deliver(transport.EmberSession("s1", "t1", None), "cli-1", "hello")
        )

    assert len(requests) == 1
    assert create_calls == 0


def test_deliver_does_not_retry_reused_session_on_404(monkeypatch):
    requests = []
    create_calls = 0

    async def handler(request):
        requests.append(request)
        return _turn_response(request, 404)

    async def create_session():
        nonlocal create_calls
        create_calls += 1
        return transport.EmberSession("s2", "t2", None)

    _client(monkeypatch, handler)
    client = transport.EmberVmShimTransport()
    monkeypatch.setattr(client, "create_session", create_session)
    with pytest.raises(EmberVMTransportError):
        asyncio.run(
            client.deliver(transport.EmberSession("s1", "t1", None), "cli-1", "hello")
        )

    assert len(requests) == 1
    assert create_calls == 0


def test_deliver_does_not_retry_fresh_session_failure(monkeypatch):
    requests = []
    fresh = transport.EmberSession("s1", "t1", None)
    create_calls = 0

    async def handler(request):
        requests.append(request)
        return _turn_response(request, 410)

    async def create_session():
        nonlocal create_calls
        create_calls += 1
        return fresh

    _client(monkeypatch, handler)
    client = transport.EmberVmShimTransport()
    monkeypatch.setattr(client, "create_session", create_session)
    with pytest.raises(EmberVMTransportError):
        asyncio.run(client.deliver(None, "cli-1", "hello"))

    assert len(requests) == 1
    assert create_calls == 1
