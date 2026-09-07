"""Public, read-only HTTP API for the knowledge graph.

Holds the public knowledge endpoints so they can be mounted on the
public-only app (``app.main_public``) without pulling in the private-route
module (``knowledge.router``):

- ``GET /api/knowledge/public/graph``: public-only graph (public nodes,
  doubly-public edges).
- ``GET /api/knowledge/public/entities``: entity catalog with public fact counts.
- ``GET /api/knowledge/public/entities/{kind}/{slug}/notes``: one entity chapter.
- ``GET /api/knowledge/public/search``: bounded grep or semantic fact search.
- ``GET /api/knowledge/public/notes/{note_id}``: a single note iff its
  effective visibility is ``public``.

These handlers are also mounted on the private app (see ``knowledge.register``)
so ``/api/knowledge/public/*`` behaves identically there.
"""

from __future__ import annotations

import hashlib
import json
import logging
from email.utils import format_datetime

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from sqlalchemy import case, func, or_
from sqlmodel import Session, select

from core.db import get_session
from knowledge.api import search_public_chunks
from knowledge.gardener import _slugify
from knowledge.http_cache import _as_utc, _graph_etag, _GRAPH_CACHE_CONTROL
from knowledge.notes import resolve_note_body
from knowledge.public_models import (
    PublicEntity,
    PublicNote,
    PublicNoteEntity,
    PublicNoteLink,
)
from knowledge.store import GRAPH_NOTE_TYPES
from knowledge.visibility import strip_private_wikilinks
from shared.embedding import EmbeddingClient

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/knowledge", tags=["knowledge"])

_VERIFICATION_STATES = (
    "legacy",
    "unverified",
    "verified",
    "disputed",
    "invalidated",
)
_RECORD_STATES = frozenset(_VERIFICATION_STATES[1:])


def _record_etag(scope: str, payload: object) -> str:
    encoded = json.dumps(
        {"scope": scope, "payload": payload},
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    digest = hashlib.sha256(encoded.encode()).hexdigest()
    return f'"record-v1-{digest}"'


def _cache_response(
    request: Request,
    response: Response,
    *,
    etag: str,
    last_modified=None,
) -> Response | None:
    headers = {"Cache-Control": _GRAPH_CACHE_CONTROL, "ETag": etag}
    latest = _as_utc(last_modified)
    if latest is not None:
        headers["Last-Modified"] = format_datetime(latest, usegmt=True)
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers=headers)
    for key, value in headers.items():
        response.headers[key] = value
    return None


def _parse_states(value: str) -> tuple[str, ...]:
    states = tuple(
        dict.fromkeys(part.strip() for part in value.split(",") if part.strip())
    )
    if not states or any(state not in _RECORD_STATES for state in states):
        raise HTTPException(status_code=422, detail="invalid verification state")
    return states


def _snippet(content: str | None, limit: int = 220) -> str:
    text = " ".join((content or "").split())
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def _note_entities(session: Session, note_ids: list[str]) -> dict[str, list[dict]]:
    if not note_ids:
        return {}
    rows = session.execute(
        select(
            PublicNoteEntity.note_id,
            PublicEntity.kind,
            PublicEntity.slug,
            PublicEntity.title,
        )
        .join(PublicEntity, PublicEntity.id == PublicNoteEntity.entity_id)
        .where(PublicNoteEntity.note_id.in_(note_ids))
        .order_by(PublicNoteEntity.note_id, PublicEntity.kind, PublicEntity.slug)
    ).all()
    out: dict[str, list[dict]] = {}
    for row in rows:
        item = {"kind": row.kind, "slug": row.slug, "title": row.title}
        values = out.setdefault(row.note_id, [])
        if item not in values:
            values.append(item)
    return out


