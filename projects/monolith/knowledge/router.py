"""HTTP API for the knowledge search overlay.

Two endpoints back the cmdk-style search UI:

- ``GET /api/knowledge/search`` — embed the query via ``EmbeddingClient`` and
  hand off to ``KnowledgeStore.search_notes_with_context``.
- ``GET /api/knowledge/notes/{note_id}`` — fetch a note by id and read its
  vault markdown off disk so the preview pane can render it.

The embedding client is injected through ``get_embedding_client`` so e2e tests
can override it with a deterministic fake via ``app.dependency_overrides``.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from email.utils import format_datetime
from pathlib import Path
from typing import Literal

import yaml
from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from pydantic import BaseModel
from sqlmodel import Session, select

from app.db import get_session
from knowledge import frontmatter
from knowledge.gaps import answer_gap, list_review_queue, split_csv
from knowledge.gardener import Gardener, _slugify
from knowledge.ingest_queue import IngestQueueItem
from knowledge.models import AtomRawProvenance, RawInput
from knowledge.service import DEFAULT_VAULT_ROOT, VAULT_ROOT_ENV
from knowledge.store import KnowledgeStore
from shared.embedding import EmbeddingClient

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/knowledge", tags=["knowledge"])


def get_embedding_client() -> EmbeddingClient:
    """DI seam for the embedding client.

    Tests override this via ``app.dependency_overrides[get_embedding_client]``
    to inject a deterministic fake.
    """
    return EmbeddingClient()


def _get_vault_root() -> Path:
    """Resolve the vault root from the env (or default), as an absolute path."""
    return Path(os.environ.get(VAULT_ROOT_ENV, DEFAULT_VAULT_ROOT)).resolve()


@router.get("/search")
async def search_knowledge(
    q: str = "",
    type: str | None = Query(default=None),
    limit: int = Query(default=20, ge=1, le=100),
    session: Session = Depends(get_session),
    embed_client: EmbeddingClient = Depends(get_embedding_client),
) -> dict:
    # Mirror the frontend's 2-char debounce threshold: skip the embed call
    # entirely for empty / single-char queries so we never hit the embed
    # service for no reason.
    if len(q) < 2:
        return {"results": []}

    try:
        vector = await embed_client.embed(q)
    except Exception:
        logger.exception("knowledge.search: embedding call failed")
        raise HTTPException(status_code=503, detail="embedding unavailable")

    results = KnowledgeStore(session).search_notes_with_context(
        query_embedding=vector,
        limit=limit,
        type_filter=type,
    )
    return {"results": results}


# Mirrors NOTES_PAGE_CACHE_CONTROL in projects/monolith/frontend/src/lib/cache-headers.js — keep in sync.
_GRAPH_CACHE_CONTROL = (
    "public, s-maxage=3600, stale-while-revalidate=86400, stale-if-error=31536000"
)


def _as_utc(value: datetime | None) -> datetime | None:
    """Coerce a datetime to tz-aware UTC.

    Postgres returns tz-aware values; SQLite (used in tests) can return
    naive ones even though we always write tz-aware UTC. Treat naive
    datetimes as UTC so downstream formatters and ETag stamps are stable
    across both backends.
    """
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _graph_etag(node_count: int, indexed_at: datetime | None) -> str:
    """Stable ETag for a graph payload.

    Combines max(indexed_at) with node count so deletions invalidate even
    when the surviving notes' timestamps don't move.
    """
    stamp = indexed_at.isoformat() if indexed_at is not None else "null"
    return f'"{stamp}-{node_count}"'


@router.get("/graph")
def get_graph(
    request: Request,
    response: Response,
    session: Session = Depends(get_session),
):
    """Return the full knowledge graph for the /notes visualisation.

    Heavily CDN-cached: the gardener mutates the graph on a schedule, so
    1h freshness with 24h SWR is generous and saves repeated DB hits.
    Conditional GETs short-circuit with 304 via ETag/Last-Modified.
    """
    graph = KnowledgeStore(session).get_graph()
    indexed_at = _as_utc(graph.get("indexed_at"))
    etag = _graph_etag(len(graph["nodes"]), indexed_at)
    headers = {"Cache-Control": _GRAPH_CACHE_CONTROL, "ETag": etag}
    if indexed_at is not None:
        headers["Last-Modified"] = format_datetime(indexed_at, usegmt=True)

    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers=headers)

    for key, value in headers.items():
        response.headers[key] = value
    return graph


@router.get("/notes/{note_id}")
def get_knowledge_note(
    note_id: str,
    session: Session = Depends(get_session),
) -> dict:
    store = KnowledgeStore(session)
    note = store.get_note_by_id(note_id)
    if note is None:
        raise HTTPException(status_code=404, detail="note not found")

    vault_root = _get_vault_root()
    resolved = (vault_root / note["path"]).resolve()
    if not resolved.is_relative_to(vault_root) or not resolved.is_file():
        raise HTTPException(status_code=404, detail="vault file missing")

    edges = store.get_note_links(note_id)
    return {**note, "content": resolved.read_text(), "edges": edges}


@router.delete("/notes/{note_id}")
def delete_note_endpoint(
    note_id: str,
    session: Session = Depends(get_session),
) -> dict:
    """Delete a note from the vault and clean up DB records."""
    store = KnowledgeStore(session)
    note = store.get_note_by_id(note_id)
    if note is None:
        raise HTTPException(status_code=404, detail="note not found")

    vault_root = _get_vault_root()
    resolved = (vault_root / note["path"]).resolve()
    if not resolved.is_relative_to(vault_root):
        raise HTTPException(status_code=400, detail="invalid note path")

    # Remove the file if it exists — don't error if already gone.
    if resolved.is_file():
        resolved.unlink()

    # Always clean up DB records (Note, Chunk, NoteLink).
    store.delete_note(note["path"])

    return {"deleted": True, "note_id": note_id}


@router.put("/notes/{note_id}")
def edit_note(
    note_id: str,
    data: EditNoteRequest,
    session: Session = Depends(get_session),
) -> dict:
    """Update an existing note's frontmatter and/or body in the vault."""
    store = KnowledgeStore(session)
    note = store.get_note_by_id(note_id)
    if note is None:
        raise HTTPException(status_code=404, detail="note not found")

    vault_root = _get_vault_root()
    resolved = (vault_root / note["path"]).resolve()
    if not resolved.is_relative_to(vault_root) or not resolved.is_file():
        raise HTTPException(status_code=404, detail="vault file missing")

    existing_raw = resolved.read_text()
    parsed, body = frontmatter.parse(existing_raw)

    # Merge provided fields into the parsed frontmatter
    if data.title is not None:
        parsed.title = data.title
    if data.tags is not None:
        parsed.tags = data.tags
    if data.content is not None:
        body = data.content.strip()

    # Re-serialize frontmatter
    fm_dict: dict = {}
    if parsed.note_id is not None:
        fm_dict["id"] = parsed.note_id
    if parsed.title is not None:
        fm_dict["title"] = parsed.title
    if parsed.type is not None:
        fm_dict["type"] = parsed.type
    if parsed.status is not None:
        fm_dict["status"] = parsed.status
    if parsed.visibility is not None:
        fm_dict["visibility"] = parsed.visibility
    if parsed.source is not None:
        fm_dict["source"] = parsed.source
    if parsed.tags:
        fm_dict["tags"] = parsed.tags
    if parsed.aliases:
        fm_dict["aliases"] = parsed.aliases
    if parsed.edges:
        fm_dict["edges"] = parsed.edges
    if parsed.created is not None:
        fm_dict["created"] = parsed.created.isoformat()
    if parsed.updated is not None:
        fm_dict["updated"] = parsed.updated.isoformat()
    fm_dict.update(parsed.extra)

    fm_str = yaml.dump(fm_dict, default_flow_style=False, sort_keys=False)
    file_content = f"---\n{fm_str}---\n\n{body}\n"
    resolved.write_text(file_content)

    return {"path": note["path"], "note_id": note_id}


