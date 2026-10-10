"""Grant-filtered vector search over entities and chunks.

Spec #5 of the pg-first design (``GET
/campaigns/{id}/search``): embed the query, over-fetch nearest neighbors from
the generic ``embedding`` table, then apply the same visibility predicate the
entity endpoints use before shaping and trimming to ``k`` results.

``knn_embeddings`` is the only pgvector-specific code in this module, kept
deliberately tiny so tests can stub it directly (SQLite test fixtures have no
cosine_distance operator). All other Session I/O lives in plain sync helper
functions called from ``search_campaign`` (which is ``async def``), mirroring
the split in ``extract.py``/``ingest.py``: no Session I/O is written inline in
an async function body.
"""

from __future__ import annotations

from typing import Any, Protocol

from sqlalchemy.sql.elements import ColumnElement
from sqlmodel import Session, select

from grimoire.audience import Member, can_see, can_see_note
from grimoire.models import Book, Embedding, Entity, KnowledgeChunk, Note, SessionEvent
from grimoire.play_embeddings import (
    event_embedding_kind,
    event_text,
    play_embedding_predicate,
)
from grimoire.visibility import Viewer, project_entity, visible_entities_query

# How many extra candidates to pull past ``k`` before visibility filtering and
# trimming, so grant-invisible hits do not starve the final result set.
OVERFETCH_FACTOR = 4

# Characters of chunk content shown in a search hit preview.
_CHUNK_PREVIEW_LEN = 200


class _Embedder(Protocol):
    async def embed(self, text: str) -> list[float]: ...


def knn_embeddings(
    session: Session,
    query_vector: list[float],
    kinds: tuple[str, ...],
    limit: int,
    model: str | None = None,
    where: ColumnElement[bool] | None = None,
) -> list[tuple[Embedding, float]]:
    """Nearest ``embedding`` rows to ``query_vector`` among ``kinds``.

    Ordered by ascending cosine distance (closest first), capped at
    ``limit``. Deliberately does no visibility filtering or result shaping,
    so it stays a thin, easily stubbed seam over the one pgvector-specific
    operator this module needs.

    ``model``, when supplied, additionally restricts the search to rows stored
    under that embedding model. Cosine distance is only meaningful WITHIN one
    model's vector space, so a caller whose query vector came from a specific
    embedding model can pass that model here to guarantee it never scores its
    query against a vector from a different (incompatible) model. Defaults to
    None (no filter), preserving the single-model behaviour existing callers
    rely on.

    ``where`` is an optional candidate clause composed before ranking and the
    limit. Callers still resolve and authorize every candidate from live rows.
    """
    distance = Embedding.vector.cosine_distance(query_vector)
    stmt = select(Embedding, distance.label("distance")).where(
        Embedding.embeddable_kind.in_(kinds)
    )
    if model is not None:
        stmt = stmt.where(Embedding.model == model)
    if where is not None:
        stmt = stmt.where(where)
    stmt = stmt.order_by(distance.asc()).limit(limit)
    rows = session.execute(stmt).all()
    return [(row[0], float(row[1])) for row in rows]


def _resolve_chunk_hit(
    session: Session, chunk_id: str, distance: float
) -> dict[str, Any] | None:
    """Sync: shape a chunk hit. Chunks are always visible in v1 (global corpus)."""
    chunk = session.get(KnowledgeChunk, chunk_id)
    if chunk is None:
        return None
    # Resolve the book's display name so the omnibox can title chunk hits with a
    # human name (and link into the reader) instead of showing the raw book_id.
    book = session.get(Book, chunk.book_id)
    return {
        "kind": "chunk",
        "id": chunk.id,
        "book_id": chunk.book_id,
        "display_name": book.display_name if book else chunk.book_id,
        "section_path": chunk.section_path,
        "preview": chunk.content[:_CHUNK_PREVIEW_LEN],
        "score": 1.0 - distance,
    }


def _resolve_entity_hit(
    session: Session,
    campaign_id: str,
    viewer: Viewer,
    entity_id: str,
    distance: float,
    not_granted_to: str | None = None,
) -> dict[str, Any] | None:
    """Sync: apply the visibility predicate/projection to one entity hit.

    Builds on visible_entities_query()/project_entity() scoped to a single
    entity_id, the same helpers and pattern router.py's _project_neighbor
    uses, so the grant predicate lives in exactly one place. Returns None
    when the entity does not exist, is not visible to the viewer at all
    (ungranted, non-global), or is a name_only grant (dropped in lookup
    context per spec #3.3).

    The DM view can return multiple (entity, grant) row tuples for the same
    entity (one per grant, per visible_entities_query()'s docstring); these
    are folded into a single result with a "grants" list, mirroring
    router._aggregate_dm_rows without importing it (router.py imports this
    module to wire the endpoint, so importing back would be circular).
    """
    rows = session.exec(
        visible_entities_query(
            campaign_id, viewer, not_granted_to=not_granted_to
        ).where(Entity.id == entity_id)
    ).all()
    if not rows:
        return None

    if viewer == "dm":
        entity = rows[0][0]
        projected = project_entity(entity, None, rows[0][1], viewer, context="lookup")
        projected.pop("grant", None)
        projected["grants"] = [
            {
                "player_character_id": grant.player_character_id,
                "grant_scope": grant.grant_scope,
                "revealed_details": grant.revealed_details,
            }
            for _, grant in rows
            if grant is not None
        ]
    else:
        entity, grant = rows[0]
        projected = project_entity(entity, None, grant, viewer, context="lookup")
        if projected is None:
            return None

    projected["kind"] = "entity"
    projected["score"] = 1.0 - distance
    return projected


