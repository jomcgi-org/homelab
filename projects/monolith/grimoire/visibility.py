"""Grant-overlay visibility predicate and scope projection.

Implements the Grimoire read predicate
(projects/monolith/ARCHITECTURE.md, section 6) and the spec's visibility semantics
(the pg-first design #3.3). Every read path
(entity lookup, relationships, vector search) is meant to build its query on
top of visible_entities_query() and shape its response with project_entity(),
so the visibility rules live in exactly one place.

Pure functions over the SQLModel models only, no FastAPI imports; router
code in later tasks composes this module.
"""

from typing import Any, Literal

from sqlmodel import Session, SQLModel, and_, or_, select

from grimoire.models import Entity, GameSession, KnowledgeGrant

Viewer = str | None  # "dm", a player_character_id, or None before association
Context = Literal["lookup", "relationship"]

# Spine identity fields returned even for the most restrictive scope
# (partial/name_only): enough to name and link the entity, nothing typed.
_SPINE_IDENTITY_FIELDS = ("id", "entity_type", "name")

# Full spine, returned alongside typed detail for "full" visibility.
_SPINE_FIELDS = (
    "id",
    "entity_type",
    "name",
    "source_type",
    "is_global",
    "source_book",
    "created_in_session",
    "created_at",
)


def visible_entities_query(campaign_id: str, viewer: Viewer):
    """Builds the grant-overlay predicate query for a campaign.

    Player view (viewer is a player_character_id): LEFT JOIN knowledge_grant
    scoped to (this entity, this campaign, this player), keep rows where the
    entity is_global or a grant exists. This is the base predicate from ADR
    011: `is_global OR g.id IS NOT NULL`.

    DM view (viewer == "dm"): global corpus entities plus private entities
    associated with this campaign by a grant or by their creating game session.
    Grants reach the caller as annotations. The campaign-scoped outer join can
    yield one row per grant; callers that need one row per entity aggregate it.

    Returns a select() yielding (Entity, KnowledgeGrant | None) row tuples.
    """
    if viewer == "dm":
        join_condition = KnowledgeGrant.entity_id == Entity.id
        grant_filter = KnowledgeGrant.campaign_id == campaign_id
        campaign_session_ids = select(GameSession.id).where(
            GameSession.campaign_id == campaign_id
        )
        return (
            select(Entity, KnowledgeGrant)
            .join(
                KnowledgeGrant,
                and_(join_condition, grant_filter),
                isouter=True,
            )
            .where(
                or_(
                    Entity.is_global,
                    KnowledgeGrant.id.is_not(None),
                    Entity.created_in_session.in_(campaign_session_ids),
                )
            )
        )

    join_condition = and_(
        KnowledgeGrant.entity_id == Entity.id,
        KnowledgeGrant.player_character_id == viewer,
        KnowledgeGrant.campaign_id == campaign_id,
    )
    query = (
        select(Entity, KnowledgeGrant)
        .join(KnowledgeGrant, join_condition, isouter=True)
        .where(or_(Entity.is_global, KnowledgeGrant.id.is_not(None)))
    )
    return query


def entity_belongs_to_campaign(
    session: Session, campaign_id: str, entity: Entity
) -> bool:
    """Whether a DM may use an entity in a campaign-scoped mutation."""
    if entity.is_global:
        return True
    if entity.created_in_session is not None:
        game_session = session.exec(
            select(GameSession.id).where(
                GameSession.id == entity.created_in_session,
                GameSession.campaign_id == campaign_id,
            )
        ).first()
        if game_session is not None:
            return True
    grant = session.exec(
        select(KnowledgeGrant.id).where(
            KnowledgeGrant.entity_id == entity.id,
            KnowledgeGrant.campaign_id == campaign_id,
        )
    ).first()
    return grant is not None


def _flatten_detail(detail: SQLModel | None) -> dict[str, Any]:
    if detail is None:
        return {}
    # Flatten via SQLAlchemy column introspection (a getattr per column), not
    # detail.model_dump(). model_dump reads the instance __dict__ directly, so
    # it silently omits any attribute SQLAlchemy has expired (which it does for
    # every column after a commit, e.g. the seeding fixtures), yielding a dict
    # missing every typed detail field. getattr goes through the ORM descriptor
    # and triggers a refresh, so it returns the real value even post-expiry -
    # the same reason the spine fields (read via getattr above) always survived.
    columns = detail.__table__.columns.keys()
    return {name: getattr(detail, name) for name in columns if name != "entity_id"}


def project_entity(
    entity: Entity,
    detail: SQLModel | None,
    grant: KnowledgeGrant | None,
    viewer: Viewer,
    context: Context = "lookup",
) -> dict[str, Any] | None:
    """Projects an entity (+ typed detail, + grant) to the dict a caller may
    see, per the scope rules in ADR 011 / spec #3.3.

    - dm: full spine + detail fields, plus a grant annotation if one is
      passed in (None otherwise).
    - player, is_global with no grant, or grant_scope == "full": spine +
      detail fields (a global entity nobody has scoped down is fully
      visible; an explicit "full" grant on a non-global entity is the same).
    - grant_scope == "partial": spine identity fields only, plus
      revealed_details. No typed detail columns.
    - grant_scope == "name_only": recognition only, not retrieval. Returns
      None in "lookup" context (suppressed from direct lookup and vector
      search per the spec); returns a name-only stub in "relationship"
      context so neighbor listings can still say "you recognize this name".
    """
    spine = {field: getattr(entity, field) for field in _SPINE_FIELDS}

    if viewer == "dm":
        result = {**spine, **_flatten_detail(detail)}
        result["grant"] = (
            None
            if grant is None
            else {
                "player_character_id": grant.player_character_id,
                "grant_scope": grant.grant_scope,
                "revealed_details": grant.revealed_details,
            }
        )
        return result

    scope = grant.grant_scope if grant is not None else None

    if scope is None or scope == "full":
        return {**spine, **_flatten_detail(detail)}

    if scope == "partial":
        identity = {field: getattr(entity, field) for field in _SPINE_IDENTITY_FIELDS}
        return {**identity, "revealed_details": grant.revealed_details}

    if scope == "name_only":
        if context == "lookup":
            return None
        return {
            "id": entity.id,
            "name": entity.name,
            "entity_type": entity.entity_type,
            "recognition_only": True,
        }

    raise ValueError(f"unknown grant_scope: {scope!r}")
