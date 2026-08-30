from __future__ import annotations

import asyncio
import base64
import json
import logging
import zlib

import httpx
import pytest

from agent_sessions import transport
from agent_sessions.transport import EmberSessionGone
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

    async def get(self, url, **kwargs):
        request = httpx.Request("GET", url, **kwargs)
        return await self.handler(request)

    async def delete(self, url, **kwargs):
        request = httpx.Request("DELETE", url, **kwargs)
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
    assert sleeps == [2, 5, 10]  # first three rungs of _CREATE_RETRY_SECONDS
    assert [json.loads(request.content) for request in attempts] == [
        {"restore_lineage": "lineage-1"}
    ] * 4
    assert result.session_id == "s1"


def test_create_session_retry_keeps_qwen_on_the_pi_workload(monkeypatch):
    """The retryable-backoff recursion must carry the model too.

    This is the fifth create path and the easiest to leave unguarded: it
    re-enters create_session positionally, so dropping the model there is
    invisible except that the retried create lands on the 4 GiB lane. It is
    live rather than theoretical, because a retryable create denial is exactly
    what pi-runtime's cap of 2 produces when the */5 probe overlaps an
    interactive qwen turn.
    """
    attempts = []

    async def handler(request):
        attempts.append(request)
        if len(attempts) < 3:
            return _error_response(request, 409, True)
        return httpx.Response(
            200,
            json={"session_id": "s1", "session_token": "t1"},
            request=request,
        )

    async def fake_sleep(seconds):
        return None

    _client(monkeypatch, handler)
    monkeypatch.setattr(transport.asyncio, "sleep", fake_sleep)
    asyncio.run(transport.EmberVmShimTransport().create_session(model="qwen"))

    assert len(attempts) == 3
    # EVERY attempt, not just the first: the recursion must not lose the model.
    assert [str(request.url) for request in attempts] == [
        "https://ember.test/v1/workloads/pi-runtime/sessions"
    ] * 3


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


# -- qwen (pi family) routing: create_session targets pi-runtime -----------


def test_create_session_qwen_targets_pi_runtime_workload(monkeypatch):
    requests = []

    async def handler(request):
        requests.append(request)
        return httpx.Response(
            201,
            json={"session_id": "s1", "session_token": "t1"},
            request=request,
        )

    _client(monkeypatch, handler)
    asyncio.run(transport.EmberVmShimTransport().create_session(model="qwen"))

    assert str(requests[0].url) == "https://ember.test/v1/workloads/pi-runtime/sessions"


@pytest.mark.parametrize("model", [None, "opus", "sonnet", "fable", "luna"])
def test_create_session_non_pi_models_target_claude_runtime_workload(
    monkeypatch, model
):
    requests = []

    async def handler(request):
        requests.append(request)
        return httpx.Response(
            201,
            json={"session_id": "s1", "session_token": "t1"},
            request=request,
        )

    _client(monkeypatch, handler)
    asyncio.run(transport.EmberVmShimTransport().create_session(model=model))

    assert (
        str(requests[0].url)
        == "https://ember.test/v1/workloads/claude-runtime/sessions"
    )


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
    assert json.loads(request.content) == {
        "message": "hello",
        "session_id": "cli-1",
        "thinking": "off",
    }
    assert turn.result == "ok"
    assert turn.diff is None
    assert used == ember


def test_deliver_maps_valid_guest_diff(monkeypatch):
    captured = {
        "base_sha": "a" * 40,
        "zlib_b64": base64.b64encode(zlib.compress(b"diff")).decode("ascii"),
        "truncated": False,
    }

    async def handler(request):
        response = _turn_response(request)
        payload = response.json()
        payload["diff"] = captured
        return httpx.Response(200, json=payload, request=request)

    _client(monkeypatch, handler)
    turn, _ = asyncio.run(
        transport.EmberVmShimTransport().deliver(
            transport.EmberSession("s1", "t1", None), "cli-1", "hello"
        )
    )

    assert turn.diff == captured


