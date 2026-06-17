"""Public, read-only HTTP API for the knowledge graph.

Holds the two public knowledge endpoints so they can be mounted on the
public-only app (``app.main_public``) without pulling in the private-route
module (``knowledge.router``):

- ``GET /api/knowledge/public/graph`` — public-only graph (public nodes,
  doubly-public edges).
- ``GET /api/knowledge/public/notes/{note_id}`` — a single note iff its
  effective visibility is ``public``.

These handlers are also mounted on the private app (see ``knowledge.register``)
so ``/api/knowledge/public/*`` behaves identically there.
"""

from __future__ import annotations

import logging
from email.utils import format_datetime

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from sqlalchemy import func, or_
from sqlmodel import Session, select

from app.db import get_session
from knowledge.gardener import _slugify
from knowledge.http_cache import _as_utc, _graph_etag, _GRAPH_CACHE_CONTROL
from knowledge.models import Note, NoteLink
from knowledge.notes import resolve_note_body
from knowledge.service import get_vault_root
from knowledge.store import GRAPH_NOTE_TYPES
from knowledge.visibility import (
    effective_visibility,
    public_notes_filter,
    sanitize_public_body,
)
from knowledge.visibility import _slugify as _visibility_slugify

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/knowledge", tags=["knowledge"])


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

    # ADR 006 Phase 2: body of record is Postgres ``content``. The vault
    # read is a fallback only while the Phase 1 backfill is in flight, and
    # ``resolve_note_body`` keeps the same path-traversal guard so a
    # malformed path can't escape the vault root.
    vault_root = get_vault_root()
    body = resolve_note_body(note.content, note.path, vault_root)
    if body is None:
        # Same identical 404 — don't leak that the DB row exists but
        # the body is unavailable.
        logger.info("public.note.404 note_id=%s reason=vault_file_missing", note_id)
        raise HTTPException(status_code=404, detail="Not Found")

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