def _resolve_hits(
    session: Session,
    campaign_id: str,
    viewer: Viewer,
    hits: list[tuple[Embedding, float]],
    not_granted_to: str | None = None,
) -> list[dict[str, Any]]:
    """Sync: resolve raw kNN hits into scored, visibility-filtered result dicts."""
    results: list[dict[str, Any]] = []
    for embedding, distance in hits:
        if embedding.embeddable_kind == "chunk":
            resolved = _resolve_chunk_hit(session, embedding.embeddable_id, distance)
        elif embedding.embeddable_kind == "entity":
            resolved = _resolve_entity_hit(
                session,
                campaign_id,
                viewer,
                embedding.embeddable_id,
                distance,
                not_granted_to,
            )
        else:
            resolved = None
        if resolved is not None:
            results.append(resolved)
    return results


async def search_campaign(
    session: Session,
    embed_client: _Embedder,
    campaign_id: str,
    viewer: Viewer,
    q: str,
    k: int = 10,
    *,
    not_granted_to: str | None = None,
) -> list[dict[str, Any]]:
    """Grant-filtered vector search over entities and chunks for one campaign.

    Embeds ``q``, over-fetches ``k * OVERFETCH_FACTOR`` nearest neighbors
    across both embeddable kinds, resolves and visibility-filters each hit,
    then sorts by score (descending) and trims to ``k``. The embed call is
    the only ``await`` in this function; all Session I/O lives in the sync
    helpers above (see module docstring).
    """
    query_vector = await embed_client.embed(q)
    hits = knn_embeddings(
        session, query_vector, ("entity", "chunk"), k * OVERFETCH_FACTOR
    )
    results = _resolve_hits(session, campaign_id, viewer, hits, not_granted_to)
    results.sort(key=lambda item: item["score"], reverse=True)
    return results[:k]


def _resolve_knowledge_hits(
    session: Session,
    campaign_id: str,
    viewer: Viewer,
    member: Member,
    hits: list[tuple[Embedding, float]],
) -> list[dict[str, Any]]:
    """Resolve every candidate from live sources, ignoring cached audience copies."""
    results = []
    seen = set()
    for embedding, distance in hits:
        kind, source_id = embedding.embeddable_kind, embedding.embeddable_id
        identity = ("event" if kind == "transcript" else kind, source_id)
        if identity in seen:
            continue
        resolved = None
        if kind == "entity":
            resolved = _resolve_entity_hit(
                session, campaign_id, viewer, source_id, distance
            )
            if resolved is not None:
                resolved["type"] = "entity"
                resolved["source"] = {"entity_id": source_id}
        elif kind == "chunk":
            resolved = _resolve_chunk_hit(session, source_id, distance)
            if resolved is not None:
                resolved["type"] = "chunk"
                resolved["source"] = {
                    "book_id": resolved["book_id"],
                    "chunk_id": source_id,
                }
        elif kind == "note":
            note = session.get(Note, source_id)
            if (
                note is not None
                and note.campaign_id == campaign_id
                and can_see_note(viewer, member, note)
            ):
                resolved = {
                    "type": "note",
                    "id": note.id,
                    "title": note.title,
                    "preview": note.markdown[:_CHUNK_PREVIEW_LEN],
                    "score": 1.0 - distance,
                    "source": {"note_id": note.id},
                }
        elif kind in ("event", "transcript"):
            event = session.get(SessionEvent, source_id)
            if (
                event is not None
                and event.campaign_id == campaign_id
                and can_see(viewer, member, event)
                and event_embedding_kind(event) == kind
            ):
                # This uses the feed's live reveal_items projection, drops
                # recognition-only items and renders partial snapshots safely.
                preview = event_text(event)
                if preview:
                    resolved = {
                        "type": "event",
                        "id": event.id,
                        "session_id": event.session_id,
                        "seq": event.seq,
                        "kind": event.kind,
                        "preview": preview[:_CHUNK_PREVIEW_LEN],
                        "score": 1.0 - distance,
                        "source": {"session_id": event.session_id, "seq": event.seq},
                    }
        if resolved is not None:
            seen.add(identity)
            results.append(resolved)
    return results


async def search_knowledge(
    session: Session,
    embed_client: _Embedder,
    campaign_id: str,
    viewer: Viewer,
    member: Member,
    q: str,
    k: int = 10,
) -> list[dict[str, Any]]:
    """Search visible corpus and play knowledge, with a live-source re-check."""
    query_vector = await embed_client.embed(q)
    hits = knn_embeddings(
        session,
        query_vector,
        ("entity", "chunk", "note", "event", "transcript"),
        k * OVERFETCH_FACTOR,
        where=play_embedding_predicate(campaign_id, viewer, member),
    )
    results = _resolve_knowledge_hits(session, campaign_id, viewer, member, hits)
    results.sort(key=lambda item: item["score"], reverse=True)
    return results[:k]