def test_deliver_maps_valid_guest_artifact(monkeypatch):
    captured = {
        "path": "plan.json",
        "content_b64": base64.b64encode(b'{"nodes": []}').decode("ascii"),
        "outcome": "ok",
    }

    async def handler(request):
        response = _turn_response(request)
        payload = response.json()
        payload["artifact"] = captured
        return httpx.Response(200, json=payload, request=request)

    _client(monkeypatch, handler)
    turn, _ = asyncio.run(
        transport.EmberVmShimTransport().deliver(
            transport.EmberSession("s1", "t1", None),
            "cli-1",
            "hello",
            artifact_path="plan.json",
        )
    )

    assert turn.artifact == captured


def test_deliver_rejects_guest_artifact_with_mismatched_path(monkeypatch, caplog):
    captured = {
        "path": "other.json",
        "content_b64": base64.b64encode(b'{"nodes": []}').decode("ascii"),
        "outcome": "ok",
    }

    async def handler(request):
        response = _turn_response(request)
        payload = response.json()
        payload["artifact"] = captured
        return httpx.Response(200, json=payload, request=request)

    _client(monkeypatch, handler)
    with caplog.at_level(logging.WARNING, logger=transport.logger.name):
        turn, _ = asyncio.run(
            transport.EmberVmShimTransport().deliver(
                transport.EmberSession("s1", "t1", None),
                "cli-1",
                "hello",
                artifact_path="plan.json",
            )
        )

    assert turn.artifact is None
    assert "path does not match the declared artifact" in caplog.text


def test_deliver_rejects_guest_artifact_when_not_declared(monkeypatch, caplog):
    captured = {
        "path": "plan.json",
        "content_b64": base64.b64encode(b'{"nodes": []}').decode("ascii"),
        "outcome": "ok",
    }

    async def handler(request):
        response = _turn_response(request)
        payload = response.json()
        payload["artifact"] = captured
        return httpx.Response(200, json=payload, request=request)

    _client(monkeypatch, handler)
    with caplog.at_level(logging.WARNING, logger=transport.logger.name):
        turn, _ = asyncio.run(
            transport.EmberVmShimTransport().deliver(
                transport.EmberSession("s1", "t1", None), "cli-1", "hello"
            )
        )

    assert turn.artifact is None
    assert "artifact was not declared" in caplog.text


@pytest.mark.parametrize("artifact_path", [None, "plan.json"])
def test_deliver_sends_artifact_path_only_when_declared(monkeypatch, artifact_path):
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
            artifact_path=artifact_path,
        )
    )

    payload = json.loads(requests[0].content)
    if artifact_path is None:
        assert "artifact_path" not in payload
    else:
        assert payload["artifact_path"] == artifact_path


@pytest.mark.parametrize(
    "value",
    [None, "bad", {}, {"base_sha": "x", "zlib_b64": "bad", "truncated": False}],
)
def test_deliver_ignores_absent_or_malformed_guest_diff(monkeypatch, value):
    async def handler(request):
        response = _turn_response(request)
        payload = response.json()
        if value is not None:
            payload["diff"] = value
        return httpx.Response(200, json=payload, request=request)

    _client(monkeypatch, handler)
    turn, _ = asyncio.run(
        transport.EmberVmShimTransport().deliver(
            transport.EmberSession("s1", "t1", None), "cli-1", "hello"
        )
    )

    assert turn.diff is None


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
        "thinking": "off",
        "model": "fable",
    }


@pytest.mark.parametrize(
    ("reasoning", "expected_thinking"), [(True, "high"), (False, "off")]
)
def test_deliver_sets_thinking_from_reasoning(
    monkeypatch, reasoning, expected_thinking
):
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
            reasoning=reasoning,
        )
    )

    assert json.loads(requests[0].content)["thinking"] == expected_thinking


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


def test_deliver_includes_system_prompt_when_present(monkeypatch):
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
            system_prompt="X",
        )
    )
    assert json.loads(requests[0].content)["system_prompt"] == "X"


def test_deliver_omits_system_prompt_when_none(monkeypatch):
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
    assert "system_prompt" not in json.loads(requests[0].content)


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

    async def create_session(restore_from=None, model=None):
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

    async def create_session(restore_from=None, model=None):
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

    async def create_session(restore_from=None, model=None):
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

    async def create_session(restore_from=None, model=None):
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

    async def create_session(restore_from=None, model=None):
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

    async def create_session(model=None):
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

    async def create_session(model=None):
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

    async def create_session(model=None):
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

    async def create_session(restore_from=None, model=None):
        return fresh

    monkeypatch.setattr(client, "create_session", create_session)
    turn, _ = asyncio.run(client.deliver(None, "cli-1", "hello"))

    assert turn.result == "ok"
    assert len(attempts) == 4
    assert sleeps == [2, 5, 10]  # first three rungs of _CREATE_RETRY_SECONDS


