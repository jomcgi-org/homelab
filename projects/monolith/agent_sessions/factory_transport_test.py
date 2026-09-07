"""Factory controls are checked at each transport side-effect boundary."""

import asyncio

import httpx
import pytest

from agent_sessions import transport
from faas.embervm_client import EmberVMTransportError


def install_client(monkeypatch, handler):
    class Client:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def post(self, url, **kwargs):
            return await handler(httpx.Request("POST", url, **kwargs))

    monkeypatch.setattr(transport.httpx, "AsyncClient", Client)
    monkeypatch.setattr(transport, "EMBERVM_URL", "https://ember.test")
    monkeypatch.setattr(transport, "auth_headers", lambda: {})


def created(request):
    return httpx.Response(
        201,
        json={"session_id": "guest", "session_token": "test-token"},
        request=request,
    )


def completed(request):
    return httpx.Response(
        200,
        json={"result": "done", "terminal_reason": "completed", "session_id": "cli"},
        request=request,
    )


@pytest.mark.parametrize("restore_from", [None, "prior-lineage"])
@pytest.mark.parametrize(
    "status,body",
    [
        (429, {"reason": "no_capacity", "retryable": True}),
        (500, {"error": "prime_failed", "retryable": True}),
    ],
)
def test_stop_during_create_backoff_prevents_all_later_create_posts(
    monkeypatch, restore_from, status, body
):
    requests = []
    allowed = True

    async def check():
        if not allowed:
            raise EmberVMTransportError("factory stopped")

    async def handler(request):
        requests.append(request)
        return httpx.Response(status, json=body, request=request)

    async def stop_while_waiting(_seconds):
        nonlocal allowed
        allowed = False

    install_client(monkeypatch, handler)
    monkeypatch.setattr(transport.asyncio, "sleep", stop_while_waiting)
    with pytest.raises(EmberVMTransportError, match="factory stopped"):
        asyncio.run(
            transport.EmberVmShimTransport().deliver(
                None, None, "work", restore_from=restore_from, admission_check=check
            )
        )
    assert len(requests) == 1
    assert requests[0].url.path.endswith("/sessions")


def test_denied_resume_does_not_invoke_existing_guest(monkeypatch):
    requests = []

    async def check():
        raise EmberVMTransportError("factory stopped")

    async def handler(request):
        requests.append(request)
        return completed(request)

    install_client(monkeypatch, handler)
    ember = transport.EmberSession("existing", "test-token", None)
    with pytest.raises(EmberVMTransportError, match="factory stopped"):
        asyncio.run(
            transport.EmberVmShimTransport().deliver(
                ember, "existing-cli", "continue", admission_check=check
            )
        )
    assert requests == []


def test_stop_during_invoke_backoff_prevents_retry_post(monkeypatch):
    requests = []
    allowed = True

    async def check():
        if not allowed:
            raise EmberVMTransportError("factory stopped")

    async def handler(request):
        requests.append(request)
        return httpx.Response(503, json={"retryable": True}, request=request)

    async def stop_while_waiting(_seconds):
        nonlocal allowed
        allowed = False

    install_client(monkeypatch, handler)
    monkeypatch.setattr(transport.asyncio, "sleep", stop_while_waiting)
    ember = transport.EmberSession("existing", "test-token", None)
    with pytest.raises(EmberVMTransportError, match="factory stopped"):
        asyncio.run(
            transport.EmberVmShimTransport().deliver(
                ember, "existing-cli", "continue", admission_check=check
            )
        )
    assert len(requests) == 1
    assert requests[0].url.path == "/v1/sessions/existing/invoke"


def test_stop_after_create_blocks_first_invoke(monkeypatch):
    requests = []
    allowed = True

    async def check():
        if not allowed:
            raise EmberVMTransportError("factory stopped")

    async def handler(request):
        nonlocal allowed
        requests.append(request)
        allowed = False
        return created(request)

    install_client(monkeypatch, handler)
    with pytest.raises(EmberVMTransportError, match="factory stopped"):
        asyncio.run(
            transport.EmberVmShimTransport().deliver(
                None, None, "work", admission_check=check
            )
        )
    assert len(requests) == 1
    assert requests[0].url.path.endswith("/sessions")


