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

from collections import deque
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


def _lens_counts(
    session: Session, scope: str, current_lens: str, current_ids: set[str]
) -> dict[str, int]:
    """Entity count per lens for ``scope``, via ``scope_entity_ids`` per lens
    (id-set sized queries, never a full graph fetch) so the frontend can grey
    out lens buttons with no entities in the current scope. Reuses the
    caller's already-computed ``current_ids`` for ``current_lens`` instead of
    re-querying it, so this is 3 extra queries, not 4."""
    return {
        name: len(current_ids)
        if name == current_lens
        else len(scope_entity_ids(session, scope, name))
        for name in _LENS_NAMES
    }


def scope_subgraph(session: Session, scope: str, lens: str) -> dict[str, Any]:
    """Induced subgraph ``{nodes, edges, lens_counts}`` for a scope + lens:
    the core bulk-load endpoint for the EXPLORE canvas (see plan design
    decision 4, "bulk over N+1" - the client fetches one payload per
    scope/lens change instead of one relationship call per node).

    Nodes are every entity in ``scope_entity_ids(session, scope, lens)``,
    projected via ``public.entity_cards`` (spine + secondary detail, batched).
    Edges are every relationship whose BOTH endpoints are in that node set
    (a true induced subgraph, not "every edge touching any node").

    ``lens_counts`` is ``{world, story, quests, rules}`` -> entity count in
    this scope, so the frontend can grey out lens buttons with zero entities
    without a separate round trip.

    A whole-corpus ``scope="everything"`` can return a large payload; no
    truncation happens here (nodes are never silently dropped), so the
    frontend should default to gallery/adventure scope and treat
    "everything" as an explicit, occasionally-large view.
    """
    ids = scope_entity_ids(session, scope, lens)
    lens_counts = _lens_counts(session, scope, lens, ids)
    if not ids:
        return {"nodes": [], "edges": [], "lens_counts": lens_counts}
    entities = session.exec(select(Entity).where(Entity.id.in_(ids))).all()
    nodes = public.entity_cards(session, entities)
    edges = [
        {"from": r.from_entity_id, "to": r.to_entity_id, "rel_type": r.rel_type}
        for r in session.exec(
            select(Relationship).where(
                Relationship.from_entity_id.in_(ids),
                Relationship.to_entity_id.in_(ids),
            )
        ).all()
    ]
    return {"nodes": nodes, "edges": edges, "lens_counts": lens_counts}


def ego_subgraph(
    session: Session,
    entity_id: str,
    scope: str = "everything",
    lens: str = "world",
) -> dict[str, Any]:
    """Focus entity + its 1-hop neighbors, as the SAME ``{nodes, edges}``
    shape ``scope_subgraph`` returns, so the canvas can merge a click-to-
    expand ("wander") result straight into the current view.

    Mirrors ``public.list_relationships_public``'s is_global gating: a
    non-public focus entity yields an empty graph, and any neighbor that
    isn't ``is_global`` (plus the edge to it) is dropped rather than shown,
    exactly like the private-entity-neighbor drop in that function.

    ``scope``/``lens`` narrow the NEIGHBOR set to ``scope_entity_ids(session,
    scope, lens)``, the same predicate ``scope_subgraph`` uses, so a World
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
    no entities in this ego neighborhood, the same UX ``scope_subgraph``'s
    lens_counts gives the EXPLORE canvas.

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
    """Per-lens entity count over a fixed ego neighbor id set, the ego
    analogue of ``_lens_counts``: that helper re-runs ``scope_entity_ids``
    per lens over a whole scope, but an ego neighborhood is already a small,
    bounded set of ids in hand, so this queries ``lens_predicate`` restricted
    to ``neighbor_ids`` directly (one query per lens, never a full-corpus
    scan) instead of re-deriving a roster from scratch."""
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


# Hop bound for shortest_path's BFS: a D&D corpus graph has a modest
# out-degree per entity, but an unbounded BFS is still a latent runaway query
# on a large/dense future corpus. 6 comfortably covers "six degrees of
# separation" framing (the pathfinding feature's own pitch) while capping
# worst-case work at a fixed number of relationship-table rows.
_MAX_PATH_DEPTH = 6


def shortest_path(
    session: Session, from_id: str, to_id: str, *, max_depth: int = _MAX_PATH_DEPTH
) -> dict[str, Any]:
    """BFS shortest path between two entities over the relationship graph.

    Traverses relationship edges in BOTH directions, restricted to
    ``is_global`` entities only (a private entity can never appear as an
    intermediate hop or an endpoint), bounded to ``max_depth`` hops. Returns
    ``{"path": [...]}`` where each entry is ``{"entity": <card>, "via":
    <rel_type used to reach this entity from the previous one, None for the
    first entry>}``, in order from ``from_id`` to ``to_id``. Returns
    ``{"path": []}`` if either id is missing/non-public, or no path exists
    within the depth bound.

    Kept as a plain in-process BFS (not a recursive SQL CTE): the relationship
    table is loaded once up front and walked in Python, which is simple and
    fast at this corpus's scale (a few thousand entities, modest out-degree).
    ``max_depth`` is the guardrail against that assumption ever becoming
    false: on a much bigger or denser future graph, this bounds the walk to a
    fixed number of hops instead of degrading into an unbounded search.
    """
    from_entity = session.get(Entity, from_id)
    to_entity = session.get(Entity, to_id)
    if (
        from_entity is None
        or to_entity is None
        or not from_entity.is_global
        or not to_entity.is_global
    ):
        return {"path": []}
    if from_id == to_id:
        card = public.entity_cards(session, [from_entity])[0]
        return {"path": [{"entity": card, "via": None}]}

    global_ids = set(session.exec(select(Entity.id).where(Entity.is_global)).all())
    adjacency: dict[str, list[tuple[str, str]]] = {}
    for r in session.exec(select(Relationship)).all():
        if r.from_entity_id not in global_ids or r.to_entity_id not in global_ids:
            continue
        adjacency.setdefault(r.from_entity_id, []).append((r.to_entity_id, r.rel_type))
        adjacency.setdefault(r.to_entity_id, []).append((r.from_entity_id, r.rel_type))

    visited = {from_id}
    # Each queued path is a list of (entity_id, rel_type_used_to_arrive)
    # tuples; the first entry's rel_type is always None.
    queue: deque[list[tuple[str, str | None]]] = deque([[(from_id, None)]])
    while queue:
        path = queue.popleft()
        if len(path) - 1 >= max_depth:
            continue
        current_id, _ = path[-1]
        for neighbor_id, rel_type in adjacency.get(current_id, []):
            if neighbor_id in visited:
                continue
            new_path = path + [(neighbor_id, rel_type)]
            if neighbor_id == to_id:
                ids = [eid for eid, _ in new_path]
                entities_by_id = {
                    e.id: e
                    for e in session.exec(
                        select(Entity).where(Entity.id.in_(ids))
                    ).all()
                }
                cards_by_id = {
                    c["id"]: c
                    for c in public.entity_cards(
                        session, [entities_by_id[eid] for eid in ids]
                    )
                }
                return {
                    "path": [
                        {"entity": cards_by_id[eid], "via": via}
                        for eid, via in new_path
                    ]
                }
            visited.add(neighbor_id)
            queue.append(new_path)
    return {"path": []}
