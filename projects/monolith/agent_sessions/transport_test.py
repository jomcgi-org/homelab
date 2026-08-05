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


def _error_response(request: httpx.Request, status_code: int, retryable: bool):
    return httpx.Response(
        status_code,
        json={"error": "error message", "retryable": retryable},
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


def test_create_session_retryable_backoff_and_restore_payload(monkeypatch):
    attempts = []
    sleeps = []

    async def handler(request):
        attempts.append(request)
        if len(attempts) < 4:
            return _error_response(request, 409, True)
        return httpx.Response(
            200,
            json={"session_id": "s1", "session_token": "t1"},
            request=request,
        )

    async def fake_sleep(seconds):
        sleeps.append(seconds)

    _client(monkeypatch, handler)
    monkeypatch.setattr(transport.asyncio, "sleep", fake_sleep)
    result = asyncio.run(
        transport.EmberVmShimTransport().create_session(restore_from="lineage-1")
    )

    assert len(attempts) == 4
    assert sleeps == [2, 5, 10]
    assert [json.loads(request.content) for request in attempts] == [
        {"restore_lineage": "lineage-1"}
    ] * 4
    assert result.session_id == "s1"


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


def test_create_session_parses_lineage_and_restored(monkeypatch):
    async def handler(request):
        return httpx.Response(
            201,
            json={
                "session_id": "s2",
                "session_token": "t2",
                "expires_at": 1754035200000,
                "lineage_id": "s1",
                "restored": True,
            },
            request=request,
        )

    _client(monkeypatch, handler)
    result = asyncio.run(
        transport.EmberVmShimTransport().create_session(restore_from="s1")
    )

    assert result.lineage_id == "s1"
    assert result.restored is True


def test_create_session_normal_posts_no_body(monkeypatch):
    requests = []

    async def handler(request):
        requests.append(request)
        return httpx.Response(
            201,
            json={"session_id": "s1", "session_token": "t1"},
            request=request,
        )

    _client(monkeypatch, handler)
    result = asyncio.run(transport.EmberVmShimTransport().create_session())

    assert not requests[0].content
    # Absent lineage_id/restored in the CP response degrades to None/False.
    assert result.lineage_id is None
    assert result.restored is False


def test_create_session_restoring_posts_restore_lineage_body(monkeypatch):
    requests = []

    async def handler(request):
        requests.append(request)
        return httpx.Response(
            201,
            json={"session_id": "s2", "session_token": "t2", "lineage_id": "s1"},
            request=request,
        )

    _client(monkeypatch, handler)
    asyncio.run(transport.EmberVmShimTransport().create_session(restore_from="s1"))

    assert json.loads(requests[0].content) == {"restore_lineage": "s1"}


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


def test_deliver_includes_model_when_present(monkeypatch):
    requests = []

    async def handler(request):
        requests.append(request)
        return _turn_response(request)

    _client(monkeypatch, handler)
    asyncio.run(
        transport.EmberVmShimTransport().deliver(
            transport.EmberSession("s1", "t1", None), "cli-1", "hello", "fable"
        )
    )
    assert json.loads(requests[0].content) == {
        "message": "hello",
        "session_id": "cli-1",
        "model": "fable",
    }


def test_deliver_includes_progress_token_when_present(monkeypatch):
    requests = []

    async def handler(request):
        requests.append(request)
        return _turn_response(request)

    _client(monkeypatch, handler)
    asyncio.run(
        transport.EmberVmShimTransport().deliver(
            transport.EmberSession("s1", "t1", None),
            "cli-1",
            "hello",
            progress_token="progress-1",
        )
    )
    assert json.loads(requests[0].content)["progress_token"] == "progress-1"


def test_deliver_omits_progress_token_when_none(monkeypatch):
    requests = []

    async def handler(request):
        requests.append(request)
        return _turn_response(request)

    _client(monkeypatch, handler)
    asyncio.run(
        transport.EmberVmShimTransport().deliver(
            transport.EmberSession("s1", "t1", None), "cli-1", "hello"
        )
    )
    assert "progress_token" not in json.loads(requests[0].content)


def test_invoke_retryable_502_is_retried_and_succeeds(monkeypatch):
    requests = []
    sleeps = []

    async def handler(request):
        requests.append(request)
        if len(requests) == 1:
            return _error_response(request, 502, True)
        return _turn_response(request)

    async def fake_sleep(seconds):
        sleeps.append(seconds)

    _client(monkeypatch, handler)
    monkeypatch.setattr(transport.asyncio, "sleep", fake_sleep)
    turn, _ = asyncio.run(
        transport.EmberVmShimTransport().deliver(
            transport.EmberSession("s1", "t1", None), "cli-1", "hello"
        )
    )

    assert len(requests) == 2
    assert sleeps == [2]
    assert turn.result == "ok"
    assert not turn.is_error


def test_invoke_retries_exhaust_after_max_attempts(monkeypatch):
    requests = []
    sleeps = []

    async def handler(request):
        requests.append(request)
        return _error_response(request, 502, True)

    async def fake_sleep(seconds):
        sleeps.append(seconds)

    _client(monkeypatch, handler)
    monkeypatch.setattr(transport.asyncio, "sleep", fake_sleep)
    with pytest.raises(EmberVMTransportError) as exc_info:
        asyncio.run(
            transport.EmberVmShimTransport().deliver(
                transport.EmberSession("s1", "t1", None), "cli-1", "hello"
            )
        )

    assert len(requests) == 8
    assert sleeps == [2, 5, 10, 20, 30, 30, 30]
    assert "502" in str(exc_info.value)
    assert "error message" in str(exc_info.value)
    assert "retryable" in str(exc_info.value)


def test_invoke_non_retryable_502_is_not_retried(monkeypatch):
    requests = []

    async def handler(request):
        requests.append(request)
        return _error_response(request, 502, False)

    _client(monkeypatch, handler)
    with pytest.raises(EmberVMTransportError) as exc_info:
        asyncio.run(
            transport.EmberVmShimTransport().deliver(
                transport.EmberSession("s1", "t1", None), "cli-1", "hello"
            )
        )

    assert len(requests) == 1
    assert "502" in str(exc_info.value)


def test_no_double_execution_on_retry(monkeypatch):
    requests = []
    sleeps = []

    async def handler(request):
        requests.append(request)
        if len(requests) == 1:
            return _error_response(request, 502, True)
        return httpx.Response(
            200,
            json={
                "result": "single execution",
                "terminal_reason": "completed",
                "num_turns": 1,
                "session_id": "cli-1",
            },
            request=request,
        )

    async def fake_sleep(seconds):
        sleeps.append(seconds)

    _client(monkeypatch, handler)
    monkeypatch.setattr(transport.asyncio, "sleep", fake_sleep)
    turn, _ = asyncio.run(
        transport.EmberVmShimTransport().deliver(
            transport.EmberSession("s1", "t1", None), "cli-1", "hello"
        )
    )

    assert len(requests) == 2
    assert sleeps == [2]
    assert requests[0].content == requests[1].content
    assert turn.result == "single execution"
    assert turn.num_turns == 1


def test_deliver_includes_repo_and_branch_when_present(monkeypatch):
    requests = []

    async def handler(request):
        requests.append(request)
        return _turn_response(request)

    _client(monkeypatch, handler)
    asyncio.run(
        transport.EmberVmShimTransport().deliver(
            transport.EmberSession("s1", "t1", None),
            "cli-1",
            "hello",
            repo="jomcgi/homelab",
            branch="develop",
        )
    )
    assert json.loads(requests[0].content)["repo"] == "jomcgi/homelab"
    assert json.loads(requests[0].content)["branch"] == "develop"


def test_deliver_defaults_repo_branch_and_omits_repo_without_repo(monkeypatch):
    requests = []

    async def handler(request):
        requests.append(request)
        return _turn_response(request)

    _client(monkeypatch, handler)
    client = transport.EmberVmShimTransport()
    asyncio.run(
        client.deliver(
            transport.EmberSession("s1", "t1", None),
            "cli-1",
            "hello",
            repo="jomcgi/homelab",
            branch=None,
        )
    )
    assert json.loads(requests[0].content)["branch"] == "main"
    requests.clear()
    asyncio.run(
        client.deliver(
            transport.EmberSession("s1", "t1", None), "cli-1", "hello", branch="dev"
        )
    )
    assert "repo" not in json.loads(requests[0].content)
    assert "branch" not in json.loads(requests[0].content)


def test_deliver_recreates_reused_stale_session_once(monkeypatch):
    requests = []
    responses = [410, 200]
    # restored defaults False: this fresh session is a BLANK create (no
    # workspace recovered), so the retry invoke must drop the CLI id below.
    fresh = transport.EmberSession("s2", "t2", 1754035200000)
    create_calls = 0
    seen_restore_from = []

    async def handler(request):
        requests.append(request)
        return _turn_response(request, responses.pop(0))

    async def create_session(restore_from=None):
        nonlocal create_calls
        create_calls += 1
        seen_restore_from.append(restore_from)
        return fresh

    _client(monkeypatch, handler)
    client = transport.EmberVmShimTransport()
    monkeypatch.setattr(client, "create_session", create_session)
    turn, used = asyncio.run(
        client.deliver(transport.EmberSession("s1", "t1", None), "cli-1", "hello")
    )

    assert len(requests) == 2
    assert json.loads(requests[0].content)["session_id"] == "cli-1"
    # restored=False on the retry create means no workspace was recovered,
    # so the CLI transcript id must NOT be resumed against a blank session.
    assert json.loads(requests[1].content)["session_id"] is None
    assert str(requests[1].url).endswith("/v1/sessions/s2/invoke")
    assert create_calls == 1
    # #4306 slice 4: the expiring session's lineage_id was never set (a
    # pre-lineage binding, EmberSession("s1", "t1", None) defaults
    # lineage_id=None), so the fallback is its session_id.
    assert seen_restore_from == ["s1"]
    assert turn.result == "ok"
    assert used == fresh


def test_deliver_recreates_reused_session_on_403(monkeypatch):
    requests = []
    responses = [403, 200]
    fresh = transport.EmberSession("s2", "t2", None)
    create_calls = 0
    seen_restore_from = []

    async def handler(request):
        requests.append(request)
        return _turn_response(request, responses.pop(0))

    async def create_session(restore_from=None):
        nonlocal create_calls
        create_calls += 1
        seen_restore_from.append(restore_from)
        return fresh

    _client(monkeypatch, handler)
    client = transport.EmberVmShimTransport()
    monkeypatch.setattr(client, "create_session", create_session)
    asyncio.run(
        client.deliver(
            transport.EmberSession("s1", "t1", None, lineage_id="lineage-1"),
            "cli-1",
            "hello",
        )
    )

    assert len(requests) == 2
    assert str(requests[1].url).endswith("/v1/sessions/s2/invoke")
    assert create_calls == 1
    # The expiring session DID carry a lineage_id: that, not session_id, is
    # what must be sent as restore_from.
    assert seen_restore_from == ["lineage-1"]


def test_deliver_keeps_cli_session_id_when_restore_recovers_workspace(monkeypatch):
    """#4306 slice 4: a restore that actually recovers the workspace
    (restored=True) must let the retry --resume the CLI transcript; only a
    BLANK fallback drops the CLI id (covered above, restored defaults False)."""
    requests = []
    responses = [410, 200]
    restored_session = transport.EmberSession(
        "s2", "t2", None, lineage_id="lineage-1", restored=True
    )

    async def handler(request):
        requests.append(request)
        return _turn_response(request, responses.pop(0))

    async def create_session(restore_from=None):
        return restored_session

    _client(monkeypatch, handler)
    client = transport.EmberVmShimTransport()
    monkeypatch.setattr(client, "create_session", create_session)
    turn, used = asyncio.run(
        client.deliver(
            transport.EmberSession("s1", "t1", None, lineage_id="lineage-1"),
            "cli-1",
            "hello",
        )
    )

    assert len(requests) == 2
    # restored=True: the retry resumes the ORIGINAL cli_session_id.
    assert json.loads(requests[1].content)["session_id"] == "cli-1"
    assert used == restored_session
    assert turn.result == "ok"


def test_deliver_falls_back_to_blank_session_when_restore_create_is_denied(
    monkeypatch,
):
    """#4306 slice 4: the restore create itself can be DENIED (unknown_lineage,
    a mismatch, a live heir, or an in-flight restore of the same lineage). This
    must degrade to a blank session and still complete the turn, never raise
    EmberSessionGone (the guest workspace being unrecoverable is not the same
    as the ORIGINAL binding being confirmed dead)."""
    requests = []
    responses = [410, 200]
    blank = transport.EmberSession("s3", "t3", None)
    create_calls = []

    async def handler(request):
        requests.append(request)
        return _turn_response(request, responses.pop(0))

    async def create_session(restore_from=None):
        create_calls.append(restore_from)
        if restore_from:
            raise EmberVMTransportError("404 unknown_lineage")
        return blank

    _client(monkeypatch, handler)
    client = transport.EmberVmShimTransport()
    monkeypatch.setattr(client, "create_session", create_session)
    turn, used = asyncio.run(
        client.deliver(
            transport.EmberSession("s1", "t1", None, lineage_id="lineage-1"),
            "cli-1",
            "hello",
        )
    )

    # First call attempted the restore and was denied; second call is the
    # blank fallback (no restore_from).
    assert create_calls == ["lineage-1", None]
    assert len(requests) == 2
    # A blank session (restored=False) must not carry the CLI id forward.
    assert json.loads(requests[1].content)["session_id"] is None
    assert used == blank
    assert turn.result == "ok"


def test_deliver_reused_session_403_with_failing_retry_raises_session_gone(monkeypatch):
    requests = []
    responses = [403, 422]
    fresh = transport.EmberSession("s2", "t2", None)

    async def handler(request):
        requests.append(request)
        return _turn_response(request, responses.pop(0))

    async def create_session(restore_from=None):
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


def test_deliver_fresh_session_retryable_then_fails(monkeypatch):
    attempts = []
    sleeps = []

    async def handler(request):
        attempts.append(request)
        if len(attempts) < 4:
            return _error_response(request, 409, True)
        return _turn_response(request)

    async def fake_sleep(seconds):
        sleeps.append(seconds)

    _client(monkeypatch, handler)
    monkeypatch.setattr(transport.asyncio, "sleep", fake_sleep)
    client = transport.EmberVmShimTransport()
    fresh = transport.EmberSession("s1", "t1", None)

    async def create_session(restore_from=None):
        return fresh

    monkeypatch.setattr(client, "create_session", create_session)
    turn, _ = asyncio.run(client.deliver(None, "cli-1", "hello"))

    assert turn.result == "ok"
    assert len(attempts) == 4
    assert sleeps == [2, 5, 10]


def test_deliver_retryable_exhaustion(monkeypatch):
    attempts = []
    sleeps = []

    async def handler(request):
        attempts.append(request)
        return _error_response(request, 429, True)

    async def fake_sleep(seconds):
        sleeps.append(seconds)

    async def create_session(restore_from=None):
        return transport.EmberSession("s1", "t1", None)

    _client(monkeypatch, handler)
    monkeypatch.setattr(transport.asyncio, "sleep", fake_sleep)
    client = transport.EmberVmShimTransport()
    monkeypatch.setattr(client, "create_session", create_session)
    with pytest.raises(EmberVMTransportError):
        asyncio.run(client.deliver(None, "cli-1", "hello"))

    assert len(attempts) == 8
    assert sleeps == [2, 5, 10, 20, 30, 30, 30]


def test_deliver_410_restore_heir_persisted_on_double_failure(monkeypatch):
    events = []
    responses = [410, 410]
    heir = transport.EmberSession("heir", "token", None)

    async def handler(request):
        events.append("invoke")
        return _error_response(request, responses.pop(0), False)

    async def create_session(restore_from=None):
        events.append("create")
        return heir

    async def on_create(ember, cli_for_binding):
        events.append(("persist", ember.session_id, cli_for_binding))

    _client(monkeypatch, handler)
    client = transport.EmberVmShimTransport()
    monkeypatch.setattr(client, "create_session", create_session)
    with pytest.raises(transport.EmberSessionGone):
        asyncio.run(
            client.deliver(
                transport.EmberSession("old", "old-token", None),
                "cli-1",
                "hello",
                on_create=on_create,
            )
        )

    assert events == ["invoke", "create", ("persist", "heir", None), "invoke"]


def test_deliver_workspace_recovery_on_normal_create(monkeypatch):
    async def handler(request):
        return _turn_response(request)

    async def create_session(restore_from=None):
        return transport.EmberSession("s1", "t1", None)

    _client(monkeypatch, handler)
    client = transport.EmberVmShimTransport()
    monkeypatch.setattr(client, "create_session", create_session)
    turn, _ = asyncio.run(client.deliver(None, None, "hello"))

    assert turn.workspace_recovery == {
        "created": True,
        "restored": False,
        "degraded": None,
    }


def test_deliver_workspace_recovery_on_restore_success(monkeypatch):
    async def handler(request):
        return _turn_response(request)

    async def create_session(restore_from=None):
        return transport.EmberSession(
            "s2", "t2", None, lineage_id="lineage-1", restored=True
        )

    _client(monkeypatch, handler)
    client = transport.EmberVmShimTransport()
    monkeypatch.setattr(client, "create_session", create_session)
    turn, _ = asyncio.run(
        client.deliver(None, "cli-1", "hello", restore_from="lineage-1")
    )

    assert turn.workspace_recovery == {
        "created": True,
        "restored": True,
        "degraded": None,
    }


def test_deliver_workspace_recovery_on_restore_denial_fallback(monkeypatch):
    async def handler(request):
        return _turn_response(request)

    async def create_session(restore_from=None):
        if restore_from:
            raise EmberVMTransportError("restore denied")
        return transport.EmberSession("s3", "t3", None)

    _client(monkeypatch, handler)
    client = transport.EmberVmShimTransport()
    monkeypatch.setattr(client, "create_session", create_session)
    turn, _ = asyncio.run(
        client.deliver(None, "cli-1", "hello", restore_from="lineage-1")
    )

    assert turn.workspace_recovery == {
        "created": True,
        "restored": False,
        "degraded": "restore_denied",
    }


def test_deliver_workspace_recovery_absent_on_reuse(monkeypatch):
    async def handler(request):
        return _turn_response(request)

    _client(monkeypatch, handler)
    ember = transport.EmberSession("s1", "t1", None)
    turn, _ = asyncio.run(
        transport.EmberVmShimTransport().deliver(ember, None, "hello")
    )

    assert turn.workspace_recovery is None


# -- #4306 slice 5: deliver(ember=None, restore_from=...) -------------------
# The binding-was-cleared path: no ember to reuse, but a PRIOR lineage
# survived clear_ember_session/clear_ember_bindings_by_ember_id.


def test_deliver_with_no_ember_restores_and_keeps_cli_when_recovered(monkeypatch):
    requests = []
    restored_session = transport.EmberSession(
        "s2", "t2", None, lineage_id="lineage-1", restored=True
    )
    create_calls = []

    async def handler(request):
        requests.append(request)
        return _turn_response(request)

    async def create_session(restore_from=None):
        create_calls.append(restore_from)
        return restored_session

    _client(monkeypatch, handler)
    client = transport.EmberVmShimTransport()
    monkeypatch.setattr(client, "create_session", create_session)
    turn, used = asyncio.run(
        client.deliver(None, "cli-prior", "hello", restore_from="lineage-1")
    )

    assert create_calls == ["lineage-1"]
    assert used == restored_session
    # restored=True: the prior CLI transcript is resumed.
    assert json.loads(requests[0].content)["session_id"] == "cli-prior"
    assert turn.result == "ok"


def test_deliver_with_no_ember_restore_denied_falls_back_to_blank(monkeypatch):
    requests = []
    blank = transport.EmberSession("s3", "t3", None)
    create_calls = []

    async def handler(request):
        requests.append(request)
        return _turn_response(request)

    async def create_session(restore_from=None):
        create_calls.append(restore_from)
        if restore_from:
            raise EmberVMTransportError("404 unknown_lineage")
        return blank

    _client(monkeypatch, handler)
    client = transport.EmberVmShimTransport()
    monkeypatch.setattr(client, "create_session", create_session)
    turn, used = asyncio.run(
        client.deliver(None, "cli-prior", "hello", restore_from="lineage-1")
    )

    # First call attempted the restore and was denied; second is the blank
    # fallback (no restore_from), same degrade pattern as the 410 retry arm.
    assert create_calls == ["lineage-1", None]
    assert used == blank
    # A blank session (restored=False) must not carry the prior CLI id forward.
    assert json.loads(requests[0].content)["session_id"] is None
    assert turn.result == "ok"


def test_deliver_with_no_ember_and_no_restore_from_is_unchanged(monkeypatch):
    """A normal first send (restore_from=None, the default) behaves exactly
    as it did before slice 5: no cli gating applied at all."""
    requests = []
    fresh = transport.EmberSession("s1", "t1", None)
    create_calls = []

    async def handler(request):
        requests.append(request)
        return _turn_response(request)

    async def create_session(restore_from=None):
        create_calls.append(restore_from)
        return fresh

    _client(monkeypatch, handler)
    client = transport.EmberVmShimTransport()
    monkeypatch.setattr(client, "create_session", create_session)
    turn, used = asyncio.run(client.deliver(None, "cli-x", "hello"))

    assert create_calls == [None]
    assert used == fresh
    assert json.loads(requests[0].content)["session_id"] == "cli-x"
    assert turn.result == "ok"
