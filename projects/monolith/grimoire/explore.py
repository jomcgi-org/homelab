"""EXPLORE lens/scope query predicates (public EXPLORE tab, Phase A).

Lens membership is a union predicate over the existing
``(category, temporality, entity_type)`` spine columns, never a stored flag
(design decision 2 of the explore-tab design):
scope (which slice of the corpus) and lens (how to view that slice) are
orthogonal dials, and a predicate can be recomputed for free if the
underlying data changes, whereas a stored flag would need a backfill every
time a category/temporality/entity_type mapping changes.

``category`` is a STORED generated column (see models._ENTITY_CATEGORY_EXPR),
so these are cheap WHERE clauses, not a join or a Python-side filter, and they
work identically against SQLite test fixtures (the Computed() column derives
under SQLite's create_all too) and Postgres.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import and_, or_
from sqlmodel import Session, select

from grimoire import library, public
from grimoire.models import ChunkEntityMention, Entity, KnowledgeChunk, Relationship


def lens_predicate(lens: str):
    """SQLAlchemy boolean clause selecting the entities in view for ``lens``.

    - "world"  = lore entities, plus historical events/quests (the settled
      backstory, not spoilers still in play).
    - "story"  = every event, any category, any temporality.
    - "quests" = every quest, any category, any temporality.
    - "rules"  = mechanics-category entities, plus spells (spells are
      categorized "lore" by _ENTITY_CATEGORY_EXPR but read as rules content).
    - anything else (including "everything") is unconstrained: a
      tautological clause so callers can AND it into a query uniformly
      without an `if lens != "everything"` branch at every call site.
    """
    if lens == "world":
        return or_(
            Entity.category == "lore",
            and_(
                Entity.entity_type.in_(("event", "quest")),
                Entity.temporality == "historical",
            ),
        )
    if lens == "story":
        return Entity.entity_type == "event"
    if lens == "quests":
        return Entity.entity_type == "quest"
    if lens == "rules":
        return or_(Entity.category == "mechanics", Entity.entity_type == "spell")
    return Entity.id == Entity.id


def _book_roster(book_id: str):
    """Entity-id subquery for every entity mentioned in some chunk of
    ``book_id``. Mirrors the join style _adventure_chunk_join uses (base
    tables, not the grimoire.adventure_entity VIEW), so it works identically
    against SQLite test fixtures and Postgres."""
    return (
        select(ChunkEntityMention.entity_id)
        .join(KnowledgeChunk, KnowledgeChunk.id == ChunkEntityMention.chunk_id)
        .where(KnowledgeChunk.book_id == book_id)
    )


def scope_entity_ids(session: Session, scope: str, lens: str) -> set[str]:
    """Resolve the node set for a (scope, lens) pair.

    ``scope`` is ``"everything"``, ``"adventure:{id}"``, or ``"book:{id}"``.
    Every branch ANDs in ``Entity.is_global`` and ``lens_predicate(lens)`` on
    top of whatever roster restriction the scope implies, so a private
    (campaign-only) entity can never leak into a public EXPLORE graph even if
    it happens to be in an adventure's or book's chunk range.

    The adventure roster is NOT hand-rolled here: it reuses
    ``library.adventure_entities``, which already computes the correct
    seq-range roster via ``_adventure_chunk_join`` (the same helper
    ``list_adventures``/``list_all_adventures`` use), so the seq-range SQL
    lives in exactly one place.
    """
    if scope.startswith("adventure:"):
        adventure_id = scope.split(":", 1)[1]
        adventure = library.adventure_entities(session, adventure_id)
        if adventure is None:
            return set()
        roster_ids = {e["id"] for e in adventure["entities"]}
        if not roster_ids:
            return set()
        query = select(Entity.id).where(
            Entity.id.in_(roster_ids), Entity.is_global, lens_predicate(lens)
        )
    elif scope.startswith("book:"):
        book_id = scope.split(":", 1)[1]
        query = select(Entity.id).where(
            Entity.id.in_(_book_roster(book_id)),
            Entity.is_global,
            lens_predicate(lens),
        )
    else:
        # "everything" (or any other unrecognized scope): no roster
        # restriction, just is_global + lens.
        query = select(Entity.id).where(Entity.is_global, lens_predicate(lens))
    return set(session.exec(query).all())


_LENS_NAMES = ("world", "story", "quests", "rules")


def ego_subgraph(
    session: Session,
    entity_id: str,
    scope: str = "everything",
    lens: str = "world",
) -> dict[str, Any]:
    """Focus entity and its 1-hop neighbors as nodes, edges and lens counts
    for the World canvas's click-to-expand view.

    Mirrors ``public.list_relationships_public``'s is_global gating: a
    non-public focus entity yields an empty graph, and any neighbor that
    isn't ``is_global`` (plus the edge to it) is dropped rather than shown,
    exactly like the private-entity-neighbor drop in that function.

    ``scope``/``lens`` narrow the NEIGHBOR set to ``scope_entity_ids(session,
    scope, lens)``, so a World
    page scoped to one adventure or lensed to "rules" only shows neighbors
    that belong to that slice. The FOCUS node is always kept regardless of
    scope/lens (an entity's own page never hides itself), matching the
    is_global-only gate a caller gets with the defaults
    ``scope="everything", lens="world"``... except "world" is NOT a no-op
    lens (see lens_predicate), so callers wanting the old unfiltered
    every-neighbor behavior should pass lens="everything" explicitly.

    ``lens_counts`` is returned alongside, one count per lens over the
    UNFILTERED is_global neighbor set (via ``scope_entity_ids`` restricted to
    those neighbor ids), so the frontend's lens tabs can grey out lenses with
    no entities in this ego neighborhood.

    The focus node's card carries an extra ``image_chunk_id`` (earliest
    image-bearing chunk mentioning it, via
    ``public.focus_entity_image_chunk_id``) for the detail card's
    illustration; neighbor cards never get this field computed, since a wander
    click only ever focuses one node at a time and computing art for every
    neighbor would turn a bounded ego-graph fetch into an N+1 query.
    """
    focus = session.get(Entity, entity_id)
    if focus is None or not focus.is_global:
        return {
            "nodes": [],
            "edges": [],
            "lens_counts": {name: 0 for name in _LENS_NAMES},
        }

    touching = session.exec(
        select(Relationship).where(
            or_(
                Relationship.from_entity_id == entity_id,
                Relationship.to_entity_id == entity_id,
            )
        )
    ).all()
    neighbor_ids = {
        r.to_entity_id if r.from_entity_id == entity_id else r.from_entity_id
        for r in touching
    }
    neighbor_ids.discard(entity_id)
    all_neighbors_by_id = (
        {
            e.id: e
            for e in session.exec(
                select(Entity).where(Entity.id.in_(neighbor_ids), Entity.is_global)
            ).all()
        }
        if neighbor_ids
        else {}
    )

    lens_counts = _ego_lens_counts(session, set(all_neighbors_by_id))

    if scope == "everything" and lens == "world":
        neighbors_by_id = all_neighbors_by_id
    else:
        scoped_ids = scope_entity_ids(session, scope, lens) & set(all_neighbors_by_id)
        neighbors_by_id = {
            eid: e for eid, e in all_neighbors_by_id.items() if eid in scoped_ids
        }

    visible_ids = {entity_id, *neighbors_by_id}
    edges = [
        {"from": r.from_entity_id, "to": r.to_entity_id, "rel_type": r.rel_type}
        for r in touching
        if r.from_entity_id in visible_ids and r.to_entity_id in visible_ids
    ]
    nodes = public.entity_cards(session, [focus, *neighbors_by_id.values()])
    for node in nodes:
        if node["id"] == entity_id:
            node["image_chunk_id"] = public.focus_entity_image_chunk_id(
                session, entity_id
            )
            break

    return {"nodes": nodes, "edges": edges, "lens_counts": lens_counts}


def _ego_lens_counts(session: Session, neighbor_ids: set[str]) -> dict[str, int]:
    """Count entities per lens within the bounded ego neighbor set.

    Queries lens_predicate once per lens, restricted to neighbor_ids.
    """
    if not neighbor_ids:
        return {name: 0 for name in _LENS_NAMES}
    return {
        name: len(
            session.exec(
                select(Entity.id).where(
                    Entity.id.in_(neighbor_ids), lens_predicate(name)
                )
            ).all()
        )
        for name in _LENS_NAMES
    }
