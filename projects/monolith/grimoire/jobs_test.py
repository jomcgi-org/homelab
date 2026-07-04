"""Wiring tests for grimoire.jobs: env plumbing into the ingest orchestrators.

Everything external is stubbed (the S3 client builder, the embedding client, the
OpenRouter client, and the async orchestrators themselves). These tests assert
the handler-to-orchestrator wiring only: which bucket/limit reaches the
orchestrator, and that extraction skips cleanly when OPENROUTER_API_KEY is
unset. The orchestrators' own behavior is covered by ingest_test/extract_test.
"""

from __future__ import annotations

import asyncio

import pytest

from grimoire import jobs


def _run(coro):
    return asyncio.run(coro)


class _SpyEmbedClient:
    model = "test-embed-model"


@pytest.fixture(name="stub_clients")
def stub_clients_fixture(monkeypatch):
    """Stub the client builders so no real S3/embedding/OpenRouter is touched."""
    monkeypatch.setattr(jobs, "_embedding_client", lambda: _SpyEmbedClient())
    monkeypatch.setattr(
        "grimoire.ingest.build_s3_client", lambda: object(), raising=True
    )


class TestLoadChunksHandler:
    def test_reads_bucket_env_and_passes_to_load_chunks(
        self, monkeypatch, stub_clients
    ):
        captured: dict = {}

        async def spy_load_chunks(session, s3_client, embed_client, bucket):
            captured["bucket"] = bucket
            captured["embed_client"] = embed_client
            return {"books": 0, "chunks_upserted": 0, "chunks_embedded": 0, "errors": 0}

        monkeypatch.setattr("grimoire.ingest.load_chunks", spy_load_chunks)
        monkeypatch.setenv("GRIMOIRE_S3_BUCKET", "grimoire-test")

        _run(jobs.grimoire_load_chunks(session=None))

        assert captured["bucket"] == "grimoire-test"
        assert isinstance(captured["embed_client"], _SpyEmbedClient)

    def test_defaults_bucket_when_env_unset(self, monkeypatch, stub_clients):
        captured: dict = {}

        async def spy_load_chunks(session, s3_client, embed_client, bucket):
            captured["bucket"] = bucket
            return {"books": 0, "chunks_upserted": 0, "chunks_embedded": 0, "errors": 0}

        monkeypatch.setattr("grimoire.ingest.load_chunks", spy_load_chunks)
        monkeypatch.delenv("GRIMOIRE_S3_BUCKET", raising=False)

        _run(jobs.grimoire_load_chunks(session=None))

        assert captured["bucket"] == "grimoire"