def test_deliver_retryable_exhaustion(monkeypatch):
    attempts = []
    sleeps = []

    async def handler(request):
        attempts.append(request)
        return _error_response(request, 429, True)

    async def fake_sleep(seconds):
        sleeps.append(seconds)

    async def create_session(restore_from=None, model=None):
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

    async def create_session(restore_from=None, model=None):
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

    async def create_session(restore_from=None, model=None):
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

    async def create_session(restore_from=None, model=None):
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

    async def create_session(restore_from=None, model=None):
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

    async def create_session(restore_from=None, model=None):
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

    async def create_session(restore_from=None, model=None):
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

    async def create_session(restore_from=None, model=None):
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


# -- qwen (pi family) routing end-to-end through deliver --------------------
# These exercise the REAL create_session (no monkeypatch on it), so the
# create URL is the thing under test, not a fake's return value.


def test_deliver_creates_qwen_session_on_pi_workload(monkeypatch):
    requests = []

    async def handler(request):
        requests.append(request)
        if str(request.url).endswith("/sessions"):
            return httpx.Response(
                201,
                json={"session_id": "s-pi", "session_token": "t-pi"},
                request=request,
            )
        return _turn_response(request)

    _client(monkeypatch, handler)
    client = transport.EmberVmShimTransport()
    turn, used = asyncio.run(client.deliver(None, None, "hello", model="qwen"))

    create_request, invoke_request = requests
    assert (
        str(create_request.url) == "https://ember.test/v1/workloads/pi-runtime/sessions"
    )
    # The workload choice must not leak into the invoke URL: it is always
    # session-scoped regardless of which workload the session lives on.
    assert str(invoke_request.url) == "https://ember.test/v1/sessions/s-pi/invoke"
    assert used.session_id == "s-pi"
    assert turn.result == "ok"


def test_deliver_with_existing_ember_issues_no_create(monkeypatch):
    """A live binding on claude-runtime must keep working untouched: this
    change must not migrate an existing session to a different workload."""
    requests = []

    async def handler(request):
        requests.append(request)
        return _turn_response(request)

    _client(monkeypatch, handler)
    ember = transport.EmberSession("s1", "t1", None)
    turn, used = asyncio.run(
        transport.EmberVmShimTransport().deliver(ember, "cli-1", "hello", model="qwen")
    )

    assert len(requests) == 1
    assert str(requests[0].url) == "https://ember.test/v1/sessions/s1/invoke"
    assert used == ember
    assert turn.result == "ok"


def test_deliver_restore_from_passes_model_to_pi_workload(monkeypatch):
    requests = []

    async def handler(request):
        requests.append(request)
        if str(request.url).endswith("/sessions"):
            return httpx.Response(
                201,
                json={
                    "session_id": "s-pi-2",
                    "session_token": "t-pi-2",
                    "lineage_id": "lineage-1",
                    "restored": True,
                },
                request=request,
            )
        return _turn_response(request)

    _client(monkeypatch, handler)
    client = transport.EmberVmShimTransport()
    turn, used = asyncio.run(
        client.deliver(
            None, "cli-prior", "hello", model="qwen", restore_from="lineage-1"
        )
    )

    create_request = requests[0]
    assert (
        str(create_request.url) == "https://ember.test/v1/workloads/pi-runtime/sessions"
    )
    assert json.loads(create_request.content) == {"restore_lineage": "lineage-1"}
    assert used.session_id == "s-pi-2"
    assert turn.result == "ok"


