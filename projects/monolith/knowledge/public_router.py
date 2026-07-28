"""Public, read-only HTTP API for the knowledge graph.

Holds the two public knowledge endpoints so they can be mounted on the
public-only app (``app.main_public``) without pulling in the private-route
module (``knowledge.router``):

- ``GET /api/knowledge/public/graph``: public-only graph (public nodes,
  doubly-public edges).
- ``GET /api/knowledge/public/notes/{note_id}``: a single note iff its
  effective visibility is ``public``.

These handlers are also mounted on the private app (see ``knowledge.register``)
so ``/api/knowledge/public/*`` behaves identically there.
"""

from __future__ import annotations

import logging
from email.utils import format_datetime

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from sqlmodel import Session, select

from core.db import get_session
from knowledge.gardener import _slugify
from knowledge.http_cache import _as_utc, _graph_etag, _GRAPH_CACHE_CONTROL
from knowledge.notes import resolve_note_body
from knowledge.public_models import PublicNote, PublicNoteLink
from knowledge.store import GRAPH_NOTE_TYPES
from knowledge.visibility import strip_private_wikilinks

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/knowledge", tags=["knowledge"])


@router.get("/public/graph")
def get_public_graph(
    request: Request,
    response: Response,
    session: Session = Depends(get_session),
):
    """Public-only knowledge graph: only public nodes, only doubly-public edges.

    Reads the ``public_api`` views (PublicNote / PublicNoteLink), which already
    filter to public, non-deleted rows at the DB layer (the public service runs
    as ``public_reader`` and cannot touch the knowledge schema). The view
    enforces source-public on edges; the target end is still resolved app-side
    against the public node set so a private/dangling target can never appear.
    Same Cache-Control + ETag semantics so the CDN treats this payload
    identically.
    """
    # The view already restricts to public + non-deleted notes; keep the type
    # filter so gap stubs (type='gap') and other non-renderable types stay out,
    # matching get_graph().
    public_note_rows = session.execute(
        select(
            PublicNote.note_id,
            PublicNote.title,
            PublicNote.type,
            PublicNote.indexed_at,
            # The view already COALESCEs layout_x_public/layout_x (and y), so
            # these columns are the public-preferred positions. Keep both
            # nullable in the response; the client handles either.
            PublicNote.layout_x.label("x"),
            PublicNote.layout_y.label("y"),
        ).where(PublicNote.type.in_(list(GRAPH_NOTE_TYPES)))
    ).all()

    public_note_ids = {row.note_id for row in public_note_rows}
    slug_to_note_id = {_slugify(nid): nid for nid in public_note_ids}

    if public_note_ids:
        # The view already enforces source-public + non-deleted on every link.
        # The target end is resolved below against the public slug set (mirrors
        # get_graph's slug->canonical resolution).
        link_rows = session.execute(
            select(
                PublicNoteLink.source,
                PublicNoteLink.target,
                PublicNoteLink.kind,
                PublicNoteLink.edge_type,
            )
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
        # Mirrors get_graph's response: the StatusBar in the SvelteKit page
        # reads this to display "indexed Xm ago". Previously omitted so
        # the public page showed no "indexed" stamp while the private one did.
        "indexed_at": indexed_at.isoformat() if indexed_at is not None else None,
    }


@router.get("/public/notes/{note_id}")
def get_public_note(
    note_id: str,
    session: Session = Depends(get_session),
) -> dict:
    """Return a single note iff its effective visibility is ``public``.

    The 404 response is identical for missing notes AND for notes that
    exist but are private/null-visibility, so the existence of a private
    note must never be observable. The ``public_api`` view returns the row
    only when it is public + non-deleted, so a private note is simply absent
    and collapses into the same 404 as a missing one. Body wikilinks targeting
    non-public notes are stripped to plain text via
    :func:`strip_private_wikilinks`; wikilinks targeting public notes are left
    intact for the frontend renderer to resolve.
    """
    note = session.exec(
        select(PublicNote).where(PublicNote.note_id == note_id)
    ).one_or_none()
    if note is None:
        # Identical 404 for missing and private: never expose existence.
        # The reason is logged but not surfaced in the response.
        logger.info("public.note.404 note_id=%s reason=not_found", note_id)
        raise HTTPException(status_code=404, detail="Not Found")

    # ADR 006: body of record is Postgres ``content`` (Obsidian decommissioned).
    body = resolve_note_body(note.content)
    if body is None:
        # Same identical 404: don't leak that the DB row exists but
        # the body is unavailable.
        logger.info("public.note.404 note_id=%s reason=no_body", note_id)
        raise HTTPException(status_code=404, detail="Not Found")

    # The public service cannot enumerate private notes (it reads only the
    # public_api views), so invert the sanitiser: keep wikilinks that resolve
    # to a known public note, strip everything else (private targets and
    # dangling links) to plain text. The view already excludes private +
    # deleted rows, so this list is exactly the public note set.
    # session.exec on a single-column select yields scalar values directly
    # (SQLModel SelectOfScalar), so these are note_id strings, not Row tuples.
    public_ids = list(session.exec(select(PublicNote.note_id)).all())
    sanitized = strip_private_wikilinks(body, public_ids)

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
