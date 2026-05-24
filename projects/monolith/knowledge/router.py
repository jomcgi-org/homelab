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
import re
from datetime import datetime, timezone
from email.utils import format_datetime
from pathlib import Path
from typing import Literal

import yaml
from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from pydantic import BaseModel
from sqlalchemy import func, or_
from sqlmodel import Session, select

from app.db import get_session
from knowledge import frontmatter
from knowledge.gaps import (
    answer_gap,
    delete_gap,
    list_gaps_for_review,
    reject_gap,
    reopen_gap,
    split_csv,
    undelete_gap,
    verify_gap,
)
from knowledge.gardener import Gardener, _slugify
from knowledge.ingest_queue import IngestQueueItem
from knowledge.models import AtomRawProvenance, Note, NoteLink, RawInput
from knowledge.notes import (
    _note_to_review_dict,
    delete_note,
    list_notes_for_review,
    reset_note_visibility,
    set_note_visibility,
    undelete_note,
    verify_note_visibility,
)
from knowledge.service import DEFAULT_VAULT_ROOT, VAULT_ROOT_ENV
from knowledge.store import GRAPH_NOTE_TYPES, KnowledgeStore
from knowledge.visibility import (
    effective_visibility,
    public_notes_filter,
    sanitize_public_body,
)
from knowledge.visibility import _slugify as _visibility_slugify
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


@router.get("/public/graph")
def get_public_graph(
    request: Request,
    response: Response,
    session: Session = Depends(get_session),
):
    """Public-only knowledge graph: only public nodes, only doubly-public edges.

    Mirrors :func:`get_graph` but applies ``public_notes_filter()`` to both
    ends of every edge so a private note can never appear as a node *or* a
    target. Same Cache-Control + ETag semantics so the CDN treats this
    payload identically.
    """
    # Only public notes whose type is in the renderable graph set. Gap
    # stubs (type='gap') with NULL visibility are excluded by the
    # visibility filter alone, but this also keeps the type filter
    # consistent with get_graph().
    public_note_rows = session.execute(
        select(
            Note.note_id,
            Note.title,
            Note.type,
            Note.indexed_at,
            # Prefer public-only layout positions (computed over the public
            # subgraph by knowledge.service._run_public_layout_pass); fall
            # back to the full-graph positions if the public pass hasn't
            # populated this row yet, so a fresh deploy never serves NULL
            # coords. Same shape consumed by /private/notes — keep both as
            # nullable in the response, the client handles either.
            func.coalesce(Note.layout_x_public, Note.layout_x).label("x"),
            func.coalesce(Note.layout_y_public, Note.layout_y).label("y"),
        )
        .where(public_notes_filter())
        .where(Note.type.in_(list(GRAPH_NOTE_TYPES)))
        .where(Note.deleted_at.is_(None))
    ).all()

    public_note_ids = {row.note_id for row in public_note_rows}
    slug_to_note_id = {_slugify(nid): nid for nid in public_note_ids}

    if public_note_ids:
        # Single SQL join enforces both-ends-public: source side via the
        # join filter, target side via the IN clause against the resolved
        # public slug set (mirrors get_graph's slug→canonical resolution).
        link_rows = session.execute(
            select(
                Note.note_id.label("source"),
                NoteLink.target_id.label("target"),
                NoteLink.kind,
                NoteLink.edge_type,
            )
            .join(Note, NoteLink.src_note_fk == Note.id)
            .where(public_notes_filter())
            .where(Note.deleted_at.is_(None))
        ).all()
    else:
        link_rows = []

    edges: list[dict] = []
    for row in link_rows:
        canonical_target = slug_to_note_id.get(_slugify(row.target))
        if canonical_target is None:
            continue
        edges.append(
            {
                "source": row.source,
                "target": canonical_target,
                "kind": row.kind,
                "edge_type": row.edge_type,
            }
        )

    degree_by_note_id: dict[str, int] = {}
    for edge in edges:
        degree_by_note_id[edge["source"]] = degree_by_note_id.get(edge["source"], 0) + 1
        degree_by_note_id[edge["target"]] = degree_by_note_id.get(edge["target"], 0) + 1

    nodes = [
        {
            "id": row.note_id,
            "title": row.title,
            "type": row.type,
            "degree": degree_by_note_id.get(row.note_id, 0),
            "x": row.x,
            "y": row.y,
        }
        for row in public_note_rows
    ]

    indexed_at = _as_utc(
        max((row.indexed_at for row in public_note_rows), default=None)
    )
    etag = _graph_etag(len(public_note_rows), indexed_at)
    headers = {"Cache-Control": _GRAPH_CACHE_CONTROL, "ETag": etag}
    if indexed_at is not None:
        headers["Last-Modified"] = format_datetime(indexed_at, usegmt=True)

    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers=headers)

    for key, value in headers.items():
        response.headers[key] = value
    logger.info("public.graph.served nodes=%d edges=%d", len(nodes), len(edges))
    return {
        "nodes": nodes,
        "edges": edges,
        # Mirrors get_graph's response — StatusBar in the SvelteKit page
        # reads this to display "indexed Xm ago". Previously omitted so
        # the public page showed "indexed —" while the private one didn't.
        "indexed_at": indexed_at.isoformat() if indexed_at is not None else None,
    }