@router.get("/public/entities")
def get_public_entities(
    request: Request,
    response: Response,
    session: Session = Depends(get_session),
):
    """List public entities with public-note counts by verification state."""
    entities = session.exec(
        select(PublicEntity).order_by(PublicEntity.kind, PublicEntity.slug)
    ).all()
    count_rows = session.execute(
        select(
            PublicNoteEntity.entity_id,
            PublicNoteEntity.verification_state,
            func.count(func.distinct(PublicNoteEntity.note_id)).label("note_count"),
        ).group_by(
            PublicNoteEntity.entity_id,
            PublicNoteEntity.verification_state,
        )
    ).all()
    counts_by_entity: dict[int, dict[str, int]] = {}
    for row in count_rows:
        counts_by_entity.setdefault(row.entity_id, {})[row.verification_state] = int(
            row.note_count
        )

    payload = [
        {
            "id": entity.id,
            "kind": entity.kind,
            "slug": entity.slug,
            "title": entity.title,
            "aliases": list(entity.aliases or []),
            "scope": entity.scope,
            "source": entity.source,
            "created_at": _as_utc(entity.created_at).isoformat(),
            "updated_at": _as_utc(entity.updated_at).isoformat(),
            "note_counts": {
                state: counts_by_entity.get(entity.id, {}).get(state, 0)
                for state in _VERIFICATION_STATES
            },
        }
        for entity in entities
    ]

    latest_entity = max(
        (_as_utc(entity.updated_at) for entity in entities), default=None
    )
    latest_note = session.exec(select(func.max(PublicNoteEntity.note_indexed_at))).one()
    latest_note = _as_utc(latest_note)
    indexed_at = max(
        (value for value in (latest_entity, latest_note) if value is not None),
        default=None,
    )
    linked_note_count = sum(
        sum(counts.values()) for counts in counts_by_entity.values()
    )
    etag = _graph_etag(len(entities) + linked_note_count, indexed_at)
    headers = {"Cache-Control": _GRAPH_CACHE_CONTROL, "ETag": etag}
    if indexed_at is not None:
        headers["Last-Modified"] = format_datetime(indexed_at, usegmt=True)
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers=headers)
    for key, value in headers.items():
        response.headers[key] = value
    logger.info(
        "public.entities.served entities=%d linked_notes=%d",
        len(entities),
        linked_note_count,
    )
    return payload


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
            PublicNote.verification_state,
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
            "verification_state": row.verification_state,
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


@router.get("/public/entities/{kind}/{slug}/notes")
def get_public_entity_notes(
    kind: str,
    slug: str,
    request: Request,
    response: Response,
    state: str = Query(default="verified,unverified", max_length=120),
    limit: int = Query(default=60, ge=1, le=60),
    session: Session = Depends(get_session),
):
    """Return the newest public notes and contradictions for one entity."""
    entity = session.exec(
        select(PublicEntity).where(
            PublicEntity.kind == kind,
            PublicEntity.slug == slug,
        )
    ).one_or_none()
    if entity is None:
        raise HTTPException(status_code=404, detail="Not Found")

    states = _parse_states(state)
    rows = session.execute(
        select(PublicNoteEntity.role, PublicNote)
        .join(PublicNote, PublicNote.note_id == PublicNoteEntity.note_id)
        .where(
            PublicNoteEntity.entity_id == entity.id,
            PublicNote.verification_state.in_(states),
        )
        .order_by(
            case((PublicNoteEntity.role == "subject", 0), else_=1),
            PublicNote.observed_at.desc().nulls_last(),
            PublicNote.indexed_at.desc(),
        )
        .limit(limit * 2)
    ).all()

    notes: list[dict] = []
    note_rows: dict[str, PublicNote] = {}
    for _role, note in rows:
        if note.note_id in note_rows:
            continue
        note_rows[note.note_id] = note
        observed_at = _as_utc(note.observed_at)
        notes.append(
            {
                "note_id": note.note_id,
                "title": note.title,
                "verification_state": note.verification_state,
                "confidence": note.confidence,
                "observed_at": (
                    observed_at.isoformat() if observed_at is not None else None
                ),
                "scope": note.scope,
                "disputed": note.disputed,
                "snippet": _snippet(note.content),
            }
        )
        if len(notes) >= limit:
            break

    selected_ids = set(note_rows)
    selected_slugs = {_slugify(note_id): note_id for note_id in selected_ids}
    contradictions: list[dict] = []
    seen_pairs: set[tuple[str, str]] = set()
    if selected_ids:
        link_rows = session.execute(
            select(PublicNoteLink).where(
                PublicNoteLink.edge_type == "contradicts",
                PublicNoteLink.source.in_(selected_ids),
            )
        ).scalars()
        for link in link_rows:
            target = (
                link.target
                if link.target in selected_ids
                else selected_slugs.get(_slugify(link.target))
            )
            if target is None:
                continue
            pair = tuple(sorted((link.source, target)))
            if pair in seen_pairs or pair[0] == pair[1]:
                continue
            seen_pairs.add(pair)
            a = note_rows[pair[0]]
            b = note_rows[pair[1]]
            contradictions.append(
                {
                    "a": {
                        "note_id": a.note_id,
                        "title": a.title,
                        "verification_state": a.verification_state,
                    },
                    "b": {
                        "note_id": b.note_id,
                        "title": b.title,
                        "verification_state": b.verification_state,
                    },
                }
            )

    payload = {
        "entity": {"kind": entity.kind, "slug": entity.slug, "title": entity.title},
        "notes": notes,
        "contradictions": contradictions,
    }
    latest = max(
        (_as_utc(note.indexed_at) for note in note_rows.values()), default=None
    )
    etag = _record_etag(f"entity-{kind}-{slug}-{','.join(states)}-{limit}", payload)
    cached = _cache_response(request, response, etag=etag, last_modified=latest)
    if cached is not None:
        return cached
    return payload