class IngestRequest(BaseModel):
    url: str
    source_type: Literal["youtube", "webpage"]


@router.post("/ingest", status_code=201)
def queue_ingest(
    data: IngestRequest,
    session: Session = Depends(get_session),
) -> dict:
    if not data.url.strip():
        raise HTTPException(status_code=400, detail="url is required")
    item = IngestQueueItem(url=data.url.strip(), source_type=data.source_type)
    session.add(item)
    session.commit()
    return {"queued": True}


class EditNoteRequest(BaseModel):
    content: str | None = None
    title: str | None = None
    tags: list[str] | None = None


class CreateNoteRequest(BaseModel):
    content: str
    title: str | None = None
    source: str | None = None
    tags: list[str] | None = None
    type: str | None = None


@router.post("/notes", status_code=201)
def create_note(data: CreateNoteRequest) -> dict:
    """Create a new markdown note in the vault with YAML frontmatter."""
    content = data.content.strip()
    if not content:
        raise HTTPException(status_code=400, detail="content must not be empty")

    title = data.title or content[:60]

    # Build frontmatter dict (only include provided fields)
    fm_dict: dict = {"title": title}
    if data.source is not None:
        fm_dict["source"] = data.source
    if data.tags is not None:
        fm_dict["tags"] = data.tags
    if data.type is not None:
        fm_dict["type"] = data.type

    fm_str = yaml.dump(fm_dict, default_flow_style=False, sort_keys=False)
    file_content = f"---\n{fm_str}---\n\n{content}\n"

    vault_root = _get_vault_root()
    slug = _slugify(title)
    filename = f"{slug}.md"

    # Handle collisions
    dest = vault_root / filename
    counter = 1
    while dest.exists():
        filename = f"{slug}-{counter}.md"
        dest = vault_root / filename
        counter += 1

    dest.write_text(file_content)
    return {"path": filename}