def test_deliver_restore_denied_falls_back_to_blank_on_pi_workload(monkeypatch):
    """A cross-workload restore is one of the CP's documented denial reasons
    (workload/principal mismatch). The existing degrade-to-blank fallback
    must still land on the CORRECT (pi) workload, not silently drop back to
    claude-runtime."""
    requests = []

    async def handler(request):
        requests.append(request)
        if str(request.url).endswith("/sessions"):
            body = json.loads(request.content) if request.content else {}
            if body.get("restore_lineage"):
                return _error_response(request, 403, False)
            return httpx.Response(
                201,
                json={"session_id": "s-blank", "session_token": "t-blank"},
                request=request,
            )
        return _turn_response(request)

    _client(monkeypatch, handler)
    client = transport.EmberVmShimTransport()
    turn, used = asyncio.run(
        client.deliver(
            None, "cli-prior", "hello", model="qwen", restore_from="lineage-1"
        )
    )

    denied_create, blank_create, _invoke = requests
    assert (
        str(denied_create.url) == "https://ember.test/v1/workloads/pi-runtime/sessions"
    )
    assert (
        str(blank_create.url) == "https://ember.test/v1/workloads/pi-runtime/sessions"
    )
    assert used.session_id == "s-blank"
    assert turn.workspace_recovery == {
        "created": True,
        "restored": False,
        "degraded": "restore_denied",
    }


def test_deliver_session_gone_recreates_qwen_on_pi_workload(monkeypatch):
    """The 403/410 mid-conversation recovery arm must recreate on the PI lane.

    This arm is the one that fails silently if the model is not threaded: the
    turn still succeeds, it just runs on the 4 GiB claude-runtime lane, so
    nothing surfaces the escape. Both of its create_session calls (the restore
    and the degrade-to-blank fallback) are covered here.
    """
    requests = []
    turn_codes = [410, 200]

    async def handler(request):
        requests.append(request)
        if str(request.url).endswith("/sessions"):
            body = json.loads(request.content) if request.content else {}
            if body.get("restore_lineage"):
                return _error_response(request, 403, False)
            return httpx.Response(
                201,
                json={"session_id": "s-pi-new", "session_token": "t-pi-new"},
                request=request,
            )
        return _turn_response(request, turn_codes.pop(0))

    _client(monkeypatch, handler)
    client = transport.EmberVmShimTransport()
    turn, used = asyncio.run(
        client.deliver(
            transport.EmberSession("s1", "t1", None), "cli-1", "hello", model="qwen"
        )
    )

    first_invoke, denied_create, blank_create, retry_invoke = requests
    assert str(first_invoke.url) == "https://ember.test/v1/sessions/s1/invoke"
    # Both creates on the recovery arm must target pi-runtime, not the
    # claude-runtime default the transport instance carries.
    assert (
        str(denied_create.url) == "https://ember.test/v1/workloads/pi-runtime/sessions"
    )
    assert (
        str(blank_create.url) == "https://ember.test/v1/workloads/pi-runtime/sessions"
    )
    # The lane choice must not leak into the session-scoped invoke URL.
    assert str(retry_invoke.url) == "https://ember.test/v1/sessions/s-pi-new/invoke"
    assert used.session_id == "s-pi-new"
    assert turn.result == "ok"


# -- AGENT_PI_WORKLOAD revert lever ------------------------------------------


def test_agent_pi_workload_override_sends_qwen_to_claude_runtime(monkeypatch):
    """The revert lever: setting the override to claude-runtime must put
    qwen back on the old lane by a values edit, with no code deploy."""
    requests = []

    async def handler(request):
        requests.append(request)
        return httpx.Response(
            201,
            json={"session_id": "s1", "session_token": "t1"},
            request=request,
        )

    _client(monkeypatch, handler)
    monkeypatch.setattr(transport, "PI_WORKLOAD", "claude-runtime")
    asyncio.run(transport.EmberVmShimTransport().create_session(model="qwen"))

    assert (
        str(requests[0].url)
        == "https://ember.test/v1/workloads/claude-runtime/sessions"
    )


def test_agent_pi_workload_env_blank_or_unset_resolves_to_pi_runtime(monkeypatch):
    """AGENT_PI_WORKLOAD unset OR blank both mean "use the code default": there
    is no security semantic here, so blank is not a deny lever.

    Asserted against the resolver rather than by reloading the module:
    importlib.reload rebinds EmberSessionGone to a fresh class object, which
    silently breaks every later pytest.raises(EmberSessionGone) in this file.
    """
    monkeypatch.delenv("AGENT_PI_WORKLOAD", raising=False)
    assert transport._resolve_pi_workload() == "pi-runtime"

    monkeypatch.setenv("AGENT_PI_WORKLOAD", "")
    assert transport._resolve_pi_workload() == "pi-runtime"

    # A set, non-blank value is the revert lever and IS honoured.
    monkeypatch.setenv("AGENT_PI_WORKLOAD", "claude-runtime")
    assert transport._resolve_pi_workload() == "claude-runtime"