class TestExtractEntitiesHandler:
    def test_skips_when_api_key_unset(self, monkeypatch, stub_clients):
        called = {"extract": False}

        async def spy_extract_chunks(
            session, or_client, embed_client, limit, concurrency
        ):
            called["extract"] = True
            return {}

        monkeypatch.setattr("grimoire.extract.extract_chunks", spy_extract_chunks)
        monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
        monkeypatch.delenv("GRIMOIRE_EXTRACT_BASE_URL", raising=False)

        # Must not raise, and must not touch the orchestrator or clients.
        result = _run(jobs.grimoire_extract_entities(session=None))

        assert result is None
        assert called["extract"] is False

    def test_skips_when_openrouter_base_url_explicit_and_key_unset(
        self, monkeypatch, stub_clients
    ):
        called = {"extract": False}

        async def spy_extract_chunks(
            session, or_client, embed_client, limit, concurrency
        ):
            called["extract"] = True
            return {}

        monkeypatch.setattr("grimoire.extract.extract_chunks", spy_extract_chunks)
        monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
        monkeypatch.setenv(
            "GRIMOIRE_EXTRACT_BASE_URL", "https://openrouter.ai/api/v1/chat/completions"
        )

        result = _run(jobs.grimoire_extract_entities(session=None))

        assert result is None
        assert called["extract"] is False

    def test_runs_keyless_against_local_qwen_base_url(self, monkeypatch, stub_clients):
        captured: dict = {}

        async def spy_extract_chunks(
            session, or_client, embed_client, limit, concurrency
        ):
            captured["ran"] = True
            captured["api_key"] = or_client.api_key
            captured["base_url"] = or_client.base_url
            return {}

        monkeypatch.setattr("grimoire.extract.extract_chunks", spy_extract_chunks)
        monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
        monkeypatch.setenv(
            "GRIMOIRE_EXTRACT_BASE_URL",
            "http://inference.inference.svc.cluster.local:8080/v1/chat/completions",
        )

        result = _run(jobs.grimoire_extract_entities(session=None))

        assert result is None
        assert captured["ran"] is True
        assert captured["api_key"] == ""
        assert (
            captured["base_url"]
            == "http://inference.inference.svc.cluster.local:8080/v1/chat/completions"
        )

    def test_passes_limit_env_and_builds_client_from_key(
        self, monkeypatch, stub_clients
    ):
        captured: dict = {}

        async def spy_extract_chunks(
            session, or_client, embed_client, limit, concurrency
        ):
            captured["limit"] = limit
            captured["concurrency"] = concurrency
            captured["api_key"] = or_client.api_key
            captured["embed_client"] = embed_client
            return {
                "chunks_processed": 0,
                "chunks_failed": 0,
                "entities_created": 0,
                "entities_reused": 0,
                "mentions_created": 0,
                "relationships_created": 0,
                "entities_embedded": 0,
            }

        monkeypatch.setattr("grimoire.extract.extract_chunks", spy_extract_chunks)
        monkeypatch.setenv("OPENROUTER_API_KEY", "sk-test-key")
        monkeypatch.setenv("GRIMOIRE_EXTRACT_LIMIT", "7")
        monkeypatch.delenv("GRIMOIRE_EXTRACT_CONCURRENCY", raising=False)

        _run(jobs.grimoire_extract_entities(session=None))

        assert captured["limit"] == 7
        # Concurrency env unset -> code default.
        assert captured["concurrency"] == jobs.DEFAULT_EXTRACT_CONCURRENCY
        assert captured["api_key"] == "sk-test-key"
        assert isinstance(captured["embed_client"], _SpyEmbedClient)

    def test_passes_concurrency_env_and_falls_back_when_invalid(
        self, monkeypatch, stub_clients
    ):
        captured: dict = {}

        async def spy_extract_chunks(
            session, or_client, embed_client, limit, concurrency
        ):
            captured["concurrency"] = concurrency
            return {}

        monkeypatch.setattr("grimoire.extract.extract_chunks", spy_extract_chunks)
        monkeypatch.setenv("OPENROUTER_API_KEY", "sk-test-key")

        monkeypatch.setenv("GRIMOIRE_EXTRACT_CONCURRENCY", "3")
        _run(jobs.grimoire_extract_entities(session=None))
        assert captured["concurrency"] == 3

        # Non-positive / non-numeric values fall back to the default rather
        # than passing 0 (which would stall) or raising.
        for bad in ("0", "-2", "not-a-number"):
            monkeypatch.setenv("GRIMOIRE_EXTRACT_CONCURRENCY", bad)
            _run(jobs.grimoire_extract_entities(session=None))
            assert captured["concurrency"] == jobs.DEFAULT_EXTRACT_CONCURRENCY

    def test_defaults_limit_when_env_unset(self, monkeypatch, stub_clients):
        captured: dict = {}

        async def spy_extract_chunks(
            session, or_client, embed_client, limit, concurrency
        ):
            captured["limit"] = limit
            return {}

        monkeypatch.setattr("grimoire.extract.extract_chunks", spy_extract_chunks)
        monkeypatch.setenv("OPENROUTER_API_KEY", "sk-test-key")
        monkeypatch.delenv("GRIMOIRE_EXTRACT_LIMIT", raising=False)

        _run(jobs.grimoire_extract_entities(session=None))

        assert captured["limit"] == jobs.DEFAULT_EXTRACT_LIMIT

    def test_invalid_limit_env_falls_back_to_default(self, monkeypatch, stub_clients):
        captured: dict = {}

        async def spy_extract_chunks(
            session, or_client, embed_client, limit, concurrency
        ):
            captured["limit"] = limit
            return {}

        monkeypatch.setattr("grimoire.extract.extract_chunks", spy_extract_chunks)
        monkeypatch.setenv("OPENROUTER_API_KEY", "sk-test-key")
        monkeypatch.setenv("GRIMOIRE_EXTRACT_LIMIT", "not-a-number")

        _run(jobs.grimoire_extract_entities(session=None))

        assert captured["limit"] == jobs.DEFAULT_EXTRACT_LIMIT