@router.get("/dead-letter")
def list_dead_letters(
    session: Session = Depends(get_session),
) -> dict:
    """List raws that have exhausted all retry attempts."""
    stmt = (
        select(RawInput, AtomRawProvenance)
        .join(AtomRawProvenance, AtomRawProvenance.raw_fk == RawInput.id)
        .where(AtomRawProvenance.derived_note_id == "failed")
        .where(AtomRawProvenance.retry_count >= Gardener._MAX_RETRIES)
    )
    results = session.exec(stmt).all()
    items = [
        {
            "id": raw.id,
            "path": raw.path,
            "source": raw.source,
            "error": prov.error,
            "retry_count": prov.retry_count,
            "last_failed_at": prov.created_at.isoformat(),
        }
        for raw, prov in results
    ]
    return {"items": items}


@router.post("/dead-letter/{raw_id}/replay")
def replay_dead_letter(
    raw_id: int,
    session: Session = Depends(get_session),
) -> dict:
    """Replay a dead-lettered raw by removing its failed provenance row."""
    raw = session.get(RawInput, raw_id)
    if raw is None:
        raise HTTPException(status_code=404, detail="raw not found")

    prov = session.exec(
        select(AtomRawProvenance).where(
            AtomRawProvenance.raw_fk == raw_id,
            AtomRawProvenance.derived_note_id == "failed",
            AtomRawProvenance.retry_count >= Gardener._MAX_RETRIES,
        )
    ).first()
    if prov is None:
        raise HTTPException(status_code=404, detail="raw is not dead-lettered")

    session.delete(prov)
    session.commit()
    return {"replayed": True}


# ---------------------------------------------------------------------------
# Gap lifecycle endpoints
# ---------------------------------------------------------------------------


class AnswerGapRequest(BaseModel):
    answer: str


@router.get("/gaps")
def list_gaps_endpoint(
    state: str | None = Query(default=None),
    gap_class: str | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
    session: Session = Depends(get_session),
) -> dict:
    """List gaps with optional state/class filters."""
    gaps = KnowledgeStore(session).list_gaps(
        states=split_csv(state),
        classes=split_csv(gap_class),
        limit=limit,
    )
    return {"gaps": gaps}


@router.get("/gaps/review-queue")
def get_review_queue_endpoint(
    session: Session = Depends(get_session),
) -> dict:
    """Return internal/hybrid gaps awaiting user review, oldest first."""
    return {"gaps": list_review_queue(session)}


@router.post("/gaps/{gap_id}/answer")
def answer_gap_endpoint(
    gap_id: int,
    data: AnswerGapRequest,
    session: Session = Depends(get_session),
) -> dict:
    """Commit a user answer for a gap and emit a personal-tier atom."""
    vault_root = _get_vault_root()
    try:
        return answer_gap(session, gap_id, data.answer, vault_root)
    except ValueError as exc:
        # TODO(post-mvp): refactor gaps.py to raise typed exceptions
        # (GapNotFoundError, GapWrongStateError, GapAnswerRejectedError) so this
        # error mapping isn't coupled to specific string messages. The router
        # should map by exception class, not str(exc) substring.
        msg = str(exc)
        if "Gap not found" in msg:
            raise HTTPException(status_code=404, detail=msg) from exc
        if "expected 'in_review'" in msg:
            raise HTTPException(status_code=409, detail=msg) from exc
        if "frontmatter terminator" in msg:
            raise HTTPException(status_code=400, detail=msg) from exc
        raise HTTPException(status_code=400, detail=msg) from exc
