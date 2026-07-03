"""Grimoire campaign CRUD + grant-filtered entity read HTTP API (private tier).

Covers campaigns, player characters, knowledge grants (the DM's visibility
overlay), game sessions, and the grant-filtered entity/relationship read
paths. Vector search lives in a later module (search.py).

CRUD handlers return a Pydantic response model, never a SQLModel table
object (ADR 010: keep the DB row shape off the wire). The entity read paths
are the exception: their projected shape is scope-dependent (full spine,
partial identity + revealed_details, or a DM view with grant annotations),
so they return plain dicts, matching the heterogeneous-payload pattern
already used elsewhere (e.g. knowledge/router.py's `-> dict` handlers).
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import func
from sqlmodel import Session, or_, select

from app.db import get_session
from grimoire.models import (
    Campaign,
    ENTITY_DETAIL_MODELS,
    Entity,
    EntityType,
    GameSession,
    GrantScope,
    KnowledgeGrant,
    PlayerCharacter,
    Relationship,
    SessionStatus,
)
from grimoire.search import search_campaign
from grimoire.visibility import project_entity, visible_entities_query
from knowledge.api import get_embedding_client
from shared.embedding import EmbeddingClient

router = APIRouter(prefix="/api/grimoire", tags=["grimoire"])


# --- Campaigns --------------------------------------------------------


class CampaignCreateRequest(BaseModel):
    name: str
    dm_name: str | None = None


class CampaignView(BaseModel):
    id: str
    name: str
    dm_name: str | None
    created_at: datetime


def _get_campaign_or_404(session: Session, campaign_id: str) -> Campaign:
    campaign = session.get(Campaign, campaign_id)
    if campaign is None:
        raise HTTPException(status_code=404, detail="campaign not found")
    return campaign


@router.post("/campaigns", response_model=CampaignView)
def create_campaign(
    body: CampaignCreateRequest,
    session: Session = Depends(get_session),
) -> Campaign:
    campaign = Campaign(name=body.name, dm_name=body.dm_name)
    session.add(campaign)
    session.commit()
    session.refresh(campaign)
    return campaign


@router.get("/campaigns", response_model=list[CampaignView])
def list_campaigns(session: Session = Depends(get_session)) -> list[Campaign]:
    return session.exec(select(Campaign).order_by(Campaign.created_at)).all()


@router.get("/campaigns/{campaign_id}", response_model=CampaignView)
def get_campaign(campaign_id: str, session: Session = Depends(get_session)) -> Campaign:
    return _get_campaign_or_404(session, campaign_id)


# --- Player characters --------------------------------------------------


class CharacterCreateRequest(BaseModel):
    character_name: str
    player_name: str | None = None
    class_name: str | None = None
    level: int | None = None
    sheet: dict = {}


class CharacterView(BaseModel):
    id: str
    campaign_id: str
    player_name: str | None
    character_name: str
    class_name: str | None
    level: int | None
    sheet: dict


@router.post(
    "/campaigns/{campaign_id}/characters",
    response_model=CharacterView,
)
def create_character(
    campaign_id: str,
    body: CharacterCreateRequest,
    session: Session = Depends(get_session),
) -> PlayerCharacter:
    _get_campaign_or_404(session, campaign_id)
    character = PlayerCharacter(
        campaign_id=campaign_id,
        character_name=body.character_name,
        player_name=body.player_name,
        class_name=body.class_name,
        level=body.level,
        sheet=body.sheet,
    )
    session.add(character)
    session.commit()
    session.refresh(character)
    return character


@router.get(
    "/campaigns/{campaign_id}/characters",
    response_model=list[CharacterView],
)
def list_characters(
    campaign_id: str, session: Session = Depends(get_session)
) -> list[PlayerCharacter]:
    _get_campaign_or_404(session, campaign_id)
    return session.exec(
        select(PlayerCharacter)
        .where(PlayerCharacter.campaign_id == campaign_id)
        .order_by(PlayerCharacter.character_name)
    ).all()


# --- Knowledge grants ----------------------------------------------------


class GrantCreateRequest(BaseModel):
    entity_id: str
    player_character_id: str
    grant_scope: GrantScope
    revealed_details: dict | None = None
    granted_in_session: str | None = None


class GrantUpdateRequest(BaseModel):
    grant_scope: GrantScope | None = None
    revealed_details: dict | None = None


class GrantView(BaseModel):
    id: str
    campaign_id: str
    entity_id: str
    player_character_id: str
    grant_scope: GrantScope
    revealed_details: dict | None
    granted_in_session: str | None
    created_at: datetime


def _get_character_in_campaign_or_404(
    session: Session, campaign_id: str, player_character_id: str
) -> PlayerCharacter:
    character = session.get(PlayerCharacter, player_character_id)
    if character is None or character.campaign_id != campaign_id:
        raise HTTPException(
            status_code=404,
            detail="player character not found in this campaign",
        )
    return character


@router.post("/campaigns/{campaign_id}/grants", response_model=GrantView)
def create_grant(
    campaign_id: str,
    body: GrantCreateRequest,
    session: Session = Depends(get_session),
) -> KnowledgeGrant:
    _get_campaign_or_404(session, campaign_id)
    _get_character_in_campaign_or_404(session, campaign_id, body.player_character_id)

    existing = session.exec(
        select(KnowledgeGrant).where(
            KnowledgeGrant.entity_id == body.entity_id,
            KnowledgeGrant.player_character_id == body.player_character_id,
        )
    ).first()
    if existing is not None:
        raise HTTPException(
            status_code=409,
            detail="grant already exists for this entity and character",
        )

    grant = KnowledgeGrant(
        campaign_id=campaign_id,
        entity_id=body.entity_id,
        player_character_id=body.player_character_id,
        grant_scope=body.grant_scope,
        revealed_details=body.revealed_details,
        granted_in_session=body.granted_in_session,
    )
    session.add(grant)
    session.commit()
    session.refresh(grant)
    return grant


@router.get("/campaigns/{campaign_id}/grants", response_model=list[GrantView])
def list_grants(
    campaign_id: str, session: Session = Depends(get_session)
) -> list[KnowledgeGrant]:
    _get_campaign_or_404(session, campaign_id)
    return session.exec(
        select(KnowledgeGrant)
        .where(KnowledgeGrant.campaign_id == campaign_id)
        .order_by(KnowledgeGrant.created_at)
    ).all()


@router.patch(
    "/campaigns/{campaign_id}/grants/{grant_id}",
    response_model=GrantView,
)
def update_grant(
    campaign_id: str,
    grant_id: str,
    body: GrantUpdateRequest,
    session: Session = Depends(get_session),
) -> KnowledgeGrant:
    _get_campaign_or_404(session, campaign_id)
    grant = session.get(KnowledgeGrant, grant_id)
    if grant is None or grant.campaign_id != campaign_id:
        raise HTTPException(status_code=404, detail="grant not found")

    if body.grant_scope is not None:
        grant.grant_scope = body.grant_scope
    if body.revealed_details is not None:
        grant.revealed_details = body.revealed_details

    session.add(grant)
    session.commit()
    session.refresh(grant)
    return grant


# --- Entities (grant-filtered read paths) --------------------------------

# All read paths below build on visible_entities_query()/project_entity()
# from visibility.py rather than reimplementing the grant predicate.


def _resolve_viewer(session: Session, campaign_id: str, viewer_param: str) -> str:
    """Validates the ``as`` query param and returns a visibility.Viewer.

    "dm" passes straight through; anything else must be a player_character_id
    that belongs to this campaign (404 otherwise, same as any other
    campaign-scoped lookup).
    """
    if viewer_param == "dm":
        return "dm"
    _get_character_in_campaign_or_404(session, campaign_id, viewer_param)
    return viewer_param


def _aggregate_dm_rows(
    rows: list[tuple[Entity, KnowledgeGrant | None]],
    *,
    detail=None,
    context: str = "lookup",
) -> tuple[dict[str, dict[str, Any]], list[str]]:
    """Collapses the DM view's one-row-per-grant rows to one item per entity.

    visible_entities_query() for the DM viewer joins on campaign only (not a
    single player_character_id), so an entity with N grants comes back as N
    row tuples sharing the same Entity. This folds those into a single
    projected dict per entity with a "grants" list, preserving first-seen
    order.
    """
    aggregated: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for entity, grant in rows:
        projected = project_entity(entity, detail, grant, "dm", context=context)
        grant_dict = projected.pop("grant")
        existing = aggregated.get(entity.id)
        if existing is None:
            projected["grants"] = [grant_dict] if grant_dict else []
            aggregated[entity.id] = projected
            order.append(entity.id)
        elif grant_dict:
            existing["grants"].append(grant_dict)
    return aggregated, order


def _project_neighbor(
    session: Session, campaign_id: str, viewer: str, neighbor_id: str
) -> dict[str, Any] | None:
    """Projects a relationship neighbor in "relationship" context.

    Returns None when the neighbor is not visible to the viewer at all
    (ungranted and not global), so the caller can drop the edge entirely.
    A name_only grant still yields a recognition stub (not None) because
    context="relationship" tells project_entity() this is neighbor listing,
    not direct lookup. Never loads typed detail: neighbor listings stay
    spine-level like the list endpoint.
    """
    rows = session.exec(
        visible_entities_query(campaign_id, viewer).where(Entity.id == neighbor_id)
    ).all()
    if not rows:
        return None
    if viewer == "dm":
        aggregated, order = _aggregate_dm_rows(rows, context="relationship")
        return aggregated[order[0]]
    entity, grant = rows[0]
    return project_entity(entity, None, grant, viewer, context="relationship")


@router.get("/campaigns/{campaign_id}/entities")
def list_entities(
    campaign_id: str,
    as_: str = Query(alias="as"),
    entity_type: EntityType | None = Query(default=None, alias="type"),
    q: str | None = Query(default=None),
    session: Session = Depends(get_session),
) -> list[dict[str, Any]]:
    """Grant-filtered entity list, spine-level only (no typed detail rows).

    Returned dict shape is scope-dependent (full spine, partial identity +
    revealed_details, or a full spine + grants annotation for the DM), so
    this returns list[dict] rather than a fixed response_model, matching the
    heterogeneous-payload pattern used by knowledge/router.py.
    """
    _get_campaign_or_404(session, campaign_id)
    viewer = _resolve_viewer(session, campaign_id, as_)

    query = visible_entities_query(campaign_id, viewer).order_by(Entity.name)
    if entity_type is not None:
        query = query.where(Entity.entity_type == entity_type)
    if q:
        query = query.where(func.lower(Entity.name).contains(q.lower()))

    rows = session.exec(query).all()

    if viewer == "dm":
        aggregated, order = _aggregate_dm_rows(rows, context="lookup")
        return [aggregated[entity_id] for entity_id in order]

    items: list[dict[str, Any]] = []
    for entity, grant in rows:
        projected = project_entity(entity, None, grant, viewer, context="lookup")
        if projected is not None:
            items.append(projected)
    return items


@router.get("/campaigns/{campaign_id}/entities/{entity_id}")
def get_entity(
    campaign_id: str,
    entity_id: str,
    as_: str = Query(alias="as"),
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    """Single entity, scope-projected, with typed detail hydrated.

    Existence must not leak past the grant predicate: both a non-visible
    entity and a name_only grant return a plain 404 (project_entity()
    returns None for name_only in lookup context, so both cases collapse to
    the same check below).
    """
    _get_campaign_or_404(session, campaign_id)
    viewer = _resolve_viewer(session, campaign_id, as_)

    rows = session.exec(
        visible_entities_query(campaign_id, viewer).where(Entity.id == entity_id)
    ).all()
    if not rows:
        raise HTTPException(status_code=404, detail="entity not found")

    entity = rows[0][0]
    detail_model = ENTITY_DETAIL_MODELS.get(entity.entity_type)
    detail = session.get(detail_model, entity_id) if detail_model else None

    if viewer == "dm":
        aggregated, order = _aggregate_dm_rows(rows, detail=detail, context="lookup")
        return aggregated[order[0]]

    grant = rows[0][1]
    projected = project_entity(entity, detail, grant, viewer, context="lookup")
    if projected is None:
        raise HTTPException(status_code=404, detail="entity not found")
    return projected


@router.get("/campaigns/{campaign_id}/entities/{entity_id}/relationships")
def list_entity_relationships(
    campaign_id: str,
    entity_id: str,
    as_: str = Query(alias="as"),
    session: Session = Depends(get_session),
) -> list[dict[str, Any]]:
    """1-hop relationship edges from/to entity_id, grant-filtered per neighbor.

    The center entity must itself be visible to the viewer in lookup context
    (same 404-or-not check as get_entity, existence must not leak). Each
    neighbor is then projected independently in "relationship" context so a
    name_only neighbor becomes a recognition stub instead of vanishing, while
    a wholly invisible neighbor (ungranted, non-global) drops its edge.
    """
    _get_campaign_or_404(session, campaign_id)
    viewer = _resolve_viewer(session, campaign_id, as_)

    center_rows = session.exec(
        visible_entities_query(campaign_id, viewer).where(Entity.id == entity_id)
    ).all()
    if not center_rows:
        raise HTTPException(status_code=404, detail="entity not found")
    if viewer != "dm":
        center_entity, center_grant = center_rows[0]
        center_projected = project_entity(
            center_entity, None, center_grant, viewer, context="lookup"
        )
        if center_projected is None:
            raise HTTPException(status_code=404, detail="entity not found")

    outgoing = session.exec(
        select(Relationship).where(Relationship.from_entity_id == entity_id)
    ).all()
    incoming = session.exec(
        select(Relationship).where(Relationship.to_entity_id == entity_id)
    ).all()
    edges = [(rel, "out", rel.to_entity_id) for rel in outgoing] + [
        (rel, "in", rel.from_entity_id) for rel in incoming
    ]

    items: list[dict[str, Any]] = []
    for rel, direction, neighbor_id in edges:
        neighbor = _project_neighbor(session, campaign_id, viewer, neighbor_id)
        if neighbor is None:
            continue
        items.append(
            {
                "rel_type": rel.rel_type,
                "direction": direction,
                "properties": rel.properties,
                "entity": neighbor,
            }
        )
    return items


# --- Vector search ---------------------------------------------------

# The embedding client is injected through knowledge.api.get_embedding_client
# (the sanctioned cross-domain import boundary, see knowledge/api.py's
# docstring), rather than grimoire duplicating its own copy: knowledge
# already owns this DI seam and grimoire has no reason to diverge from it.
# Tests override it the same way knowledge's own tests do, via
# ``app.dependency_overrides[get_embedding_client]``.


@router.get("/campaigns/{campaign_id}/search")
async def search_campaign_route(
    campaign_id: str,
    as_: str = Query(alias="as"),
    q: str = Query(min_length=1),
    k: int = Query(default=10, ge=1, le=50),
    session: Session = Depends(get_session),
    embed_client: EmbeddingClient = Depends(get_embedding_client),
) -> list[dict[str, Any]]:
    """kNN search over embedding, grant-filtered, mixed entity/chunk hits.

    All visibility filtering happens in search.search_campaign, which builds
    on the same visible_entities_query()/project_entity() helpers as the
    entity read paths above.
    """
    _get_campaign_or_404(session, campaign_id)
    viewer = _resolve_viewer(session, campaign_id, as_)
    return await search_campaign(session, embed_client, campaign_id, viewer, q, k=k)


# --- Game sessions ---------------------------------------------------


class GameSessionUpdateRequest(BaseModel):
    status: SessionStatus


class GameSessionView(BaseModel):
    id: str
    campaign_id: str
    status: SessionStatus
    started_at: datetime
    ended_at: datetime | None


@router.post(
    "/campaigns/{campaign_id}/sessions",
    response_model=GameSessionView,
)
def create_game_session(
    campaign_id: str,
    session: Session = Depends(get_session),
) -> GameSession:
    _get_campaign_or_404(session, campaign_id)

    active = session.exec(
        select(GameSession).where(
            GameSession.campaign_id == campaign_id,
            or_(GameSession.status != "ended", GameSession.status.is_(None)),
        )
    ).first()
    if active is not None:
        raise HTTPException(
            status_code=409,
            detail="an active or paused session already exists for this campaign",
        )

    game_session = GameSession(campaign_id=campaign_id)
    session.add(game_session)
    session.commit()
    session.refresh(game_session)
    return game_session


@router.patch(
    "/campaigns/{campaign_id}/sessions/{session_id}",
    response_model=GameSessionView,
)
def update_game_session(
    campaign_id: str,
    session_id: str,
    body: GameSessionUpdateRequest,
    session: Session = Depends(get_session),
) -> GameSession:
    _get_campaign_or_404(session, campaign_id)
    game_session = session.get(GameSession, session_id)
    if game_session is None or game_session.campaign_id != campaign_id:
        raise HTTPException(status_code=404, detail="game session not found")

    game_session.status = body.status
    if body.status == "ended" and game_session.ended_at is None:
        game_session.ended_at = datetime.now(timezone.utc)

    session.add(game_session)
    session.commit()
    session.refresh(game_session)
    return game_session
