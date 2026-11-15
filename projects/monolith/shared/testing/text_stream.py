"""Synthetic upstream streams and the contract shared by the two adapters."""

import asyncio
import json

import httpx
import pytest

import shared.inference


def frame(value):
    return "data: " + json.dumps(value) + "\n\n"


def text(value):
    return frame({"choices": [{"delta": {"content": value}}]})


MIXED = [
    "\n: comment\nevent: token\nnot data\ndata: broken\n\n",
    'data:{"choices":[{"delta":{"content":"Hello"}}]}\n\n',
    frame({"choices": None}),
    frame({"choices": [{"delta": None}, {"delta": {"content": ""}}]}),
    text(" world"),
]
COUNTS = {"prompt_tokens": "17", "completion_tokens": "3", "total_tokens": 20}


class Harness:
    """Use real httpx contexts over a network-free, observable byte stream."""

    def __init__(self, monkeypatch, parts, *, status=200):
        self.events = []
        self.requests = []
        self.auth_inputs = []
        self.usage_calls = []
        self.reading = asyncio.Event()
        self.timeouts = []
        owner = self

        class Stream(httpx.AsyncByteStream):
            async def __aiter__(self):
                for part in parts:
                    if part is None:
                        owner.reading.set()
                        await asyncio.Event().wait()
                    elif isinstance(part, BaseException):
                        raise part
                    else:
                        yield part.encode()

            async def aclose(self):
                owner.events.append("response_closed")

        def handler(request):
            owner.requests.append(request)
            return httpx.Response(status, stream=Stream())

        original_client = httpx.AsyncClient

        class Client(original_client):
            def __init__(self, *, timeout):
                owner.timeouts.append(timeout)
                super().__init__(
                    timeout=timeout, transport=httpx.MockTransport(handler)
                )

            async def __aexit__(self, *args):
                await super().__aexit__(*args)
                owner.events.append("client_closed")

        def auth_headers(endpoint):
            owner.auth_inputs.append(endpoint)
            return {"Authorization": "Bearer synthetic-test-only"}

        def record_usage(*args):
            assert owner.events == ["response_closed", "client_closed"]
            owner.usage_calls.append(args)

        monkeypatch.setattr(httpx, "AsyncClient", Client)
        monkeypatch.setattr(shared.inference, "auth_headers", auth_headers)
        monkeypatch.setattr(shared.inference, "record_usage", record_usage)

    def assert_closed(self):
        assert self.events == ["response_closed", "client_closed"]

    def assert_request(self, endpoint, model, messages, cap, timeout):
        assert len(self.requests) == 1
        request = self.requests[0]
        assert request.method == "POST"
        assert str(request.url) == endpoint + "/v1/chat/completions"
        assert self.auth_inputs == [endpoint]
        assert request.headers["Authorization"] == "Bearer synthetic-test-only"
        assert self.timeouts == [timeout]
        assert json.loads(request.content) == {
            "model": model,
            "messages": messages,
            "max_tokens": cap,
            "stream": True,
            "stream_options": {"include_usage": True},
        }


