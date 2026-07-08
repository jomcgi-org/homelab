"""Public, no-grants read paths over the Grimoire corpus (Task 2).

The private read paths (router.py, library.py) all take a campaign + viewer
and route every visibility decision through visibility.visible_entities_query()
/ project_entity(), because a campaign's DM can scope a non-global entity down
to "partial" or "name_only" per player character. The public tier has no
campaign, no viewer, and no grants at all, so these functions skip the grant
join entirely rather than calling visible_entities_query() with a synthetic
"everything is global" viewer.

Skipping grants is NOT the same as exposing everything: the entity table mixes
the shared corpus (is_global = true) with campaign-private entities (is_global
= false, created in a session). The private tier gates those with
``is_global OR a grant exists``; the public tier has no grants, so every entity
query here filters to ``is_global`` and never returns a campaign-private
entity. Chunks, mentions, and relationships are likewise projected only through
is_global entities. This module is the only place that assumption lives, so a
future private-tier visibility change cannot silently leak into (or break) the
public surface.

Every function here is a plain sync helper over a Session (no FastAPI, no
async), mirroring library.py: the GET endpoints that call these run as sync
``def`` handlers so FastAPI's threadpool absorbs the blocking DB work.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import and_, func, or_, union_all
from sqlmodel import Session, select

from grimoire.library import _iso, _PREVIEW_LEN
from grimoire.models import (
    ENTITY_DETAIL_MODELS,
    Book,
    ChunkEntityMention,
    Entity,
    EntityCreature,
    EntitySpell,
    KnowledgeChunk,
    Relationship,
)
from grimoire.visibility import _flatten_detail, _SPINE_FIELDS

# Default and max page size for the paged entity list, matching the private
# entity list's bounds (router.list_entities's Query(..., ge=1, le=500)).
DEFAULT_ENTITY_PAGE = 100
MAX_ENTITY_PAGE = 500

# Public entity spine: the full spine minus created_in_session, a private
# game-session UUID that must never reach a public payload (the corpus is public,
# the session that created a homebrew entity is not).
_PUBLIC_SPINE_FIELDS = tuple(f for f in _SPINE_FIELDS if f != "created_in_session")

# Keyset cursor packs (name, id) so a page boundary landing inside a run of
# duplicate names (common in a D&D corpus) does not skip the remaining
# same-named rows. The unit separator never appears in a name or a uuid.
_CURSOR_SEP = "\x1f"


def _encode_cursor(entity: Entity) -> str:
    return f"{entity.name}{_CURSOR_SEP}{entity.id}"


def _decode_cursor(cursor: str) -> tuple[str, str]:
    name, _, ident = cursor.partition(_CURSOR_SEP)
    return name, ident


def get_chunk_public(session: Session, chunk_id: str) -> dict[str, Any] | None:
    """One chunk with full content, image URL, seq neighbours, and ALL
    on-page entity mentions, unprojected (no grants). Mirrors
    library.get_chunk, minus the campaign/viewer plumbing and the
    visible_entities_query() projection: the public tier has nothing to
    filter, so every mention on the chunk is returned as-is.

    Returns None if the chunk is missing (the router 404s).
    """
    chunk = session.get(KnowledgeChunk, chunk_id)
    if chunk is None:
        return None

    prev_id: str | None = None
    next_id: str | None = None
    if chunk.seq is not None:
        prev_id = session.exec(
            select(KnowledgeChunk.id)
            .where(
                KnowledgeChunk.book_id == chunk.book_id,
                KnowledgeChunk.seq < chunk.seq,
            )
            .order_by(KnowledgeChunk.seq.desc())
            .limit(1)
        ).first()
        next_id = session.exec(
            select(KnowledgeChunk.id)
            .where(
                KnowledgeChunk.book_id == chunk.book_id,
                KnowledgeChunk.seq > chunk.seq,
            )
            .order_by(KnowledgeChunk.seq)
            .limit(1)
        ).first()

    mention_rows = session.exec(
        select(
            Entity.id, Entity.name, Entity.entity_type, ChunkEntityMention.mention_text
        )
        .join(ChunkEntityMention, ChunkEntityMention.entity_id == Entity.id)
        .where(ChunkEntityMention.chunk_id == chunk_id, Entity.is_global)
        .order_by(Entity.name)
    ).all()
    entities = [
        {
            "id": entity_id,
            "name": name,
            "entity_type": entity_type,
            "mention_text": mention_text,
        }
        for entity_id, name, entity_type, mention_text in mention_rows
    ]

    chunk_count = session.exec(
        select(func.count())
        .select_from(KnowledgeChunk)
        .where(KnowledgeChunk.book_id == chunk.book_id)
    ).one()

    return {
        "id": chunk.id,
        "book_id": chunk.book_id,
        "content": chunk.content,
        "section_path": chunk.section_path,
        "seq": chunk.seq,
        "chunk_count": chunk_count,
        "image_url": (
            f"/api/grimoire/chunks/{chunk.id}/image" if chunk.image_ref else None
        ),
        "prev_id": prev_id,
        "next_id": next_id,
        "entities": entities,
        "created_at": _iso(chunk.created_at),
    }


def _secondary_fields(entity_type: str, detail_row: Any) -> dict[str, Any]:
    """The entity list's secondary line: creature size/CR, spell level/school,
    location region. NPC has no list-view secondary fields in v1 (spine
    only); other entity types (faction, deity, item) have no detail table at
    all. ``list_entities_public`` only ever passes a location detail_row via
    the EXPLORE node projection (``entity_cards``); its own creature/spell-only
    batching leaves it None for locations, so this is a pure addition.
    """
    if detail_row is None:
        return {}
    if entity_type == "creature":
        return {"size": detail_row.size, "cr": detail_row.cr}
    if entity_type == "spell":
        return {"level": detail_row.level, "school": detail_row.school}
    if entity_type == "location":
        return {"region": detail_row.region}
    return {}


def _entity_card(entity: Entity, detail_row: Any | None) -> dict[str, Any]:
    """One EXPLORE graph node: the spine fields the canvas needs to draw and
    color a node (id, entity_type, name, category, temporality) plus the same
    secondary detail line the public entity list shows. Shared by
    grimoire.explore's subgraph/ego/path endpoints so "what does a node look
    like" lives in exactly one place instead of being re-derived per caller.
    """
    return {
        "id": entity.id,
        "entity_type": entity.entity_type,
        "name": entity.name,
        "category": entity.category,
        "temporality": entity.temporality,
        **_secondary_fields(entity.entity_type, detail_row),
    }


def entity_cards(session: Session, entities: list[Entity]) -> list[dict[str, Any]]:
    """Batch node projection for a set of entities (see _entity_card),
    without an N+1 detail-table query per entity: EXPLORE subgraphs can have
    hundreds of nodes, so detail rows are fetched once per entity_type
    present in the set (mirrors list_entities_public's creature/spell
    batching, generalized over every ENTITY_DETAIL_MODELS type).
    """
    ids_by_type: dict[str, list[str]] = {}
    for entity in entities:
        if entity.entity_type in ENTITY_DETAIL_MODELS:
            ids_by_type.setdefault(entity.entity_type, []).append(entity.id)

    detail_by_id: dict[str, Any] = {}
    for entity_type, ids in ids_by_type.items():
        model = ENTITY_DETAIL_MODELS[entity_type]
        for row in session.exec(select(model).where(model.entity_id.in_(ids))).all():
            detail_by_id[row.entity_id] = row

    return [_entity_card(entity, detail_by_id.get(entity.id)) for entity in entities]


def list_entities_public(
    session: Session,
    *,
    entity_type: str | None = None,
    q: str | None = None,
    limit: int = DEFAULT_ENTITY_PAGE,
    cursor: str | None = None,
) -> dict[str, Any]:
    """All entities, paginated, no grants.

    Default load (no ``q``, no ``entity_type``) orders by relationship DEGREE
    descending so the most-connected entities surface first, since that is the
    most useful entry point into the corpus. Degree is the number of edges
    touching an entity as either endpoint, computed by UNION ALL-ing the
    relationship table's from/to columns and grouping. This mode paginates by
    OFFSET (``cursor`` is a stringified integer offset), because a degree tie
    makes a keyset cursor on (degree, name, id) more trouble than it's worth at
    corpus scale.

    When a search (``q``) or type (``entity_type``) filter is active the order
    reverts to name, keyset-paginated on (name, id) (``cursor`` packs the
    (name, id) of the last row on the previous page): the filtered set is small
    and name order is what a searching user expects.

    Left-joins the typed detail table per entity_type (creature, spell) to add
    the secondary line the private list omits (it stays spine-only);
    location/npc have no secondary fields to add.
    """
    limit = max(1, min(limit, MAX_ENTITY_PAGE))
    # is_global: only the shared corpus is public; campaign-private entities
    # (is_global false) are never listed.
    degree_mode = q is None and entity_type is None

    if degree_mode:
        # An entity's degree = count of relationship rows where it is the
        # from- OR the to-endpoint. UNION ALL (not UNION) so both endpoints of
        # the same edge each count; a self-loop counts twice, matching "number
        # of edge endpoints touching this entity".
        endpoints = union_all(
            select(Relationship.from_entity_id.label("entity_id")),
            select(Relationship.to_entity_id.label("entity_id")),
        ).subquery()
        degrees = (
            select(
                endpoints.c.entity_id.label("entity_id"),
                func.count().label("degree"),
            )
            .group_by(endpoints.c.entity_id)
            .subquery()
        )
        # LEFT JOIN + coalesce so an entity with no edges sorts as degree 0
        # rather than dropping out of the list.
        degree_col = func.coalesce(degrees.c.degree, 0)
        offset = int(cursor) if cursor else 0
        query = (
            select(Entity)
            .outerjoin(degrees, degrees.c.entity_id == Entity.id)
            .where(Entity.is_global)
            .order_by(degree_col.desc(), Entity.name, Entity.id)
            .offset(offset)
            .limit(limit + 1)  # +1 to detect a following page without a 2nd query.
        )
        rows = session.exec(query).all()
        has_more = len(rows) > limit
        rows = rows[:limit]
        next_cursor = str(offset + limit) if has_more else None
    else:
        query = select(Entity).where(Entity.is_global).order_by(Entity.name, Entity.id)
        if entity_type is not None:
            query = query.where(Entity.entity_type == entity_type)
        if q:
            query = query.where(func.lower(Entity.name).contains(q.lower()))
        if cursor is not None:
            cur_name, cur_id = _decode_cursor(cursor)
            query = query.where(
                or_(
                    Entity.name > cur_name,
                    and_(Entity.name == cur_name, Entity.id > cur_id),
                )
            )
        # limit + 1 to detect whether another page follows without a second query.
        query = query.limit(limit + 1)
        rows = session.exec(query).all()
        has_more = len(rows) > limit
        rows = rows[:limit]
        next_cursor = _encode_cursor(rows[-1]) if has_more and rows else None

    creature_ids = [e.id for e in rows if e.entity_type == "creature"]
    spell_ids = [e.id for e in rows if e.entity_type == "spell"]
    creatures_by_id = (
        {
            row.entity_id: row
            for row in session.exec(
                select(EntityCreature).where(EntityCreature.entity_id.in_(creature_ids))
            ).all()
        }
        if creature_ids
        else {}
    )
    spells_by_id = (
        {
            row.entity_id: row
            for row in session.exec(
                select(EntitySpell).where(EntitySpell.entity_id.in_(spell_ids))
            ).all()
        }
        if spell_ids
        else {}
    )

    items = []
    for entity in rows:
        detail_row = creatures_by_id.get(entity.id) or spells_by_id.get(entity.id)
        items.append(
            {
                "id": entity.id,
                "name": entity.name,
                "entity_type": entity.entity_type,
                **_secondary_fields(entity.entity_type, detail_row),
            }
        )

    count_query = select(func.count()).select_from(Entity).where(Entity.is_global)
    if entity_type is not None:
        count_query = count_query.where(Entity.entity_type == entity_type)
    if q:
        count_query = count_query.where(func.lower(Entity.name).contains(q.lower()))
    total = session.exec(count_query).one()

    return {"items": items, "total": total, "next_cursor": next_cursor}


def get_entity_public(session: Session, entity_id: str) -> dict[str, Any] | None:
    """Full spine + typed detail, no grants. None -> 404 (the router raises).

    Reuses visibility._flatten_detail so the typed-column flattening logic
    (getattr over SQLAlchemy column introspection, not model_dump, to survive
    post-commit attribute expiry) lives in exactly one place.
    """
    entity = session.get(Entity, entity_id)
    # A campaign-private entity (is_global false) is not public: treat it as
    # missing so a guessed id cannot fetch it.
    if entity is None or not entity.is_global:
        return None
    detail_model = ENTITY_DETAIL_MODELS.get(entity.entity_type)
    detail = session.get(detail_model, entity_id) if detail_model else None
    spine = {field: getattr(entity, field) for field in _PUBLIC_SPINE_FIELDS}
    return {**spine, **_flatten_detail(detail)}


def list_relationships_public(session: Session, entity_id: str) -> list[dict[str, Any]]:
    """Both directions of every relationship edge touching entity_id, no
    recognition dimming: every neighbor is a full spine (id, name, entity_type).

    Both the source entity and every neighbor must be is_global: a call for a
    campaign-private id returns nothing (so a guessed private id cannot reveal
    its edges), and any private neighbor is dropped from a public entity's list.
    """
    source = session.get(Entity, entity_id)
    if source is None or not source.is_global:
        return []
    outgoing = session.exec(
        select(Relationship).where(Relationship.from_entity_id == entity_id)
    ).all()
    incoming = session.exec(
        select(Relationship).where(Relationship.to_entity_id == entity_id)
    ).all()
    edges = [(rel, "out", rel.to_entity_id) for rel in outgoing] + [
        (rel, "in", rel.from_entity_id) for rel in incoming
    ]

    neighbor_ids = {neighbor_id for _, _, neighbor_id in edges}
    neighbors_by_id = (
        {
            e.id: e
            for e in session.exec(
                select(Entity).where(Entity.id.in_(neighbor_ids), Entity.is_global)
            ).all()
        }
        if neighbor_ids
        else {}
    )

    items: list[dict[str, Any]] = []
    for rel, direction, neighbor_id in edges:
        neighbor = neighbors_by_id.get(neighbor_id)
        if neighbor is None:
            # Dangling edge (neighbor row missing); skip rather than 500.
            continue
        items.append(
            {
                "direction": direction,
                "rel_type": rel.rel_type,
                "entity": {
                    "id": neighbor.id,
                    "name": neighbor.name,
                    "entity_type": neighbor.entity_type,
                },
            }
        )
    return items


def search_public(session: Session, q: str) -> dict[str, Any]:
    """Name + lore search over the whole corpus, no grants.

    Public search is name-only, not the private tier's vector/kNN search
    (search.search_campaign): that path calls out to the embedding inference
    service via knowledge.api.get_embedding_client, and nothing in the public
    tier today (the public knowledge router included) reaches that service
    from the public network path. Rather than wire a new cross-tier
    dependency for this PR, entity hits are a case-insensitive name-contains
    match and lore hits are a case-insensitive chunk-content-contains match,
    both capped to a small result count. See the plan's open question on
    this: promoting to real semantic search is a follow-up once the public
    tier's reachability to the embedding service is verified.
    """
    _MAX_HITS = 20
    needle = q.lower()

    entity_rows = session.exec(
        select(Entity)
        .where(func.lower(Entity.name).contains(needle), Entity.is_global)
        .order_by(Entity.name)
        .limit(_MAX_HITS)
    ).all()
    entities = [
        {"id": e.id, "name": e.name, "entity_type": e.entity_type} for e in entity_rows
    ]

    chunk_rows = session.exec(
        select(KnowledgeChunk)
        .where(func.lower(KnowledgeChunk.content).contains(needle))
        .order_by(KnowledgeChunk.book_id, KnowledgeChunk.seq)
        .limit(_MAX_HITS)
    ).all()
    book_ids = {c.book_id for c in chunk_rows}
    books_by_id = (
        {b.id: b for b in session.exec(select(Book).where(Book.id.in_(book_ids))).all()}
        if book_ids
        else {}
    )
    lore = [
        {
            "chunk_id": c.id,
            "book_id": c.book_id,
            "display_name": (
                books_by_id[c.book_id].display_name
                if c.book_id in books_by_id
                else c.book_id
            ),
            "section_path": c.section_path,
            "preview": c.content[:_PREVIEW_LEN],
        }
        for c in chunk_rows
    ]

    return {"entities": entities, "lore": lore}
