import asyncio
import json

import httpx
import pytest  # noqa: F401

from bench.openrouter import OpenRouterClient


def test_complete_parses_usage_and_measures_latency():
    def handler(request):
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": "hello"}}],
                "usage": {"prompt_tokens": 11, "completion_tokens": 3},
            },
        )

    transport = httpx.MockTransport(handler)
    client = OpenRouterClient(api_key="test", transport=transport)
    c = asyncio.run(
        client.complete(
            model="x/y", messages=[{"role": "user", "content": "hi"}], temperature=0.0
        )
    )
    assert (
        c.text == "hello"
        and c.prompt_tokens == 11
        and c.completion_tokens == 3
        and c.latency_ms >= 0
    )


def test_price_lookup_computes_usd():
    client = OpenRouterClient(api_key="test")
    client._prices = {"x/y": (1.0, 2.0)}  # $/1M prompt, $/1M completion
    assert abs(client.cost_usd("x/y", 1_000_000, 500_000) - (1.0 + 1.0)) < 1e-9


def test_complete_merges_extra_body_without_overwriting_model():
    seen = {}

    def handler(request):
        seen["json"] = request.content
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": "ok"}}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1},
            },
        )

    transport = httpx.MockTransport(handler)
    client = OpenRouterClient(api_key="test", transport=transport, timeout=5.0)
    asyncio.run(
        client.complete(
            model="served-alias",
            messages=[{"role": "user", "content": "hi"}],
            temperature=0.0,
            extra_body={
                "model": "should-not-win",
                "chat_template_kwargs": {"reasoning_effort": "xhigh"},
            },
        )
    )
    payload = json.loads(seen["json"])
    assert payload["model"] == "served-alias"
    assert payload["chat_template_kwargs"] == {"reasoning_effort": "xhigh"}


def test_chat_merges_extra_body_without_overwriting_model():
    seen = {}

    def handler(request):
        seen["json"] = request.content
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": "", "tool_calls": []}}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1},
            },
        )

    transport = httpx.MockTransport(handler)
    client = OpenRouterClient(api_key="test", transport=transport)
    asyncio.run(
        client.chat(
            model="served-alias",
            messages=[{"role": "user", "content": "hi"}],
            extra_body={
                "model": "should-not-win",
                "chat_template_kwargs": {"reasoning_effort": "xhigh"},
            },
        )
    )
    payload = json.loads(seen["json"])
    assert payload["model"] == "served-alias"
    assert payload["chat_template_kwargs"] == {"reasoning_effort": "xhigh"}
