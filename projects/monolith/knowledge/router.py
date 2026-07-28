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
from email.utils import format_datetime
from typing import Literal

import yaml
from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from pydantic import BaseModel
from sqlmodel import Session, select

from core.db import get_session
from knowledge.gaps import (
    GapError,
    answer_gap,
    delete_gap,
    list_gaps_for_review,
    reject_gap,
    reopen_gap,
    split_csv,
    undelete_gap,
    verify_gap,
)
from knowledge.gardener import Gardener
from knowledge.http_cache import _as_utc, _graph_etag, _GRAPH_CACHE_CONTROL
from knowledge.indexing import reindex_note_with_edits
from knowledge.ingest_queue import IngestQueueItem, ingest_raw
from knowledge.models import AtomRawProvenance, RawInput
from knowledge.notes import (
    _note_to_review_dict,
    delete_note,
    list_notes_for_review,
    reset_note_visibility,
    resolve_note_body,
    set_note_visibility,
    undelete_note,
    verify_note_visibility,
)
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


@router.get("/notes/review-queue")
def get_notes_review_queue_endpoint(
    mode: Literal["pending", "audit"] = Query(default="pending"),
    limit: int = Query(default=50, ge=1, le=200),
    session: Session = Depends(get_session),
) -> dict:
    """Return notes for the private review page.

    ``mode=pending`` (default) — notes with ``visibility IS NULL``
    (never classified), oldest-created first.

    ``mode=audit`` — notes with a visibility set but
    ``visibility_verified IS FALSE`` (automation classified, human
    hasn't confirmed). Most-recently-updated first.

    Must be declared before the catch-all ``GET /notes/{note_id}`` route
    below — FastAPI matches routes in declaration order, so without this
    ordering, ``GET /notes/review-queue`` would resolve to
    ``get_knowledge_note(note_id="review-queue")`` and 404.
    """
    return {"notes": list_notes_for_review(session, mode=mode, limit=limit)}


@router.get("/notes/{note_id}")
def get_knowledge_note(
    note_id: str,
    session: Session = Depends(get_session),
) -> dict:
    store = KnowledgeStore(session)
    note = store.get_note_by_id(note_id)
    if note is None:
        raise HTTPException(status_code=404, detail="note not found")

    # ADR 006: body of record is Postgres ``content``.
    body = resolve_note_body(note.get("content"))
    if body is None:
        raise HTTPException(status_code=404, detail="note has no body")

    edges = store.get_note_links(note_id)
    return {**note, "content": body, "edges": edges}


@router.delete("/notes/{note_id}")
def delete_note_endpoint(
    note_id: str,
    session: Session = Depends(get_session),
) -> dict:
    """Soft-delete a note by stamping ``deleted_at`` to hide the row.

    Backs the /private/review audit "delete" action. The DB row survives
    so :func:`undelete_note_endpoint` can restore it. Bodies are
    authoritative in Postgres (ADR 006); there is no file to move.

    NOTE: this is a behaviour change from the original hard-delete. The
    response shape used to be ``{"deleted": True, "note_id": ...}``; it
    is now the standard review-dict payload (matching the other
    note-action endpoints), with ``deleted_at`` populated.
    """
    try:
        note = delete_note(session, note_id)
    except ValueError as exc:
        raise _map_note_error(exc) from exc
    return _note_to_review_dict(note)


@router.post("/notes/{note_id}/undelete")
def undelete_note_endpoint(
    note_id: str,
    session: Session = Depends(get_session),
) -> dict:
    """Undo a soft-delete: clear ``deleted_at`` to unhide the row.

    Calling this on a live note returns 404 — the row isn't "not found"
    from the user's perspective, but the error contract reuses the
    Note-not-found mapper for consistency with the other write paths.
    Use the audit-mode review queue + delete action to land here in the
    first place.
    """
    try:
        note = undelete_note(session, note_id)
    except ValueError as exc:
        raise _map_note_error(exc) from exc
    return _note_to_review_dict(note)


@router.put("/notes/{note_id}")
async def edit_note(
    note_id: str,
    data: EditNoteRequest,
    session: Session = Depends(get_session),
    embed_client: EmbeddingClient = Depends(get_embedding_client),
) -> dict:
    """Update an existing note's frontmatter and/or body, then re-index.

    DB-only (ADR 006, Obsidian decommissioned): the shared
    :func:`reindex_note_with_edits` core reconstructs frontmatter from the
    note's Postgres row, merges the provided fields, and re-indexes. The
    same core backs the MCP ``edit_note`` so the two can never drift.
    """
    store = KnowledgeStore(session)
    result = await reindex_note_with_edits(
        store,
        embed_client,
        note_id=note_id,
        title=data.title,
        tags=data.tags,
        content=data.content,
    )
    if result is None:
        raise HTTPException(status_code=404, detail="note not found")
    return result


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
def create_note(
    data: CreateNoteRequest,
    session: Session = Depends(get_session),
) -> dict:
    """Capture a new markdown note straight into the raw-input pipeline.

    This is a capture path, not direct graph-note creation: the frontmatter +
    body is inserted as a ``knowledge.raw_inputs`` row (with the markdown body
    uploaded to ``s3://knowledge/raws/<raw_id>.md``), no files. The gardener
    routine later decomposes it into ``_processed`` atoms, so its content
    reaches the graph via the gardener. It is therefore intentionally NOT
    indexed into ``knowledge.notes`` here. Only ``edit_note``, which mutates an
    existing ``_processed`` note, indexes synchronously (ADR 006 Phase 3).
    """
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

    raw = ingest_raw(session, content=file_content, source=data.source or "capture")
    return {"raw_id": raw.raw_id}


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
    mode: Literal["pending", "audit"] = Query(default="pending"),
    limit: int = Query(default=50, ge=1, le=200),
    session: Session = Depends(get_session),
) -> dict:
    """Return gaps for the private review page.

    ``mode=pending`` (default, back-compat) returns internal/hybrid/external
    gaps awaiting user attention (internal/hybrid await an answer; external
    awaits approval to spend research tokens), oldest first. ``mode=audit``
    returns terminal gaps (committed/rejected/parked) where
    ``human_verified=False``, most-recently-resolved first.

    Each row carries the richer review-dict shape: ``referenced_by_count``
    (how many notes link at the term), ``research_attempts``, ``answer``.
    """
    return {"gaps": list_gaps_for_review(session, mode=mode, limit=limit)}