@router.get("/public/notes/{note_id}")
def get_public_note(
    note_id: str,
    session: Session = Depends(get_session),
) -> dict:
    """Return a single note iff its effective visibility is ``public``.

    The 404 response is identical for missing notes AND for notes that
    exist but are private/null-visibility — the existence of a private
    note must never be observable. Body wikilinks targeting private
    notes are stripped to plain text via :func:`sanitize_public_body`;
    wikilinks targeting public notes are left intact for the frontend
    renderer to resolve.
    """
    note = session.exec(
        select(Note).where(Note.note_id == note_id).where(Note.deleted_at.is_(None))
    ).one_or_none()
    if note is None or effective_visibility(note) != "public":
        # Identical 404 for missing and private — never expose existence.
        # The reason is logged but not surfaced in the response.
        reason = "not_found" if note is None else "private_gated"
        logger.info("public.note.404 note_id=%s reason=%s", note_id, reason)
        raise HTTPException(status_code=404, detail="Not Found")

    # Load the body from the vault. Reuse the same path-traversal guard
    # the private GET /notes/{id} uses so a malformed path can't escape
    # the vault root.
    vault_root = _get_vault_root()
    resolved = (vault_root / note.path).resolve()
    if not resolved.is_relative_to(vault_root) or not resolved.is_file():
        # Same identical 404 — don't leak that the DB row exists but
        # the file is missing/escaped.
        logger.info("public.note.404 note_id=%s reason=vault_file_missing", note_id)
        raise HTTPException(status_code=404, detail="Not Found")
    raw = resolved.read_text()
    _, body = frontmatter.parse(raw)

    # Slugified ids of every non-public note. ``sanitize_public_body``
    # slugifies wikilink display text via ``visibility._slugify`` and
    # drops the bracketing on any match — use the same helper here so
    # the sets compare consistently. Note: ``or_(... != 'public', ...
    # IS NULL)`` is required because in SQL ``NULL != 'public'`` is
    # ``NULL``, not true, so a bare ``!=`` would silently leave NULL-
    # visibility notes out of the private set.
    private_slugs = {
        _visibility_slugify(n.note_id)
        for n in session.exec(
            select(Note)
            .where(or_(Note.visibility != "public", Note.visibility.is_(None)))
            .where(Note.deleted_at.is_(None))
        ).all()
    }
    sanitized = sanitize_public_body(body, private_slugs)

    indexed_at = _as_utc(note.indexed_at)
    logger.info("public.note.served note_id=%s", note_id)
    return {
        "note_id": note.note_id,
        "title": note.title,
        "tags": list(note.tags or []),
        "aliases": list(note.aliases or []),
        "indexed_at": indexed_at.isoformat() if indexed_at is not None else None,
        "body": sanitized,
    }


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
    vault_root = _get_vault_root()
    return {
        "notes": list_notes_for_review(
            session, mode=mode, limit=limit, vault_root=vault_root
        )
    }


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
    """Soft-delete a note. Moves the file to ``_trash/`` and hides the row.

    Backs the /private/review audit "delete" action. The DB row survives
    so :func:`undelete_note_endpoint` can restore it; the on-disk file
    is moved to ``_trash/<ts>-<slug>.md`` where both the gardener and
    raw-ingest scanners skip it. There is no auto-purge of ``_trash/``
    today — the user can hand-clean periodically.

    NOTE: this is a behaviour change from the original hard-delete. The
    response shape used to be ``{"deleted": True, "note_id": ...}``; it
    is now the standard review-dict payload (matching the other
    note-action endpoints), with ``deleted_at`` populated.
    """
    vault_root = _get_vault_root()
    try:
        note = delete_note(session, note_id, vault_root)
    except ValueError as exc:
        raise _map_note_error(exc) from exc
    return _note_to_review_dict(note, vault_root)


