"""Shared note-indexing pipeline (ADR 006 Phase 3).

Chunk + embed + link-extract + upsert for a note body. Lifted out of the
reconciler so the create/edit write paths can index synchronously into
Postgres on every write, rather than waiting for the reconciler's next
vault scan. The reconciler calls :func:`index_parsed_note` for its
normal-note branch so both paths share one code path and cannot drift.

The body of record is ``knowledge.notes.content``; chunks + embeddings
back pgvector search. ``upsert_note`` persists all of it in one committed
transaction, so a successful call leaves the note fully indexed.
"""

from __future__ import annotations

import hashlib
import logging
from pathlib import Path
from typing import Protocol

from knowledge import frontmatter, links, wikilinks
from knowledge.frontmatter import ParsedFrontmatter
from knowledge.store import KnowledgeStore
from knowledge.chunker import chunk_markdown

logger = logging.getLogger(__name__)


class _Embedder(Protocol):
    async def embed_batch(self, texts: list[str]) -> list[list[float]]: ...


async def index_parsed_note(
    store: KnowledgeStore,
    embed_client: _Embedder,
    *,
    note_id: str,
    rel_path: str,
    content_hash: str,
    title: str,
    meta: ParsedFrontmatter,
    authored_body: str,
) -> None:
    """Chunk, embed, extract links, and upsert a non-gap note.

    ``authored_body`` must already have the generated ``## Links`` section
    stripped (the reconciler does this before calling). Mirrors the
    reconciler's normal-note indexing exactly; gap stubs are handled by the
    reconciler's own branch and never reach here.
    """
    chunks = chunk_markdown(authored_body)
    if not chunks:
        chunks = [{"index": 0, "section_header": "", "text": authored_body or title}]
    vectors = await embed_client.embed_batch([c["text"] for c in chunks])
    note_links = links.extract(authored_body)
    store.upsert_note(
        note_id=note_id,
        path=rel_path,
        content_hash=content_hash,
        title=title,
        metadata=meta,
        chunks=chunks,
        vectors=vectors,
        links=note_links,
        content=authored_body,
    )


async def index_note_from_raw(
    store: KnowledgeStore,
    embed_client: _Embedder,
    *,
    note_id: str,
    rel_path: str,
    raw: str,
) -> None:
    """Parse a raw markdown file and index it under ``note_id``.

    Used by the write paths, which already hold the full file content and
    a stable ``note_id`` (the existing id for edits, the collision-resolved
    filename stem for creates). Computes the content hash from ``raw`` so it
    matches what the reconciler would compute for the same bytes on disk.
    """
    meta, body = frontmatter.parse(raw)
    title = meta.title or Path(rel_path).stem
    content_hash = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    authored_body = wikilinks.strip_links_section(body)
    await index_parsed_note(
        store,
        embed_client,
        note_id=note_id,
        rel_path=rel_path,
        content_hash=content_hash,
        title=title,
        meta=meta,
        authored_body=authored_body,
    )


async def index_note_best_effort(
    store: KnowledgeStore,
    embed_client: _Embedder,
    *,
    note_id: str,
    rel_path: str,
    raw: str,
) -> bool:
    """Index on a write path, swallowing failures.

    Returns ``True`` on success. On any error (e.g. the embedder being
    unreachable) it logs and returns ``False``: the disk file is already
    written, so the reconciler will index it on its next scan. A flaky
    embedder must never fail a user's save.
    """
    try:
        await index_note_from_raw(
            store, embed_client, note_id=note_id, rel_path=rel_path, raw=raw
        )
        return True
    except Exception:  # noqa: BLE001 — best-effort; reconciler is the fallback
        logger.exception(
            "knowledge: synchronous index failed for %s; reconciler will retry",
            rel_path,
        )
        return False
