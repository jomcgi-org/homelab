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

from sqlalchemy.orm import aliased
from sqlmodel import Session, SQLModel, and_, or_, select

from grimoire.models import Entity, GameSession, KnowledgeGrant, PlayerCharacter

Viewer = str | None  # "dm", a player_character_id, or None before association
Context = Literal["lookup", "relationship"]

# Exact corpus identifiers, reviewed as rules/reference sources. Copyright is
# a separate public-reader policy and never establishes campaign knowledge.
# Unknown sources (including source-less globals) fail closed.
COMMON_KNOWLEDGE_BOOK_IDS = frozenset(
    {
        "system-reference-doc-5-1",
        "system-reference-doc-5-2",
        "black-flag-reference-document",
        "a5e-srd",
    }
)


def common_knowledge_predicate():
    return and_(
        Entity.is_global.is_(True), Entity.source_book.in_(COMMON_KNOWLEDGE_BOOK_IDS)
    )


def is_common_knowledge(entity: Entity) -> bool:
    return entity.is_global and entity.source_book in COMMON_KNOWLEDGE_BOOK_IDS


def campaign_player_predicate(campaign_id: str, viewer: Viewer):
    return (
        select(PlayerCharacter.id)
        .where(
            PlayerCharacter.id == viewer,
            PlayerCharacter.campaign_id == campaign_id,
        )
        .exists()
    )


def can_read_book(
    session: Session, campaign_id: str, viewer: Viewer, book_id: str
) -> bool:
    """Entity grants never authorize source pages, including name-only cards."""
    return viewer == "dm" or (
        book_id in COMMON_KNOWLEDGE_BOOK_IDS
        and session.exec(select(campaign_player_predicate(campaign_id, viewer))).one()
    )


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


def visible_entities_query(
    campaign_id: str, viewer: Viewer, *, not_granted_to: str | None = None
):
    """Builds the grant-overlay predicate query for a campaign.

    Player view (viewer is a player_character_id): LEFT JOIN knowledge_grant
    scoped to (this entity, this campaign, this player), keep rows where the
    entity has an explicit common-knowledge classification or a grant exists.
    A campaign-local character is required even for common knowledge.

    DM view (viewer == "dm"): global corpus entities plus private entities
    associated with this campaign by a grant or by their creating game session.
    Grants reach the caller as annotations. The campaign-scoped outer join can
    yield one row per grant; callers that need one row per entity aggregate it.

    ``not_granted_to`` hides entities that PC already knows: common entities
    or any grant scope. The correlated alias is separate from the DM join.
    Callers must authorize this DM-only filter before building the query.
    Returns a select() yielding (Entity, KnowledgeGrant | None) row tuples.
    """
    if viewer == "dm":
        join_condition = KnowledgeGrant.entity_id == Entity.id
        grant_filter = KnowledgeGrant.campaign_id == campaign_id
        campaign_session_ids = select(GameSession.id).where(
            GameSession.campaign_id == campaign_id
        )
        query = (
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
        if not_granted_to is not None:
            known_grant = aliased(KnowledgeGrant)
            known = (
                select(known_grant.id)
                .where(
                    known_grant.entity_id == Entity.id,
                    known_grant.campaign_id == campaign_id,
                    known_grant.player_character_id == not_granted_to,
                )
                .correlate(Entity)
                .exists()
            )
            query = query.where(~common_knowledge_predicate(), ~known)
        return query

    join_condition = and_(
        KnowledgeGrant.entity_id == Entity.id,
        KnowledgeGrant.player_character_id == viewer,
        KnowledgeGrant.campaign_id == campaign_id,
    )
    query = (
        select(Entity, KnowledgeGrant)
        .join(KnowledgeGrant, join_condition, isouter=True)
        .where(
            campaign_player_predicate(campaign_id, viewer),
            or_(common_knowledge_predicate(), KnowledgeGrant.id.is_not(None)),
        )
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
    - player, common knowledge or grant_scope == "full": spine and detail
      fields. Narrower grants cannot reduce the common-knowledge baseline.
    - grant_scope == "partial": spine identity fields only, plus
      revealed_details. No typed detail columns.
    - grant_scope == "name_only": an identity card in lookup, search and
      relationships, without typed fields, source text or descriptions.
    """
    spine = {field: getattr(entity, field) for field in _SPINE_FIELDS}

    if viewer == "dm":
        result = {**spine, **_flatten_detail(detail)}
        result["common_knowledge"] = is_common_knowledge(entity)
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

    if viewer is None:
        return None

    if is_common_knowledge(entity) or scope == "full":
        return {**spine, **_flatten_detail(detail)}

    if scope is None:
        return None

    if scope == "partial":
        identity = {field: getattr(entity, field) for field in _SPINE_IDENTITY_FIELDS}
        return {**identity, "revealed_details": grant.revealed_details}

    if scope == "name_only":
        return {
            "id": entity.id,
            "name": entity.name,
            "entity_type": entity.entity_type,
            "recognition_only": True,
        }

    raise ValueError(f"unknown grant_scope: {scope!r}")