def _map_gap_error(exc: ValueError) -> HTTPException:
    """Map a gap-lifecycle error to an HTTP error by exception class.

    ``knowledge.gaps`` raises typed :class:`~knowledge.gaps.GapError`
    subclasses, each carrying the ``status_code`` its endpoint should
    surface (404 not-found, 409 wrong-state / not-deleted, 400 otherwise).
    A bare ``ValueError`` that is not a ``GapError`` (should not occur from
    the gap layer) falls back to 400.
    """
    status_code = exc.status_code if isinstance(exc, GapError) else 400
    return HTTPException(status_code=status_code, detail=str(exc))


@router.post("/gaps/{gap_id}/answer")
async def answer_gap_endpoint(
    gap_id: int,
    data: AnswerGapRequest,
    session: Session = Depends(get_session),
) -> dict:
    """Commit a user answer for a gap and emit a personal-tier atom."""
    try:
        return await answer_gap(session, gap_id, data.answer)
    except ValueError as exc:
        raise _map_gap_error(exc) from exc


@router.post("/gaps/{gap_id}/reject")
def reject_gap_endpoint(
    gap_id: int,
    session: Session = Depends(get_session),
) -> dict:
    """Reject a pending gap. Transitions in_review → rejected (pure DB)."""
    try:
        return reject_gap(session, gap_id)
    except ValueError as exc:
        raise _map_gap_error(exc) from exc


@router.post("/gaps/{gap_id}/verify")
def verify_gap_endpoint(
    gap_id: int,
    session: Session = Depends(get_session),
) -> dict:
    """Mark a gap as human-verified. Works on any state; no state change."""
    try:
        return verify_gap(session, gap_id)
    except ValueError as exc:
        raise _map_gap_error(exc) from exc


@router.post("/gaps/{gap_id}/reopen")
def reopen_gap_endpoint(
    gap_id: int,
    session: Session = Depends(get_session),
) -> dict:
    """Reopen a terminal gap (committed/rejected/parked) back to in_review."""
    try:
        return reopen_gap(session, gap_id)
    except ValueError as exc:
        raise _map_gap_error(exc) from exc


@router.delete("/gaps/{gap_id}")
def delete_gap_endpoint(
    gap_id: int,
    session: Session = Depends(get_session),
) -> dict:
    """Soft-delete a gap (pure DB); reappears in queries on undelete."""
    try:
        return delete_gap(session, gap_id)
    except ValueError as exc:
        raise _map_gap_error(exc) from exc


@router.post("/gaps/{gap_id}/undelete")
def undelete_gap_endpoint(
    gap_id: int,
    session: Session = Depends(get_session),
) -> dict:
    """Undo a soft-delete: the gap reappears in queries; stub regenerates lazily."""
    try:
        return undelete_gap(session, gap_id)
    except ValueError as exc:
        raise _map_gap_error(exc) from exc


# ---------------------------------------------------------------------------
# Note visibility review endpoints
# ---------------------------------------------------------------------------


class SetNoteVisibilityRequest(BaseModel):
    visibility: str


def _map_note_error(exc: ValueError) -> HTTPException:
    """Map a :class:`ValueError` from a note function to an HTTP error.

    Parallel to :func:`_map_gap_error`. Notes raise distinct error
    prefixes — kept as a separate mapper instead of generalising because
    the two domains' message shapes diverged enough that a single helper
    would need two lookup tables anyway.
    """
    msg = str(exc)
    if "Note not found" in msg:
        return HTTPException(status_code=404, detail=msg)
    if "visibility is unset" in msg:
        return HTTPException(status_code=409, detail=msg)
    if "visibility must be" in msg:
        return HTTPException(status_code=400, detail=msg)
    return HTTPException(status_code=400, detail=msg)


@router.post("/notes/{note_id}/visibility")
def set_note_visibility_endpoint(
    note_id: str,
    data: SetNoteVisibilityRequest,
    session: Session = Depends(get_session),
) -> dict:
    """Set ``visibility`` (public|private) and mark verified."""
    try:
        note = set_note_visibility(session, note_id, data.visibility)
    except ValueError as exc:
        raise _map_note_error(exc) from exc
    return _note_to_review_dict(note)


@router.post("/notes/{note_id}/verify-visibility")
def verify_note_visibility_endpoint(
    note_id: str,
    session: Session = Depends(get_session),
) -> dict:
    """Mark ``visibility_verified=True``. 409 if visibility is unset."""
    try:
        note = verify_note_visibility(session, note_id)
    except ValueError as exc:
        raise _map_note_error(exc) from exc
    return _note_to_review_dict(note)


@router.post("/notes/{note_id}/reset-visibility")
def reset_note_visibility_endpoint(
    note_id: str,
    session: Session = Depends(get_session),
) -> dict:
    """Clear ``visibility`` and ``visibility_verified``. Sends back to pending."""
    try:
        note = reset_note_visibility(session, note_id)
    except ValueError as exc:
        raise _map_note_error(exc) from exc
    return _note_to_review_dict(note)
