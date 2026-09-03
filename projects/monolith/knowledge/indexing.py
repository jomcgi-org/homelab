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

import yaml
from sqlmodel import select

from knowledge import frontmatter, links, wikilinks
from knowledge.frontmatter import ParsedFrontmatter
from knowledge.models import Note
from knowledge.store import KnowledgeStore
from knowledge.chunker import chunk_markdown

logger = logging.getLogger(__name__)


def _edges_from_links(note_links: list[dict]) -> dict[str, list[str]]:
    """Rebuild the frontmatter ``edges`` map from stored ``note_links`` rows.

    Typed frontmatter edges are persisted as ``NoteLink(kind='edge')`` rows
    (one per target), so grouping the edge rows by ``edge_type`` reconstructs
    the ``{edge_type: [target_id, ...]}`` shape the frontmatter started with.
    Untyped body wikilinks (``kind='link'``) are re-extracted from the body at
    index time and are skipped here.
    """
    edges: dict[str, list[str]] = {}
    for link in note_links:
        if link.get("kind") == "edge" and link.get("edge_type"):
            edges.setdefault(link["edge_type"], []).append(link["target_id"])
    return edges


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
    except Exception:  # noqa: BLE001 — best-effort
        logger.exception(
            "knowledge: synchronous index failed for %s",
            rel_path,
        )
        return False


async def reindex_note_with_edits(
    store: KnowledgeStore,
    embed_client: _Embedder,
    *,
    note_id: str,
    title: str | None = None,
    tags: list[str] | None = None,
    content: str | None = None,
    visibility: str | None = None,
) -> dict | None:
    """Apply field edits to an existing note and re-index it into Postgres.

    The single DB-only edit core shared by the HTTP and MCP ``edit_note``
    paths (ADR 006, Obsidian decommissioned). Reconstructs the note's
    frontmatter from its authoritative Postgres row — promoted columns,
    ``extra``, and typed ``edges`` rebuilt from ``note_links`` — merges the
    provided fields, then re-indexes from the in-memory markdown. No disk.

    Returns ``{"path", "note_id"}`` on success, or ``None`` if no live note
    matches ``note_id`` (so callers can 404). Keeping both edit endpoints on
    this one helper is what prevents the frontmatter-field drift that once
    silently nulled visibility on thousands of notes.
    """
    row = store.session.exec(
        select(Note).where(Note.note_id == note_id, Note.deleted_at.is_(None))
    ).one_or_none()
    if row is None:
        return None

    body = content if content is not None else (row.content or "")
    new_title = title if title is not None else row.title
    new_tags = tags if tags is not None else list(row.tags or [])
    new_visibility = visibility if visibility is not None else row.visibility

    fm_dict: dict = {"id": row.note_id}
    if new_title:
        fm_dict["title"] = new_title
    if row.type:
        fm_dict["type"] = row.type
    if row.status:
        fm_dict["status"] = row.status
    if new_visibility is not None:
        fm_dict["visibility"] = new_visibility
    if row.source:
        fm_dict["source"] = row.source
    if row.scope is not None:
        fm_dict["scope"] = row.scope
    if row.verification_state is not None:
        fm_dict["verification_state"] = row.verification_state
    if row.confidence is not None:
        fm_dict["confidence"] = row.confidence
    if row.valid_from is not None:
        fm_dict["valid_from"] = row.valid_from.isoformat()
    if row.valid_until is not None:
        fm_dict["valid_until"] = row.valid_until.isoformat()
    if row.observed_at is not None:
        fm_dict["observed_at"] = row.observed_at.isoformat()
    if new_tags:
        fm_dict["tags"] = new_tags
    if row.aliases:
        fm_dict["aliases"] = list(row.aliases)
    edges = _edges_from_links(store.get_note_links(note_id))
    if edges:
        fm_dict["edges"] = edges
    if row.created_at is not None:
        fm_dict["created"] = row.created_at.isoformat()
    if row.updated_at is not None:
        fm_dict["updated"] = row.updated_at.isoformat()
    # Non-promoted frontmatter (e.g. source_tier, derived_from_raw) lives in
    # the ``extra`` JSONB column; re-emit it without clobbering promoted keys.
    for key, value in (row.extra or {}).items():
        fm_dict.setdefault(key, value)

    fm_str = yaml.dump(fm_dict, default_flow_style=False, sort_keys=False)
    raw = f"---\n{fm_str}---\n\n{body}\n"
    await index_note_from_raw(
        store, embed_client, note_id=note_id, rel_path=row.path, raw=raw
    )
    return {"path": row.path, "note_id": note_id}
