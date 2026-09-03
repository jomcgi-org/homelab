"""Unit tests for knowledge.indexing (ADR 006 Phase 3).

The shared chunk/embed/upsert pipeline used by both the reconciler and the
write paths. Store + embedder are mocked; chunking, link extraction, and
frontmatter parsing run for real.
"""

from __future__ import annotations

import hashlib
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from knowledge.frontmatter import ParsedFrontmatter
from knowledge.indexing import (
    index_note_best_effort,
    index_note_from_raw,
    index_parsed_note,
    reindex_note_with_edits,
)


def _embed() -> AsyncMock:
    client = AsyncMock()
    client.embed_batch.side_effect = lambda texts: [[0.1] * 1024 for _ in texts]
    return client


class TestIndexParsedNote:
    @pytest.mark.asyncio
    async def test_embeds_chunks_and_upserts_with_body(self):
        store = MagicMock()
        embed = _embed()
        meta = ParsedFrontmatter(title="My Note")
        await index_parsed_note(
            store,
            embed,
            note_id="my-note",
            rel_path="_processed/my-note.md",
            content_hash="h1",
            title="My Note",
            meta=meta,
            authored_body="# Heading\n\nBody text.",
        )
        embed.embed_batch.assert_awaited_once()
        store.upsert_note.assert_called_once()
        kwargs = store.upsert_note.call_args.kwargs
        assert kwargs["note_id"] == "my-note"
        assert kwargs["content"] == "# Heading\n\nBody text."
        assert kwargs["content_hash"] == "h1"
        # One vector per chunk.
        assert len(kwargs["vectors"]) == len(kwargs["chunks"])

    @pytest.mark.asyncio
    async def test_empty_body_still_embeds_one_chunk(self):
        store = MagicMock()
        embed = _embed()
        await index_parsed_note(
            store,
            embed,
            note_id="empty",
            rel_path="empty.md",
            content_hash="h",
            title="Empty",
            meta=ParsedFrontmatter(title="Empty"),
            authored_body="",
        )
        kwargs = store.upsert_note.call_args.kwargs
        # Falls back to a single chunk seeded with the title so search has
        # something to match and the row is never chunk-less.
        assert len(kwargs["chunks"]) == 1
        assert kwargs["chunks"][0]["text"] == "Empty"


class TestIndexNoteFromRaw:
    @pytest.mark.asyncio
    async def test_parses_strips_and_hashes(self):
        store = MagicMock()
        embed = _embed()
        raw = "---\nid: my-note\ntitle: My Note\n---\n\n# Heading\n\nBody text.\n"
        await index_note_from_raw(
            store, embed, note_id="my-note", rel_path="_processed/my-note.md", raw=raw
        )
        kwargs = store.upsert_note.call_args.kwargs
        assert kwargs["note_id"] == "my-note"
        assert kwargs["content_hash"] == hashlib.sha256(raw.encode("utf-8")).hexdigest()
        # Frontmatter stripped from the stored body.
        assert "title:" not in kwargs["content"]
        assert "Body text." in kwargs["content"]


class TestIndexNoteBestEffort:
    @pytest.mark.asyncio
    async def test_returns_true_and_indexes_on_success(self):
        store = MagicMock()
        embed = _embed()
        raw = "---\nid: n\ntitle: N\n---\n\nbody\n"
        ok = await index_note_best_effort(
            store, embed, note_id="n", rel_path="n.md", raw=raw
        )
        assert ok is True
        store.upsert_note.assert_called_once()

    @pytest.mark.asyncio
    async def test_swallows_embed_failure_and_returns_false(self):
        store = MagicMock()
        embed = AsyncMock()
        embed.embed_batch.side_effect = RuntimeError("embedder down")
        raw = "---\nid: n\ntitle: N\n---\n\nbody\n"
        # Must not raise — a flaky embedder cannot fail a user's save.
        ok = await index_note_best_effort(
            store, embed, note_id="n", rel_path="n.md", raw=raw
        )
        assert ok is False
        store.upsert_note.assert_not_called()


@pytest.mark.asyncio
async def test_reindex_omits_legacy_verification_state():
    row = SimpleNamespace(
        note_id="legacy",
        title="Legacy",
        content="Body",
        path="legacy.md",
        type="fact",
        status=None,
        visibility="private",
        source=None,
        scope=None,
        verification_state="legacy",
        confidence=None,
        valid_from=None,
        valid_until=None,
        observed_at=None,
        tags=[],
        aliases=[],
        created_at=None,
        updated_at=None,
        extra={},
    )
    store = MagicMock()
    store.session.exec.return_value.one_or_none.return_value = row
    store.get_note_links.return_value = []

    with patch("knowledge.indexing.index_note_from_raw", new=AsyncMock()) as index:
        await reindex_note_with_edits(store, _embed(), note_id="legacy")

    assert "verification_state" not in index.await_args.kwargs["raw"]
