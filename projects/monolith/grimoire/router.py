"""Grimoire campaign CRUD HTTP API (private tier only).

Covers campaigns, player characters, knowledge grants (the DM's visibility
overlay), and game sessions. Entity lookup, relationships, and vector search
live in later modules (search.py); this router is deliberately just the
campaign-management surface the /app/grimoire demo needs to set up a game.

Every handler returns a Pydantic response model, never a SQLModel table
object (ADR 010: keep the DB row shape off the wire).
"""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlmodel import Session, or_, select

from app.db import get_session
from grimoire.models import (
    Campaign,
    GameSession,
    GrantScope,
    KnowledgeGrant,
    PlayerCharacter,
    SessionStatus,
)

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