@router.post("/notes/{note_id}/undelete")
def undelete_note_endpoint(
    note_id: str,
    session: Session = Depends(get_session),
) -> dict:
    """Undo a soft-delete: restore the file from ``_trash/`` and unhide the row.

    Calling this on a live note returns 404 — the row isn't "not found"
    from the user's perspective, but the error contract reuses the
    Note-not-found mapper for consistency with the other write paths.
    Use the audit-mode review queue + delete action to land here in the
    first place.
    """
    vault_root = _get_vault_root()
    try:
        note = undelete_note(session, note_id, vault_root)
    except ValueError as exc:
        raise _map_note_error(exc) from exc
    return _note_to_review_dict(note, vault_root)


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
    mode: Literal["pending", "audit"] = Query(default="pending"),
    limit: int = Query(default=50, ge=1, le=200),
    session: Session = Depends(get_session),
) -> dict:
    """Return gaps for the private review page.

    ``mode=pending`` (default, back-compat) returns internal/hybrid gaps
    awaiting a user answer, oldest first. ``mode=audit`` returns terminal
    gaps (committed/rejected/parked) where ``human_verified=False``,
    most-recently-resolved first.

    Each row carries the richer review-dict shape: ``referenced_by_count``
    (how many notes link at the term), ``research_attempts``, ``answer``,
    plus a ``stub_body`` read from ``_researching/<slug>.md`` so the
    audit UI can render in-place without a per-row round-trip.
    """
    vault_root = _get_vault_root()
    return {
        "gaps": list_gaps_for_review(
            session, mode=mode, limit=limit, vault_root=vault_root
        )
    }


def _map_gap_error(exc: ValueError) -> HTTPException:
    """Map a :class:`ValueError` from a gap function to an HTTP error.

    Centralises the string-prefix mapping so reject/verify/reopen/answer
    all surface the same status codes. The post-MVP TODO on
    ``answer_gap_endpoint`` (typed exceptions) applies here too — once
    gaps.py raises typed errors, this helper collapses to an
    ``isinstance`` dispatch.
    """
    msg = str(exc)
    if "Gap not found" in msg:
        return HTTPException(status_code=404, detail=msg)
    if "is not deleted" in msg:
        # Calling undelete on a live row is a UI bug, not "not found"
        # — surface a distinct 409 so the frontend can react sensibly.
        return HTTPException(status_code=409, detail=msg)
    if re.search(
        r"\bexpected\b", msg
    ):  # "expected 'in_review'" / "expected one of [...]"
        # \b word boundary so "unexpected" (substring) doesn't false-match.
        return HTTPException(status_code=409, detail=msg)
    if "frontmatter terminator" in msg:
        return HTTPException(status_code=400, detail=msg)
    return HTTPException(status_code=400, detail=msg)


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
        raise _map_gap_error(exc) from exc


@router.post("/gaps/{gap_id}/reject")
def reject_gap_endpoint(
    gap_id: int,
    session: Session = Depends(get_session),
) -> dict:
    """Reject a pending gap. Transitions in_review → rejected and tombstones the stub."""
    vault_root = _get_vault_root()
    try:
        return reject_gap(session, gap_id, vault_root)
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
    """Soft-delete a gap. Hard-deletes the stub file; regenerable on undelete."""
    vault_root = _get_vault_root()
    try:
        return delete_gap(session, gap_id, vault_root)
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
    vault_root = _get_vault_root()
    try:
        note = set_note_visibility(session, note_id, data.visibility, vault_root)
    except ValueError as exc:
        raise _map_note_error(exc) from exc
    return _note_to_review_dict(note, vault_root)


@router.post("/notes/{note_id}/verify-visibility")
def verify_note_visibility_endpoint(
    note_id: str,
    session: Session = Depends(get_session),
) -> dict:
    """Mark ``visibility_verified=True``. 409 if visibility is unset."""
    vault_root = _get_vault_root()
    try:
        note = verify_note_visibility(session, note_id)
    except ValueError as exc:
        raise _map_note_error(exc) from exc
    return _note_to_review_dict(note, vault_root)


@router.post("/notes/{note_id}/reset-visibility")
def reset_note_visibility_endpoint(
    note_id: str,
    session: Session = Depends(get_session),
) -> dict:
    """Clear ``visibility`` and ``visibility_verified``. Sends back to pending."""
    vault_root = _get_vault_root()
    try:
        note = reset_note_visibility(session, note_id, vault_root)
    except ValueError as exc:
        raise _map_note_error(exc) from exc
    return _note_to_review_dict(note, vault_root)