def test_factory_context_isolated_from_concurrent_unrelated_delivery(monkeypatch):
    requests = []
    shim = transport.EmberVmShimTransport()

    async def scenario():
        factory_waiting = asyncio.Event()
        unrelated_finished = asyncio.Event()
        allowed = True

        async def check():
            if not allowed:
                raise EmberVMTransportError("factory stopped")

        async def handler(request):
            name = asyncio.current_task().get_name()
            requests.append((name, request.url.path))
            if name == "factory":
                return httpx.Response(
                    429,
                    json={"reason": "no_capacity", "retryable": True},
                    request=request,
                )
            if request.url.path.endswith("/sessions"):
                return created(request)
            return completed(request)

        async def stop_and_wait(_seconds):
            nonlocal allowed
            allowed = False
            factory_waiting.set()
            await unrelated_finished.wait()

        async def factory():
            with pytest.raises(EmberVMTransportError, match="factory stopped"):
                await shim.deliver(None, None, "factory", admission_check=check)
            # Same task and transport instance: finally must remove the denied
            # callback before a subsequent direct non-factory create.
            asyncio.current_task().set_name("after-factory")
            assert (await shim.create_session()).session_id == "guest"

        async def unrelated():
            await factory_waiting.wait()
            try:
                turn, _ = await shim.deliver(None, None, "unrelated")
                assert turn.terminal_reason == "completed"
            finally:
                unrelated_finished.set()

        install_client(monkeypatch, handler)
        monkeypatch.setattr(transport.asyncio, "sleep", stop_and_wait)
        await asyncio.gather(
            asyncio.create_task(factory(), name="factory"),
            asyncio.create_task(unrelated(), name="unrelated"),
        )

    asyncio.run(scenario())
    assert len([path for name, path in requests if name == "factory"]) == 1
    assert len([path for name, path in requests if name == "unrelated"]) == 2
    assert len([path for name, path in requests if name == "after-factory"]) == 1


def test_cancellation_resets_callback_for_subsequent_direct_create(monkeypatch):
    requests = []
    shim = transport.EmberVmShimTransport()

    async def check():
        raise asyncio.CancelledError()

    async def handler(request):
        requests.append(request)
        return created(request)

    async def scenario():
        with pytest.raises(asyncio.CancelledError):
            await shim.deliver(None, None, "work", admission_check=check)
        assert (await shim.create_session()).session_id == "guest"

    install_client(monkeypatch, handler)
    asyncio.run(scenario())
    assert len(requests) == 1


def test_in_flight_timeout_keeps_transport_uncertainty_after_stop(monkeypatch):
    requests = []
    checks = 0
    allowed = True

    async def check():
        nonlocal checks
        checks += 1
        if not allowed:
            raise EmberVMTransportError("factory stopped")

    async def handler(request):
        nonlocal allowed
        requests.append(request)
        allowed = False
        raise httpx.ReadTimeout("response unknown", request=request)

    install_client(monkeypatch, handler)
    ember = transport.EmberSession("existing", "test-token", None)
    with pytest.raises(transport.EmberVMTimeout, match="response unknown"):
        asyncio.run(
            transport.EmberVmShimTransport().deliver(
                ember, "existing-cli", "continue", admission_check=check
            )
        )
    assert len(requests) == 1
    assert checks == 1


def test_nested_unrelated_delivery_temporarily_clears_factory_check(monkeypatch):
    requests = []
    allowed = True
    shim = transport.EmberVmShimTransport()

    async def check():
        if not allowed:
            raise EmberVMTransportError("factory stopped")

    async def handler(request):
        requests.append(request)
        return (
            created(request)
            if request.url.path.endswith("/sessions")
            else completed(request)
        )

    async def after_create(_ember, _cli):
        nonlocal allowed
        allowed = False
        turn, _ = await shim.deliver(None, None, "unrelated")
        assert turn.terminal_reason == "completed"

    install_client(monkeypatch, handler)
    with pytest.raises(EmberVMTransportError, match="factory stopped"):
        asyncio.run(
            shim.deliver(
                None, None, "factory", admission_check=check, on_create=after_create
            )
        )
    assert len(requests) == 3
    assert sum(request.url.path.endswith("/invoke") for request in requests) == 1
