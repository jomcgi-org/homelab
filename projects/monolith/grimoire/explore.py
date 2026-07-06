"""EXPLORE lens/scope query predicates (public EXPLORE tab, Phase A).

Lens membership is a union predicate over the existing
``(category, temporality, entity_type)`` spine columns, never a stored flag
(see docs/plans/2026-07-05-grimoire-explore-tab.md design decision 2):
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

from sqlalchemy import and_, or_

from grimoire.models import Entity


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
