"""Tests for knowledge/chat.py — rate limiter and stream helper."""

import asyncio
import json
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from app.db import get_session
from app.main import app
from knowledge.chat import PublicNotesRateLimiter, _build_context
from knowledge.router import get_embedding_client


# ── _build_context ──────────────────────────────────────────────────


class TestBuildContext:
    def test_includes_title_and_snippet(self):
        results = [
            {"title": "Foo", "snippet": "bar baz", "section": ""},
        ]
        ctx = _build_context(results)
        assert "Foo" in ctx
        assert "bar baz" in ctx

    def test_section_appended_when_present(self):
        results = [
            {"title": "T", "snippet": "body", "section": "## Intro"},
        ]
        ctx = _build_context(results)
        assert "## Intro" in ctx

    def test_caps_at_max_context_notes(self):
        results = [
            {"title": f"Note {i}", "snippet": "x", "section": ""}
            for i in range(10)
        ]
        ctx = _build_context(results)
        # Only MAX_CONTEXT_NOTES (6) should appear.
        assert ctx.count("## Note") == 0  # sections absent
        assert ctx.count("Note ") == 6

    def test_empty_results(self):
        assert _build_context([]) == ""


# ── PublicNotesRateLimiter ──────────────────────────────────────────


class TestPublicNotesRateLimiter:
    def test_first_request_allowed(self):
        limiter = PublicNotesRateLimiter()
        allowed, remaining, reset_epoch = asyncio.run(limiter.check("1.2.3.4"))
        assert allowed is True
        assert reset_epoch > time.time()

    def test_exhausting_burst_blocks(self):
        from knowledge.chat import RATE_LIMIT_BURST

        limiter = PublicNotesRateLimiter()
        ip = "10.0.0.1"

        async def _drain():
            results = []
            for _ in range(RATE_LIMIT_BURST + 2):
                results.append(await limiter.check(ip))
            return results

        results = asyncio.run(_drain())
        allowed_flags = [r[0] for r in results]
        # First BURST requests should be allowed.
        assert all(allowed_flags[:RATE_LIMIT_BURST])
        # At least the last one must be blocked.
        assert not allowed_flags[-1]

    def test_different_ips_independent(self):
        limiter = PublicNotesRateLimiter()

        async def _check():
            a = await limiter.check("1.1.1.1")
            b = await limiter.check("2.2.2.2")
            return a, b

        (a_allowed, *_), (b_allowed, *_) = asyncio.run(_check())
        assert a_allowed
        assert b_allowed


# ── /api/knowledge/public/chat endpoint ────────────────────────────

FAKE_EMBEDDING = [0.1] * 1024
FAKE_RESULTS = [
    {
        "note_id": "n1",
        "title": "Attention",
        "snippet": "transformers replace recurrence",
        "section": "## Architecture",
        "score": 0.9,
    }
]


@pytest.fixture()
def _client_with_overrides():
    fake_embed = AsyncMock()
    fake_embed.embed.return_value = FAKE_EMBEDDING
    fake_session = MagicMock()

    app.dependency_overrides[get_session] = lambda: fake_session
    app.dependency_overrides[get_embedding_client] = lambda: fake_embed

    with TestClient(app, raise_server_exceptions=False) as c:
        yield c, fake_session, fake_embed

    app.dependency_overrides.clear()


class TestPublicChatEndpoint:
    def test_short_question_returns_400(self, _client_with_overrides):
        client, *_ = _client_with_overrides
        res = client.post(
            "/api/knowledge/public/chat", json={"question": "hi"}
        )
        assert res.status_code == 400

    def test_rate_limit_headers_present_on_success(self, _client_with_overrides):
        client, fake_session, _ = _client_with_overrides
        store_mock = MagicMock()
        store_mock.search_notes_with_context.return_value = []

        async def _fake_stream(*_a, **_kw):
            yield 'data: {"type":"done"}\n\n'

        with (
            patch("knowledge.router.KnowledgeStore", return_value=store_mock),
            patch("knowledge.router.stream_chat_response", side_effect=_fake_stream),
            patch("knowledge.router.public_limiter") as mock_limiter,
        ):
            mock_limiter.check = AsyncMock(
                return_value=(True, 4, time.time() + 60)
            )
            res = client.post(
                "/api/knowledge/public/chat",
                json={"question": "what are transformers?"},
            )

        assert res.status_code == 200
        assert "x-ratelimit-limit" in res.headers

    def test_rate_limited_returns_429(self, _client_with_overrides):
        client, *_ = _client_with_overrides

        with patch("knowledge.router.public_limiter") as mock_limiter:
            mock_limiter.check = AsyncMock(
                return_value=(False, 0, time.time() + 30)
            )
            res = client.post(
                "/api/knowledge/public/chat",
                json={"question": "what are transformers?"},
            )

        assert res.status_code == 429
        body = res.json()
        assert body["error"] == "rate_limited"
        assert "x-ratelimit-limit" in res.headers