@router.get("/public/search")
async def search_public_record(
    request: Request,
    response: Response,
    q: str = Query(default="", max_length=200),
    mode: str = Query(default="grep", pattern="^(grep|semantic)$"),
    limit: int = Query(default=30, ge=1, le=50),
    session: Session = Depends(get_session),
):
    """Search public, governed knowledge notes by text or embedding."""
    query = q.strip()
    result_rows: list[dict] = []
    indexed_by_id: dict[str, object] = {}
    if query and mode == "grep":
        pattern = f"%{query}%"
        rows = session.exec(
            select(PublicNote)
            .where(
                PublicNote.verification_state.in_(_RECORD_STATES),
                or_(
                    PublicNote.title.ilike(pattern),
                    PublicNote.content.ilike(pattern),
                ),
            )
            .order_by(
                PublicNote.observed_at.desc().nulls_last(),
                PublicNote.indexed_at.desc(),
            )
            .limit(limit)
        ).all()
        for note in rows:
            indexed_by_id[note.note_id] = note.indexed_at
            result_rows.append(
                {
                    "note_id": note.note_id,
                    "title": note.title,
                    "verification_state": note.verification_state,
                    "disputed": note.disputed,
                }
            )
    elif query:
        client = EmbeddingClient()
        if client.base_url:
            try:
                vector = await client.embed(query)
                semantic_rows = search_public_chunks(session, vector, limit=limit)
            except Exception:  # noqa: BLE001 - search degrades to no matches
                logger.exception("public.record.semantic_search_failed")
                semantic_rows = []
            for row in semantic_rows:
                result_rows.append(
                    {
                        "note_id": row["note_id"],
                        "title": row["title"],
                        "verification_state": row["verification_state"],
                        "disputed": row["disputed"],
                    }
                )

    entities_by_note = _note_entities(session, [row["note_id"] for row in result_rows])
    payload = [
        {**row, "entities": entities_by_note.get(row["note_id"], [])}
        for row in result_rows
    ]
    latest = max((_as_utc(value) for value in indexed_by_id.values()), default=None)
    etag = _record_etag(f"search-{mode}-{query}-{limit}", payload)
    cached = _cache_response(request, response, etag=etag, last_modified=latest)
    if cached is not None:
        return cached
    return payload


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
        "verification_state": note.verification_state,
        "confidence": note.confidence,
        "observed_at": note.observed_at,
        "scope": note.scope,
        "valid_from": note.valid_from,
        "valid_until": note.valid_until,
        "published_at": note.published_at,
        "disputed": note.disputed,
        "body": sanitized,
    }