# -- list_sessions workload lane selection -----------------------------------


def test_list_sessions_targets_named_workload(monkeypatch):
    requests = []

    async def handler(request):
        requests.append(request)
        return httpx.Response(200, json={"items": []}, request=request)

    _client(monkeypatch, handler)
    asyncio.run(transport.EmberVmShimTransport().list_sessions(workload="pi-runtime"))

    assert (
        str(requests[0].url).split("?")[0]
        == "https://ember.test/v1/workloads/pi-runtime/sessions"
    )


def test_list_sessions_defaults_to_claude_runtime(monkeypatch):
    requests = []

    async def handler(request):
        requests.append(request)
        return httpx.Response(200, json={"items": []}, request=request)

    _client(monkeypatch, handler)
    asyncio.run(transport.EmberVmShimTransport().list_sessions())

    assert (
        str(requests[0].url).split("?")[0]
        == "https://ember.test/v1/workloads/claude-runtime/sessions"
    )


def test_destroy_session_maps_404_to_session_gone(monkeypatch):
    """404 is the control plane's "session not found": the goal state."""

    async def handler(request):
        return httpx.Response(
            404,
            json={"error": "session not found", "retryable": False},
            request=request,
        )

    _client(monkeypatch, handler)
    with pytest.raises(EmberSessionGone):
        asyncio.run(transport.EmberVmShimTransport().destroy_session("s-1"))


def test_destroy_session_keeps_403_and_500_as_plain_failures(monkeypatch):
    """Only 404/410 mean gone on this route.

    DELETE is management-auth, so its 403 is "service account not permitted",
    which fails EVERY destroy at once. Treating that as gone would report the
    whole fleet reaped while every VM stayed alive holding a capacity slot.
    A 500 whose body merely mentions a missing resource must not be gone
    either.
    """
    statuses = []

    async def handler(request):
        status = statuses.pop(0)
        return httpx.Response(
            status,
            json={"error": "service account not permitted / not found"},
            request=request,
        )

    _client(monkeypatch, handler)
    for status in (403, 500):
        statuses.append(status)
        with pytest.raises(EmberVMTransportError) as caught:
            asyncio.run(transport.EmberVmShimTransport().destroy_session("s-404xyz"))
        assert not isinstance(caught.value, EmberSessionGone), status


def test_guest_diff_logs_a_distinct_reason_per_rejection(caplog):
    # A present-but-invalid payload and an absent one both yield None, so the
    # database cannot tell them apart. The reason is the only thing that can.
    good = base64.b64encode(zlib.compress(b"diff --git a/a b/a\n")).decode()
    sha = "a" * 40
    cases = [
        (
            {"base_sha": sha, "zlib_b64": good, "truncated": "no"},
            "truncated is not a bool",
        ),
        ({"base_sha": "nothex", "zlib_b64": good, "truncated": False}, "base_sha"),
        ({"base_sha": sha, "truncated": False}, "missing keys"),
        (
            {"base_sha": sha, "zlib_b64": "!!!not base64!!!", "truncated": False},
            "undecodable",
        ),
        ("not a mapping", "not a mapping"),
    ]
    for payload, expected in cases:
        caplog.clear()
        with caplog.at_level(logging.WARNING, logger=transport.logger.name):
            assert transport._guest_diff(payload, 42) is None
        assert expected in caplog.text, f"{payload!r} did not log {expected!r}"
        assert "42" in caplog.text


def test_guest_diff_is_silent_when_absent(caplog):
    # Guests predating diff capture send nothing. That is not a fault and must
    # not produce a warning on every single turn they take.
    with caplog.at_level(logging.WARNING, logger=transport.logger.name):
        assert transport._guest_diff(None, 42) is None
    assert caplog.text == ""


def test_guest_diff_accepts_a_valid_payload(caplog):
    payload = {
        "base_sha": "b" * 40,
        "zlib_b64": base64.b64encode(zlib.compress(b"diff --git a/a b/a\n")).decode(),
        "truncated": False,
    }
    with caplog.at_level(logging.WARNING, logger=transport.logger.name):
        assert transport._guest_diff(payload, 42) == payload
    assert caplog.text == ""


