"""Hermetic tests for the policy-free synchronous submit transport."""

import httpx
import pytest

from shared.embervm_submit import submit_task


@pytest.mark.parametrize(
    "payload",
    [
        {"content": b"\x00raw\xff"},
        {"json": {"code": "42"}},
        {"content": b""},
        {"json": None},
    ],
)
@pytest.mark.asyncio
async def test_submit_preserves_request_and_raw_response(monkeypatch, payload):
    requests = []
    response = httpx.Response(503, content=b"guest failure")

    def handler(request):
        requests.append(request)
        return response

    real_init = httpx.AsyncClient.__init__

    def init(self, *args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        real_init(self, *args, **kwargs)

    monkeypatch.setattr(httpx.AsyncClient, "__init__", init)
    headers = {"Authorization": "Bearer caller", "X-Optional": "unchanged"}
    result = await submit_task(
        base_url="http://ev",
        workload="test-workload",
        timeout=httpx.Timeout(connect=1, read=2, write=3, pool=4),
        headers=headers,
        **payload,
    )
    assert result is response
    assert len(requests) == 1
    request = requests[0]
    assert request.method == "POST"
    assert request.url.path == "/v1/workloads/test-workload/tasks"
    assert request.url.query == b"wait=true"
    assert request.extensions["timeout"] == {
        "connect": 1,
        "read": 2,
        "write": 3,
        "pool": 4,
    }
    for key, value in headers.items():
        assert request.headers[key] == value
    assert headers == {"Authorization": "Bearer caller", "X-Optional": "unchanged"}
    if "content" in payload:
        assert request.content == payload["content"]
        assert "content-type" not in request.headers
    elif payload["json"] is not None:
        assert request.content == b'{"code":"42"}'
        assert request.headers["content-type"] == "application/json"
    else:
        # Preserve httpx's explicit json=None encoding.
        assert request.content == b""


@pytest.mark.parametrize(
    "payload", [{}, {"content": b"raw", "json": {}}, {"content": b"", "json": None}]
)
@pytest.mark.asyncio
async def test_exactly_one_payload_is_required(monkeypatch, payload):
    def unexpected_client(*args, **kwargs):
        pytest.fail("invalid payload must fail before creating a client")

    monkeypatch.setattr(httpx, "AsyncClient", unexpected_client)
    with pytest.raises(ValueError, match="exactly one"):
        await submit_task(
            base_url="http://ev",
            workload="test",
            timeout=httpx.Timeout(5),
            headers={},
            **payload,
        )


@pytest.mark.parametrize(
    "error", [httpx.ConnectError("refused"), httpx.ReadTimeout("timeout")]
)
@pytest.mark.asyncio
async def test_transport_errors_are_not_translated(monkeypatch, error):
    def handler(request):
        raise error

    real_init = httpx.AsyncClient.__init__

    def init(self, *args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        real_init(self, *args, **kwargs)

    monkeypatch.setattr(httpx.AsyncClient, "__init__", init)
    with pytest.raises(type(error)) as caught:
        await submit_task(
            base_url="http://ev",
            workload="test",
            timeout=httpx.Timeout(5),
            headers={},
            content=b"",
        )
    assert caught.value is error