class AdapterContract:
    """Inherited by each adapter's explicitly registered test target."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("ending", [[], ["data: [DONE]\n\n", text("ignored")]])
    async def test_mixed_frames_and_last_usage(self, monkeypatch, adapter, ending):
        earlier = {"prompt_tokens": 2, "completion_tokens": 1}
        harness = Harness(
            monkeypatch,
            MIXED + [frame({"usage": earlier}), frame({"usage": COUNTS})] + ending,
        )
        output = [event async for event in adapter.stream()]
        adapter.assert_output(output, "Hello world", COUNTS)
        harness.assert_closed()
        assert harness.usage_calls == [(COUNTS, adapter.model, adapter.caller)]
        harness.assert_request(
            adapter.endpoint,
            adapter.model,
            adapter.messages,
            adapter.cap,
            adapter.timeout,
        )

    @pytest.mark.asyncio
    @pytest.mark.parametrize("usage", [None, {}, False, 0, "", []])
    async def test_empty_usage_estimates(self, monkeypatch, adapter, usage):
        harness = Harness(monkeypatch, [text("Hello world"), frame({"usage": usage})])
        output = [event async for event in adapter.stream()]
        adapter.assert_output(output, "Hello world", None)
        assert harness.usage_calls == [(None, adapter.model, adapter.caller)]
        harness.assert_closed()

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "parts, expected", [([], ""), ([text("Hello world")], "Hello world")]
    )
    async def test_missing_usage(self, monkeypatch, adapter, parts, expected):
        harness = Harness(monkeypatch, parts)
        output = [event async for event in adapter.stream()]
        adapter.assert_output(output, expected, None)
        assert harness.usage_calls == [(None, adapter.model, adapter.caller)]
        harness.assert_closed()

    @pytest.mark.asyncio
    async def test_done_ignores_invalid_later_frames(self, monkeypatch, adapter):
        harness = Harness(
            monkeypatch,
            [
                text("ok"),
                "data: [DONE]\n\n",
                frame(None),
                frame({"usage": "bad"}),
                httpx.ReadError("unread"),
            ],
        )
        output = [event async for event in adapter.stream()]
        adapter.assert_output(output, "ok", None)
        assert harness.usage_calls == [(None, adapter.model, adapter.caller)]

    @pytest.mark.asyncio
    async def test_repeated_usage_with_text_then_empty_usage(
        self, monkeypatch, adapter
    ):
        first = {"prompt_tokens": 1, "completion_tokens": 1}
        harness = Harness(
            monkeypatch,
            [
                frame({"usage": first, "choices": [{"delta": {"content": "Hello"}}]}),
                frame({"usage": COUNTS, "choices": [{"delta": {"content": " world"}}]}),
                frame({"usage": {}}),
            ],
        )
        output = [event async for event in adapter.stream()]
        adapter.assert_output(output, "Hello world", COUNTS)
        assert harness.usage_calls == [(COUNTS, adapter.model, adapter.caller)]

    @pytest.mark.asyncio
    @pytest.mark.parametrize("payload", [[], None, 7, "object expected"])
    async def test_non_object_json_fails(self, monkeypatch, adapter, payload):
        harness = Harness(monkeypatch, [text("partial"), frame(payload)])
        stream = adapter.stream()
        assert adapter.text(await anext(stream)) == "partial"
        with pytest.raises(AttributeError):
            await anext(stream)
        harness.assert_closed()
        assert harness.usage_calls == []

    @pytest.mark.asyncio
    @pytest.mark.parametrize("failure", ["status", "read"])
    async def test_http_failure(self, monkeypatch, adapter, failure):
        error = httpx.ReadError("synthetic read failure")
        parts = [text("partial"), error] if failure == "read" else []
        harness = Harness(
            monkeypatch, parts, status=503 if failure == "status" else 200
        )
        stream = adapter.stream()
        if failure == "read":
            assert adapter.text(await anext(stream)) == "partial"
        with pytest.raises(adapter.http_error) as caught:
            await anext(stream)
        if adapter.caller == "grimoire_chat":
            assert isinstance(caught.value.__cause__, httpx.HTTPError)
            assert (
                str(caught.value)
                == "inference request failed: " + type(caught.value.__cause__).__name__
            )
            if failure == "read":
                assert caught.value.__cause__ is error
        elif failure == "read":
            assert caught.value is error
        harness.assert_closed()
        assert harness.usage_calls == []

    @pytest.mark.asyncio
    async def test_close_after_text(self, monkeypatch, adapter):
        harness = Harness(monkeypatch, [text("partial"), frame({"usage": COUNTS})])
        stream = adapter.stream()
        assert adapter.text(await anext(stream)) == "partial"
        await stream.aclose()
        harness.assert_closed()
        assert harness.usage_calls == []

    @pytest.mark.asyncio
    async def test_cancel_read(self, monkeypatch, adapter):
        harness = Harness(monkeypatch, [text("partial"), None])
        stream = adapter.stream()
        await anext(stream)
        task = asyncio.create_task(anext(stream))
        await asyncio.wait_for(harness.reading.wait(), 2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        harness.assert_closed()
        assert harness.usage_calls == []