def test_guest_diff_accepts_a_valid_truncated_payload_with_blob(caplog):
    payload = {
        "base_sha": "b" * 40,
        "zlib_b64": base64.b64encode(zlib.compress(b"diff --git a/a b/a\n")).decode(),
        "truncated": True,
    }
    with caplog.at_level(logging.WARNING, logger=transport.logger.name):
        assert transport._guest_diff(payload, 42) == payload
    assert caplog.text == ""


def test_guest_diff_rejects_an_invalid_truncated_payload_with_blob(caplog):
    payload = {
        "base_sha": "b" * 40,
        "zlib_b64": "!!!not base64!!!",
        "truncated": True,
    }
    with caplog.at_level(logging.WARNING, logger=transport.logger.name):
        assert transport._guest_diff(payload, 42) is None
    assert "undecodable" in caplog.text


def test_guest_diff_accepts_a_legacy_truncated_payload_without_blob(caplog):
    payload = {"base_sha": "b" * 40, "zlib_b64": None, "truncated": True}
    with caplog.at_level(logging.WARNING, logger=transport.logger.name):
        assert transport._guest_diff(payload, 42) == payload
    assert caplog.text == ""


def test_guest_artifact_logs_a_distinct_reason_per_rejection(caplog):
    good = base64.b64encode(b'{"nodes": []}').decode("ascii")
    cases = [
        ("not a mapping", "not a mapping"),
        ({"path": "plan.json"}, "missing keys"),
        (
            {"path": "plan.json", "content_b64": None, "outcome": "unknown"},
            "invalid outcome",
        ),
        (
            {"path": "plan.json", "content_b64": good, "outcome": "missing"},
            "must be null",
        ),
        (
            {"path": "plan.json", "content_b64": "not-base64", "outcome": "ok"},
            "undecodable",
        ),
        (
            {
                "path": "plan.json",
                "content_b64": base64.b64encode(b"x" * (256 * 1024 + 1)).decode(),
                "outcome": "ok",
            },
            "over 256 KiB",
        ),
        ({"path": "", "content_b64": good, "outcome": "ok"}, "non-empty"),
    ]
    for payload, expected in cases:
        caplog.clear()
        declared_path = (
            payload.get("path") if isinstance(payload, dict) else "plan.json"
        )
        with caplog.at_level(logging.WARNING, logger=transport.logger.name):
            assert transport._guest_artifact(payload, declared_path, 42) is None
        assert expected in caplog.text, f"{payload!r} did not log {expected!r}"
        assert "discarding guest artifact for session 42" in caplog.text


def test_guest_artifact_accepts_valid_ok_and_missing_payloads(caplog):
    payloads = [
        {
            "path": "plan.json",
            "content_b64": base64.b64encode(b'{"nodes": []}').decode("ascii"),
            "outcome": "ok",
        },
        {"path": "plan.json", "content_b64": None, "outcome": "missing"},
    ]
    with caplog.at_level(logging.WARNING, logger=transport.logger.name):
        for payload in payloads:
            assert transport._guest_artifact(payload, "plan.json", 42) == payload
    assert caplog.text == ""


def _capacity_response(request: httpx.Request, reason: str = "workload_cap"):
    """The exact body the control plane returns for a cap denial."""
    return httpx.Response(
        429,
        json={
            "error": "session create denied",
            "reason": reason,
            "workload": "pi-runtime",
            "retryable": True,
        },
        request=request,
    )


def test_create_session_waits_out_a_capacity_denial(monkeypatch):
    """A cap denial must outwait a running turn, not the 17s generic ladder.

    piRuntimeWorkload.concurrency.cap is 2, a pi turn runs until the 900s
    invoke watchdog, and the slot frees only when that turn ends. The old
    3-attempt ladder summed to 17 seconds, so an ad-hoc create that collided
    with the drainer failed every time.
    """
    attempts = []
    sleeps = []

    async def handler(request):
        attempts.append(request)
        if len(attempts) < 6:
            return _capacity_response(request)
        return httpx.Response(
            200,
            json={"session_id": "s1", "session_token": "t1"},
            request=request,
        )

    async def fake_sleep(seconds):
        sleeps.append(seconds)

    _client(monkeypatch, handler)
    monkeypatch.setattr(transport.asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(transport.random, "uniform", lambda _lo, _hi: 0.0)

    result = asyncio.run(transport.EmberVmShimTransport().create_session())

    assert result.session_id == "s1"
    assert len(attempts) == 6
    assert sleeps == [5, 10, 20, 30, 45]
    assert sum(transport._CAPACITY_BACKOFF_SECONDS) > 900, (
        "the ladder must outlast one watchdog-bounded turn"
    )


def test_create_session_capacity_denial_eventually_gives_up(monkeypatch):
    attempts = []

    async def handler(request):
        attempts.append(request)
        return _capacity_response(request)

    async def fake_sleep(_seconds):
        return None

    _client(monkeypatch, handler)
    monkeypatch.setattr(transport.asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(transport.random, "uniform", lambda _lo, _hi: 0.0)

    with pytest.raises(transport.EmberVMTransportError):
        asyncio.run(transport.EmberVmShimTransport().create_session())

    assert len(attempts) == len(transport._CAPACITY_BACKOFF_SECONDS) + 1


def test_create_session_non_capacity_429_uses_the_generic_ladder(monkeypatch):
    """A retryable 429 that is not a cap denial takes the generic ladder.

    Only capacity has to outlast a whole turn. Everything else retryable gets
    the shorter generic ladder, which is still minutes rather than the 17
    seconds it used to be.
    """
    attempts = []
    sleeps = []

    async def handler(request):
        attempts.append(request)
        return httpx.Response(
            429,
            json={"error": "rate limit exceeded", "retryable": True},
            request=request,
        )

    async def fake_sleep(seconds):
        sleeps.append(seconds)

    _client(monkeypatch, handler)
    monkeypatch.setattr(transport.asyncio, "sleep", fake_sleep)

    with pytest.raises(transport.EmberVMTransportError):
        asyncio.run(transport.EmberVmShimTransport().create_session())

    assert sleeps == list(transport._CREATE_RETRY_SECONDS)


def test_capacity_jitter_stays_within_bounds(monkeypatch):
    """Jitter spreads waiters off the same freed slot without going negative."""
    monkeypatch.setattr(transport.random, "uniform", lambda lo, _hi: lo)
    assert transport._capacity_sleep_seconds(0) == pytest.approx(5 * 0.75)
    monkeypatch.setattr(transport.random, "uniform", lambda _lo, hi: hi)
    assert transport._capacity_sleep_seconds(0) == pytest.approx(5 * 1.25)
    # Past the end of the ladder the last rung repeats rather than raising.
    assert transport._capacity_sleep_seconds(999) == pytest.approx(180 * 1.25)


def test_create_session_retries_prime_failed_past_the_old_17s_window(monkeypatch):
    """A 500 prime_failed is retryable, and 17 seconds was too short for it.

    On 2026-08-29 seven consecutive creates failed this way at 17 to 22
    seconds each while EmberVM was healthy, then the condition cleared and a
    create succeeded after 3m30s. The server marks it retryable; the client
    was the thing giving up too early.
    """
    attempts = []
    sleeps = []

    async def handler(request):
        attempts.append(request)
        if len(attempts) < 8:
            return httpx.Response(
                500,
                json={
                    "error": "session create failed",
                    "reason": "{:prime_failed, {:error, %GRPC.RPCError{}}}",
                    "workload": "pi-runtime",
                    "retryable": True,
                },
                request=request,
            )
        return httpx.Response(
            200,
            json={"session_id": "s1", "session_token": "t1"},
            request=request,
        )

    async def fake_sleep(seconds):
        sleeps.append(seconds)

    _client(monkeypatch, handler)
    monkeypatch.setattr(transport.asyncio, "sleep", fake_sleep)

    result = asyncio.run(transport.EmberVmShimTransport().create_session())

    assert result.session_id == "s1"
    assert len(attempts) == 8, "it must keep retrying well past the old 3 attempts"
    assert sum(transport._CREATE_RETRY_SECONDS) > 210, (
        "the ladder must outlast the observed 3m30s recovery, not 17 seconds"
    )
    assert sum(transport._CREATE_RETRY_SECONDS) < sum(
        transport._CAPACITY_BACKOFF_SECONDS
    ), "a non-capacity failure must not wait as long as a full turn"
    assert sleeps == list(transport._CREATE_RETRY_SECONDS[:7])
